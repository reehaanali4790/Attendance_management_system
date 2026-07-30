// Constants & Global State
const API_BASE = "";
const TOKEN_KEY = "ams_auth_token";
let currentTab = "dashboard";
let employeeListFilter = null; // null | "active"
let nextSyncCountdown = 300; // in seconds
let countdownTimer = null;
let trendChart = null;
let allShifts = [];
let allDepartments = [];
let allLeaveTypes = [];
let allEmployeesCache = [];
let leavesCache = [];
let dashboardDrilldownBound = false;
let attendanceFiltersBound = false;

// --- Authentication ---
function getAuthToken() {
    return localStorage.getItem(TOKEN_KEY);
}

function setAuthToken(token) {
    localStorage.setItem(TOKEN_KEY, token);
}

function clearAuthToken() {
    localStorage.removeItem(TOKEN_KEY);
}

function showLoginScreen(message = "") {
    const loginScreen = document.getElementById("login-screen");
    const appRoot = document.getElementById("app-root");
    const loginError = document.getElementById("login-error");

    document.body.classList.add("auth-login-mode");

    if (loginScreen) loginScreen.hidden = false;
    if (appRoot) appRoot.hidden = true;

    if (loginError) {
        if (message) {
            loginError.textContent = message;
            loginError.hidden = false;
        } else {
            loginError.hidden = true;
            loginError.textContent = "";
        }
    }

    if (countdownTimer) {
        clearInterval(countdownTimer);
        countdownTimer = null;
    }
}

function showAppShell() {
    const loginScreen = document.getElementById("login-screen");
    const appRoot = document.getElementById("app-root");

    document.body.classList.remove("auth-login-mode");

    if (loginScreen) loginScreen.hidden = true;
    if (appRoot) appRoot.hidden = false;
}

async function apiFetch(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    const token = getAuthToken();

    if (token) {
        headers["Authorization"] = `Bearer ${token}`;
    }

    if (options.body && typeof options.body === "string" && !headers["Content-Type"]) {
        headers["Content-Type"] = "application/json";
    }

    const response = await fetch(url, { ...options, headers });

    if (response.status === 401) {
        clearAuthToken();
        showLoginScreen("Session expired. Please sign in again.");
        throw new Error("Unauthorized");
    }

    return response;
}

async function loginUser(username, password) {
    const form = new URLSearchParams();
    form.append("username", username);
    form.append("password", password);

    const response = await fetch(`${API_BASE}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: form.toString(),
    });

    if (!response.ok) {
        let detail = "Incorrect username or password";
        try {
            const data = await response.json();
            if (data.detail) detail = data.detail;
        } catch (_) {}
        throw new Error(detail);
    }

    const data = await response.json();
    setAuthToken(data.access_token);
}

function setupAuth() {
    const loginForm = document.getElementById("login-form");
    const logoutBtn = document.getElementById("btn-logout");

    if (loginForm) {
        loginForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const username = document.getElementById("login-username").value.trim();
            const password = document.getElementById("login-password").value;
            const submitBtn = document.getElementById("login-submit-btn");
            const loginError = document.getElementById("login-error");

            submitBtn.disabled = true;
            submitBtn.textContent = "Signing in...";
            loginError.hidden = true;

            try {
                await loginUser(username, password);
                showAppShell();
                initializeAuthenticatedApp();
            } catch (error) {
                loginError.textContent = error.message || "Login failed";
                loginError.hidden = false;
            } finally {
                submitBtn.disabled = false;
                submitBtn.textContent = "Sign In";
            }
        });
    }

    if (logoutBtn) {
        logoutBtn.addEventListener("click", () => {
            clearAuthToken();
            showLoginScreen();
        });
    }
}

async function bootstrapApp() {
    setupAuth();

    const token = getAuthToken();
    if (!token) {
        showLoginScreen();
        return;
    }

    try {
        const response = await apiFetch(`${API_BASE}/api/auth/me`);
        if (!response.ok) throw new Error("Invalid session");
        showAppShell();
        initializeAuthenticatedApp();
    } catch (_) {
        clearAuthToken();
        showLoginScreen();
    }
}

function initializeAuthenticatedApp() {
    setupTabNavigation();
    setupDashboardDrilldown();
    setupAttendanceFilters();
    setupSettingsForms();
    setupEmployeeModal();
    setupDepartmentForm();
    setupLeaveForm();

    loadDashboardData();
    loadAllShifts();
    loadAllDepartments();
    loadLeaveTypes();
    startCountdown();
}

// DOM Elements
const tabButtons = document.querySelectorAll(".nav-item");
const tabPanes = document.querySelectorAll(".tab-pane");
const connectionDot = document.getElementById("connection-dot");
const connectionTitle = document.getElementById("connection-title");
const connectionIp = document.getElementById("connection-ip");
const syncCountdown = document.getElementById("sync-countdown");
const syncLastTime = document.getElementById("sync-last-time");
const btnSync = document.getElementById("btn-sync");
const syncIcon = document.getElementById("sync-icon");
const syncBtnText = document.getElementById("sync-btn-text");
const btnSyncFull = document.getElementById("btn-sync-full");
const syncStatusHint = document.getElementById("sync-status-hint");

// Initialize application on load
document.addEventListener("DOMContentLoaded", () => {
    bootstrapApp();
});

// 1. Tab Switching System
function switchTab(tabName, options = {}) {
    const buttons = document.querySelectorAll(".nav-item");
    const panes = document.querySelectorAll(".tab-pane");

    buttons.forEach(button => {
        button.classList.toggle("active", button.getAttribute("data-tab") === tabName);
    });

    panes.forEach(pane => pane.classList.remove("active"));
    const targetPane = document.getElementById(`tab-${tabName}`);
    if (targetPane) targetPane.classList.add("active");

    currentTab = tabName;
    updateHeaderTitles();

    if (options.subtitle) {
        const subtitleEl = document.getElementById("active-tab-subtitle");
        if (subtitleEl) subtitleEl.textContent = options.subtitle;
    }

    if (tabName === "dashboard") {
        loadDashboardData();
    } else if (tabName === "attendance") {
        loadAttendanceLogs();
    } else if (tabName === "employees") {
        loadEmployees();
    } else if (tabName === "departments") {
        loadDepartments();
    } else if (tabName === "leaves") {
        loadLeaveFormOptions();
        loadLeaves();
    } else if (tabName === "settings") {
        loadSettings();
        loadAllShifts();
    }
}

function setupTabNavigation() {
    document.querySelectorAll(".nav-item").forEach(button => {
        button.addEventListener("click", () => {
            const tab = button.getAttribute("data-tab");
            if (tab === "employees") {
                employeeListFilter = null;
            }
            switchTab(tab);
        });
    });
}

function getTodayDateString() {
    return new Date().toISOString().split("T")[0];
}

function applyAttendanceFilters({ startDate, endDate, status = "", search = "", departmentId = "" }) {
    const startEl = document.getElementById("filter-start-date");
    const endEl = document.getElementById("filter-end-date");
    const statusEl = document.getElementById("filter-status");
    const searchEl = document.getElementById("filter-search-input");
    const deptEl = document.getElementById("filter-department");

    if (startEl) startEl.value = startDate;
    if (endEl) endEl.value = endDate;
    if (statusEl) statusEl.value = status;
    if (searchEl) searchEl.value = search;
    if (deptEl) deptEl.value = departmentId;
}

function setupDashboardDrilldown() {
    if (dashboardDrilldownBound) return;
    dashboardDrilldownBound = true;

    document.addEventListener("click", (event) => {
        const card = event.target.closest(".metric-drill");
        if (!card) return;

        const drill = card.getAttribute("data-drill");
        if (!drill) return;

        event.preventDefault();
        drillFromDashboard(drill);
    });

    document.addEventListener("keydown", (event) => {
        const card = event.target.closest(".metric-drill");
        if (!card) return;
        if (event.key !== "Enter" && event.key !== " ") return;

        event.preventDefault();
        const drill = card.getAttribute("data-drill");
        if (drill) drillFromDashboard(drill);
    });
}

function drillFromDashboard(drillType) {
    const today = getTodayDateString();

    if (drillType === "active") {
        employeeListFilter = "active";
        switchTab("employees", {
            subtitle: "Active employees in your organization.",
        });
        return;
    }

    const statusMap = {
        present: "Present",
        late: "Late",
        absent: "Absent",
        "on-leave": "On Leave",
    };

    applyAttendanceFilters({
        startDate: today,
        endDate: today,
        status: statusMap[drillType] || "",
        search: "",
        departmentId: "",
    });

    const labels = {
        present: "Employees marked Present today.",
        late: "Employees marked Late today.",
        absent: "Employees marked Absent today.",
        "on-leave": "Employees on Leave today.",
        hours: "All attendance records for today.",
    };

    switchTab("attendance", {
        subtitle: labels[drillType] || "Search, filter, and inspect detailed employee timesheets.",
    });
}

function updateHeaderTitles() {
    const titleEl = document.getElementById("active-tab-title");
    const subtitleEl = document.getElementById("active-tab-subtitle");
    
    if (currentTab === "dashboard") {
        titleEl.textContent = "Dashboard";
        subtitleEl.textContent = "Real-time attendance analysis and activity metrics.";
    } else if (currentTab === "attendance") {
        titleEl.textContent = "Attendance Log";
        subtitleEl.textContent = "Search, filter, and inspect detailed employee timesheets.";
    } else if (currentTab === "employees") {
        titleEl.textContent = "Employees Directory";
        subtitleEl.textContent = "Assign departments, shifts, and manage employee profiles.";
    } else if (currentTab === "departments") {
        titleEl.textContent = "Departments";
        subtitleEl.textContent = "Create departments and organize your workforce.";
    } else if (currentTab === "leaves") {
        titleEl.textContent = "Leave Management";
        subtitleEl.textContent = "Record leave on behalf of employees and track application status.";
    } else if (currentTab === "settings") {
        titleEl.textContent = "System Configurations";
        subtitleEl.textContent = "Manage hardware connectivity parameters and working shift definitions.";
    }
}

// 2. Countdown and Manual Sync
function startCountdown() {
    if (countdownTimer) clearInterval(countdownTimer);
    
    countdownTimer = setInterval(() => {
        if (nextSyncCountdown > 0) {
            nextSyncCountdown--;
            const mins = String(Math.floor(nextSyncCountdown / 60)).padStart(2, "0");
            const secs = String(nextSyncCountdown % 60).padStart(2, "0");
            syncCountdown.textContent = `${mins}:${secs}`;
        } else {
            // Trigger sync automatically when countdown hits zero
            triggerSync(true);
        }
    }, 1000);
}

btnSync.addEventListener("click", () => {
    triggerSync(false, false);
});

if (btnSyncFull) {
    btnSyncFull.addEventListener("click", () => {
        const confirmed = window.confirm(
            "Full sync imports all device punches and recalculates historical attendance. This can take several minutes. Continue?"
        );
        if (confirmed) triggerSync(false, true);
    });
}

function formatSyncMode(mode) {
    if (mode === "manual_full") return "full history";
    if (mode === "full") return "current month";
    return "recent data";
}

async function triggerSync(isAuto = false, full = false) {
    if (btnSync.classList.contains("syncing")) return;
    
    btnSync.classList.add("syncing");
    if (btnSyncFull) btnSyncFull.disabled = true;
    syncBtnText.textContent = full ? "Full sync running..." : "Syncing recent data...";
    if (syncStatusHint) {
        syncStatusHint.textContent = full
            ? "Importing all device logs and recalculating attendance history..."
            : "Importing recent punches and updating today/yesterday attendance...";
    }
    
    try {
        const url = full ? `${API_BASE}/api/sync?full=true` : `${API_BASE}/api/sync`;
        const response = await apiFetch(url, { method: "POST" });
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || "Sync failed");
        }
        
        const result = await response.json();
        console.log("Sync Complete:", result);

        const modeLabel = formatSyncMode(result.sync_mode);
        const newLogs = result.logs_synced ?? 0;
        if (syncStatusHint) {
            syncStatusHint.textContent = `Last sync: ${modeLabel} · ${newLogs} new log(s) imported`;
        }
        if (!isAuto) {
            alert(`Synchronization complete (${modeLabel}). New logs imported: ${newLogs}`);
        }
        
        // Reload current active tab
        if (currentTab === "dashboard") loadDashboardData();
        else if (currentTab === "attendance") loadAttendanceLogs();
        else if (currentTab === "employees") loadEmployees();
        else if (currentTab === "leaves") loadLeaves();
        
        // Reset countdown timer
        nextSyncCountdown = 300;
        
    } catch (error) {
        console.error("Sync Error:", error);
        if (syncStatusHint) {
            syncStatusHint.textContent = "Sync failed. The server may still be processing — try again in a minute.";
        }
        if (!isAuto) {
            alert(`Synchronization failed: ${error.message}`);
        }
    } finally {
        btnSync.classList.remove("syncing");
        if (btnSyncFull) btnSyncFull.disabled = false;
        syncBtnText.textContent = "Sync Now";
    }
}

// 3. Dashboard Functionality
async function loadDashboardData() {
    try {
        const response = await apiFetch(`${API_BASE}/api/dashboard`);
        if (!response.ok) throw new Error("Failed to fetch dashboard stats");
        const data = await response.json();
        
        const setStat = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        };

        setStat("stat-active", data.total_employees);
        setStat("stat-present", data.present_today);
        setStat("stat-late", data.late_today);
        setStat("stat-absent", data.absent_today);
        setStat("stat-hours", `${data.avg_work_hours_today}h`);
        setStat("stat-on-leave", data.on_leave_today || 0);
        
        // Update Connection status bar
        updateConnectionStatus(data.connection_status, data.last_sync_time);
        
        // Set last sync timestamp
        if (data.last_sync_time) {
            const dt = new Date(data.last_sync_time);
            syncLastTime.textContent = dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + " (" + dt.toLocaleDateString() + ")";
        } else {
            syncLastTime.textContent = "Never";
        }
        
        // Update Countdown
        nextSyncCountdown = data.next_sync_in_seconds;
        
        // Render recent punches feed
        renderRecentPunches(data.recent_punches);
        
        // Render Chart
        renderTrendChart(data.weekly_trend);
        
    } catch (error) {
        console.error("Dashboard error:", error);
    }
}

function updateConnectionStatus(status, lastSync) {
    connectionDot.className = "pulse-indicator";
    
    if (status === "CONNECTED") {
        connectionDot.classList.add("status-online");
        connectionTitle.textContent = "Hardware Connected";
        connectionTitle.style.color = "var(--color-green)";
    } else {
        connectionDot.classList.add("status-offline");
        connectionTitle.textContent = "Disconnected";
        connectionTitle.style.color = "var(--color-red)";
    }
}

function renderRecentPunches(punches) {
    const listEl = document.getElementById("recent-punches-list");
    listEl.innerHTML = "";
    
    if (punches.length === 0) {
        listEl.innerHTML = `<div class="empty-state"><p>No punches logged for today.</p></div>`;
        return;
    }
    
    punches.forEach(punch => {
        const initials = punch.employee_name.split(" ").map(n => n[0]).join("").toUpperCase().substring(0, 2);
        const time = new Date(punch.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        
        const isCheckIn = punch.punch_type.toLowerCase() === "check in" || punch.punch_type.toLowerCase() === "break in";
        const badgeClass = isCheckIn ? "bg-green-badge" : "bg-orange-badge";
        
        const row = document.createElement("div");
        row.className = "feed-row";
        row.innerHTML = `
            <div class="feed-avatar">${initials}</div>
            <div class="feed-details">
                <span class="feed-name">${punch.employee_name}</span>
                <span class="feed-time">ID: ${punch.user_id} • ${time}</span>
            </div>
            <span class="badge ${badgeClass}">${punch.punch_type}</span>
        `;
        listEl.appendChild(row);
    });
}

function renderTrendChart(weeklyTrend) {
    const ctx = document.getElementById("trendChart").getContext("2d");
    
    const dates = Object.keys(weeklyTrend);
    const presentData = dates.map(d => weeklyTrend[d].present);
    const lateData = dates.map(d => weeklyTrend[d].late);
    const absentData = dates.map(d => weeklyTrend[d].absent);
    const onLeaveData = dates.map(d => weeklyTrend[d].on_leave || 0);
    
    // Format dates to display nicely (e.g. "Mon, Jul 20")
    const formattedLabels = dates.map(d => {
        const dateObj = new Date(d);
        return dateObj.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
    });
    
    if (trendChart) trendChart.destroy();
    
    trendChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: formattedLabels,
            datasets: [
                {
                    label: 'On Time',
                    data: presentData,
                    backgroundColor: 'rgba(16, 185, 129, 0.75)',
                    borderColor: '#10b981',
                    borderWidth: 1,
                    borderRadius: 4,
                },
                {
                    label: 'Late Check-In',
                    data: lateData,
                    backgroundColor: 'rgba(245, 158, 11, 0.75)',
                    borderColor: '#f59e0b',
                    borderWidth: 1,
                    borderRadius: 4,
                },
                {
                    label: 'Absent',
                    data: absentData,
                    backgroundColor: 'rgba(239, 68, 68, 0.75)',
                    borderColor: '#ef4444',
                    borderWidth: 1,
                    borderRadius: 4,
                },
                {
                    label: 'On Leave',
                    data: onLeaveData,
                    backgroundColor: 'rgba(139, 92, 246, 0.75)',
                    borderColor: '#8b5cf6',
                    borderWidth: 1,
                    borderRadius: 4,
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: {
                    stacked: true,
                    grid: { color: 'rgba(255, 255, 255, 0.04)' },
                    ticks: { color: '#9ca3af', font: { family: 'Plus Jakarta Sans', size: 10 } }
                },
                y: {
                    stacked: true,
                    grid: { color: 'rgba(255, 255, 255, 0.04)' },
                    ticks: { 
                        color: '#9ca3af', 
                        font: { family: 'Plus Jakarta Sans', size: 10 },
                        stepSize: 1
                    }
                }
            },
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: {
                        color: '#f3f4f6',
                        boxWidth: 12,
                        font: { family: 'Outfit', size: 11 }
                    }
                }
            }
        }
    });
}

// 4. Attendance Log Table
function setupAttendanceFilters() {
    if (attendanceFiltersBound) return;
    attendanceFiltersBound = true;

    // Set default dates: start of current month to today
    const today = new Date();
    const firstDay = new Date(today.getFullYear(), today.getMonth(), 1);
    
    document.getElementById("filter-start-date").value = firstDay.toISOString().split("T")[0];
    document.getElementById("filter-end-date").value = today.toISOString().split("T")[0];
    
    document.getElementById("btn-apply-filters").addEventListener("click", loadAttendanceLogs);
    document.getElementById("btn-clear-filters").addEventListener("click", () => {
        document.getElementById("filter-start-date").value = firstDay.toISOString().split("T")[0];
        document.getElementById("filter-end-date").value = today.toISOString().split("T")[0];
        document.getElementById("filter-status").value = "";
        document.getElementById("filter-search-input").value = "";
        loadAttendanceLogs();
    });
    
    document.getElementById("btn-export-excel").addEventListener("click", async () => {
        const start = document.getElementById("filter-start-date").value;
        const end = document.getElementById("filter-end-date").value;
        const status = document.getElementById("filter-status").value;
        const search = document.getElementById("filter-search-input").value;
        const departmentId = document.getElementById("filter-department").value;
        
        let url = `${API_BASE}/api/attendance/export?`;
        if (start) url += `start_date=${start}&`;
        if (end) url += `end_date=${end}&`;
        if (status) url += `status=${status}&`;
        if (search) url += `search=${encodeURIComponent(search)}&`;
        if (departmentId) url += `department_id=${departmentId}&`;

        try {
            const response = await apiFetch(url);
            if (!response.ok) throw new Error("Export failed");
            const blob = await response.blob();
            const downloadUrl = window.URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = downloadUrl;
            link.download = `attendance_export_${new Date().toISOString().slice(0, 10)}.xlsx`;
            document.body.appendChild(link);
            link.click();
            link.remove();
            window.URL.revokeObjectURL(downloadUrl);
        } catch (error) {
            alert(error.message || "Could not export attendance data.");
        }
    });
    
    populateDepartmentSelect("filter-department", true, null, "All Departments");
}

async function loadAttendanceLogs() {
    const tableBody = document.getElementById("attendance-table-body");
    tableBody.innerHTML = `<tr><td colspan="11" class="text-center">Loading attendance records...</td></tr>`;
    
    const start = document.getElementById("filter-start-date").value;
    const end = document.getElementById("filter-end-date").value;
    const status = document.getElementById("filter-status").value;
    const search = document.getElementById("filter-search-input").value;
    const departmentId = document.getElementById("filter-department").value;
    
    let url = `${API_BASE}/api/attendance?`;
    if (start) url += `start_date=${start}&`;
    if (end) url += `end_date=${end}&`;
    if (status) url += `status=${status}&`;
    if (search) url += `search=${encodeURIComponent(search)}&`;
    if (departmentId) url += `department_id=${departmentId}&`;
    
    try {
        const response = await apiFetch(url);
        if (!response.ok) throw new Error("Could not load attendance logs");
        const logs = await response.json();
        
        tableBody.innerHTML = "";
        
        if (logs.length === 0) {
            tableBody.innerHTML = `<tr><td colspan="11" class="text-center">No attendance logs found matching filters.</td></tr>`;
            return;
        }
        
        logs.forEach(log => {
            const formatTime = (isoString) => {
                if (!isoString) return "--:--";
                return new Date(isoString).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            };
            
            const tr = document.createElement("tr");
            
            let statusBadgeClass = "bg-green-badge";
            if (log.status === "Late") statusBadgeClass = "bg-orange-badge";
            if (log.status === "Absent") statusBadgeClass = "bg-red-badge";
            if (log.status === "Left Early") statusBadgeClass = "bg-purple-badge";
            if (log.status === "On Leave") statusBadgeClass = "bg-teal-badge";
            if (log.status === "Half Day") statusBadgeClass = "bg-blue-badge";
            
            tr.innerHTML = `
                <td><strong>${log.date}</strong></td>
                <td>${log.employee_name}</td>
                <td>${log.department_name || "-"}</td>
                <td><code class="conn-ip">${log.employee_user_id}</code></td>
                <td>${formatTime(log.check_in)}</td>
                <td>${formatTime(log.check_out)}</td>
                <td>${log.work_hours > 0 ? log.work_hours + " hrs" : "--"}</td>
                <td><span class="badge ${statusBadgeClass}">${log.status}</span></td>
                <td>${log.late_minutes > 0 ? log.late_minutes + "m" : "-"}</td>
                <td>${log.early_leave_minutes > 0 ? log.early_leave_minutes + "m" : "-"}</td>
                <td><small class="page-subtitle">${log.remarks || ""}</small></td>
            `;
            tableBody.appendChild(tr);
        });
        
    } catch (error) {
        console.error("Log fetch error:", error);
        tableBody.innerHTML = `<tr><td colspan="11" class="text-center text-glow-red">Error loading logs: ${error.message}</td></tr>`;
    }
}

// 5. Employees Directory Management
async function loadEmployees() {
    const tableBody = document.getElementById("employees-table-body");
    const scopeBanner = document.getElementById("employees-scope-banner");
    tableBody.innerHTML = `<tr><td colspan="7" class="text-center">Loading employee directory...</td></tr>`;

    if (employeeListFilter === "active") {
        scopeBanner.textContent = "Showing active employees only (from dashboard).";
        scopeBanner.classList.remove("hidden");
    } else {
        scopeBanner.classList.add("hidden");
        scopeBanner.textContent = "";
    }
    
    try {
        const response = await apiFetch(`${API_BASE}/api/employees`);
        if (!response.ok) throw new Error("Could not load employees");
        let employees = await response.json();

        if (employeeListFilter === "active") {
            employees = employees.filter(emp => emp.is_active);
        }
        
        tableBody.innerHTML = "";
        
        if (employees.length === 0) {
            const emptyMessage = employeeListFilter === "active"
                ? "No active employees found."
                : "No employees registered yet. Run sync to load device users.";
            tableBody.innerHTML = `<tr><td colspan="7" class="text-center">${emptyMessage}</td></tr>`;
            return;
        }
        
        employees.forEach(emp => {
            const shiftName = emp.shift ? emp.shift.name : "Unassigned";
            const deptName = emp.department ? emp.department.name : "Unassigned";
            const startStr = emp.shift ? emp.shift.start_time.substring(0, 5) : "";
            const endStr = emp.shift ? emp.shift.end_time.substring(0, 5) : "";
            const shiftTimes = emp.shift ? `${startStr} - ${endStr}` : "-";
            const roleLabel = emp.privilege === 14 ? "Admin" : "Standard User";
            const statusBadgeClass = emp.is_active ? "bg-green-badge" : "bg-red-badge";
            const statusLabel = emp.is_active ? "Active" : "Inactive";
            const safeName = emp.name.replace(/'/g, "\\'");

            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td><strong>${emp.name}</strong></td>
                <td><code class="conn-ip">${emp.user_id}</code></td>
                <td>${deptName}</td>
                <td>${shiftName}<br><small class="page-subtitle">${shiftTimes}</small></td>
                <td>${roleLabel}</td>
                <td><span class="badge ${statusBadgeClass}">${statusLabel}</span></td>
                <td>
                    <button class="btn btn-secondary btn-sm" onclick="openEditEmployeeModal(${emp.id}, '${safeName}', ${emp.shift_id || 'null'}, ${emp.department_id || 'null'}, ${emp.is_active})">
                        Configure
                    </button>
                </td>
            `;
            tableBody.appendChild(tr);
        });
        
    } catch (error) {
        console.error("Employee list error:", error);
        tableBody.innerHTML = `<tr><td colspan="7" class="text-center text-glow-red">Error: ${error.message}</td></tr>`;
    }
}

// 6. Employee Edit Modal Actions
function setupEmployeeModal() {
    const modal = document.getElementById("employee-modal");
    const closeBtn = document.getElementById("btn-close-modal");
    
    closeBtn.addEventListener("click", () => {
        modal.style.display = "none";
    });
    
    window.addEventListener("click", (e) => {
        if (e.target === modal) {
            modal.style.display = "none";
        }
    });
    
    document.getElementById("edit-employee-form").addEventListener("submit", async (e) => {
        e.preventDefault();

        const saveBtn = e.target.querySelector('button[type="submit"]');
        const empId = document.getElementById("edit-emp-id").value;
        const shiftId = document.getElementById("edit-emp-shift").value;
        const deptId = document.getElementById("edit-emp-department").value;
        const isActive = document.getElementById("edit-emp-active").checked;

        if (!shiftId) {
            alert("Please select a shift before saving.");
            return;
        }

        if (saveBtn) {
            saveBtn.disabled = true;
            saveBtn.textContent = "Saving...";
        }

        try {
            const response = await apiFetch(`${API_BASE}/api/employees/${empId}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    shift_id: parseInt(shiftId, 10),
                    department_id: deptId ? parseInt(deptId, 10) : 0,
                    is_active: isActive
                })
            });

            if (!response.ok) {
                const errData = await response.json().catch(() => ({}));
                const detail = errData.detail || response.statusText || "Could not update employee settings";
                throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
            }

            modal.style.display = "none";
            await loadEmployees();
        } catch (error) {
            alert(`Error: ${error.message}`);
        } finally {
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.textContent = "Save Employee Details";
            }
        }
    });
}

window.openEditEmployeeModal = async function(id, name, shiftId, departmentId, isActive) {
    const modal = document.getElementById("employee-modal");
    if (!allShifts.length) {
        try {
            await fetchAllShifts();
        } catch (error) {
            alert(`Could not load shifts: ${error.message}`);
            return;
        }
    }

    document.getElementById("edit-emp-id").value = id;
    document.getElementById("edit-emp-name").value = name;
    document.getElementById("edit-emp-active").checked = isActive;

    const shiftSelect = document.getElementById("edit-emp-shift");
    shiftSelect.innerHTML = "";
    if (!allShifts.length) {
        shiftSelect.innerHTML = `<option value="">No shifts configured — add one in Settings</option>`;
    } else {
        allShifts.forEach(shift => {
            const option = document.createElement("option");
            option.value = shift.id;
            option.textContent = `${shift.name} (${shift.start_time.substring(0, 5)} - ${shift.end_time.substring(0, 5)})`;
            if (shiftId && Number(shift.id) === Number(shiftId)) option.selected = true;
            shiftSelect.appendChild(option);
        });
    }

    populateDepartmentSelect("edit-emp-department", true, departmentId, "Unassigned");

    modal.style.display = "flex";
};

// 7. Settings Page Operations (Device Settings & Shift Settings)
function formatTimeForApi(value) {
    if (!value) return value;
    return value.length === 5 ? `${value}:00` : value;
}

function getHardwareSettingsPayload() {
    return {
        ip_address: document.getElementById("device-ip").value,
        port: parseInt(document.getElementById("device-port").value),
        comm_key: parseInt(document.getElementById("device-key").value) || 0,
        sync_interval_minutes: parseInt(document.getElementById("sync-interval").value)
    };
}

function getWorkWeekSettingsPayload() {
    return {
        saturday_is_working_day: document.getElementById("saturday-working").checked,
        saturday_start_time: formatTimeForApi(document.getElementById("saturday-start").value),
        saturday_end_time: formatTimeForApi(document.getElementById("saturday-end").value),
        saturday_grace_period_minutes: parseInt(document.getElementById("saturday-grace").value),
        saturday_late_after_minutes: parseInt(document.getElementById("saturday-late").value),
        sunday_is_working_day: document.getElementById("sunday-working").checked
    };
}

function getFullSettingsPayload() {
    return {
        ...getHardwareSettingsPayload(),
        ...getWorkWeekSettingsPayload()
    };
}

function applyWorkWeekSettings(settings) {
    document.getElementById("saturday-working").checked = settings.saturday_is_working_day !== false;
    document.getElementById("saturday-start").value = (settings.saturday_start_time || "11:00:00").slice(0, 5);
    document.getElementById("saturday-end").value = (settings.saturday_end_time || "16:00:00").slice(0, 5);
    document.getElementById("saturday-grace").value = settings.saturday_grace_period_minutes ?? 15;
    document.getElementById("saturday-late").value = settings.saturday_late_after_minutes ?? 30;
    document.getElementById("sunday-working").checked = !!settings.sunday_is_working_day;
}

function setupSettingsForms() {
    // Save ZK Device connection
    document.getElementById("settings-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        
        const payload = getFullSettingsPayload();
        
        try {
            const response = await apiFetch(`${API_BASE}/api/settings`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            if (!response.ok) throw new Error("Could not save settings");
            
            const updated = await response.json();
            alert("Hardware configuration saved successfully.");
            updateConnectionStatus(updated.last_sync_status === "Success" ? "CONNECTED" : "DISCONNECTED", updated.last_sync_time);
            
            // Reload countdown and configs
            loadDashboardData();
            
        } catch (error) {
            alert(`Error saving configurations: ${error.message}`);
        }
    });

    document.getElementById("work-week-form").addEventListener("submit", async (e) => {
        e.preventDefault();

        const payload = getFullSettingsPayload();

        try {
            const response = await apiFetch(`${API_BASE}/api/settings`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            if (!response.ok) throw new Error("Could not save work week policy");

            await response.json();
            alert("Work week policy saved. Attendance for this month will be recalculated.");
            loadDashboardData();
        } catch (error) {
            alert(`Error saving work week policy: ${error.message}`);
        }
    });
    
    // Save Shift Config
    document.getElementById("shift-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        
        const id = document.getElementById("shift-id").value;
        const name = document.getElementById("shift-name").value;
        const start = document.getElementById("shift-start").value;
        const end = document.getElementById("shift-end").value;
        const grace = parseInt(document.getElementById("shift-grace").value);
        const late = parseInt(document.getElementById("shift-late").value);
        
        // API requires HH:MM:SS format
        const formattedStart = start.length === 5 ? `${start}:00` : start;
        const formattedEnd = end.length === 5 ? `${end}:00` : end;
        
        const payload = {
            name: name,
            start_time: formattedStart,
            end_time: formattedEnd,
            grace_period_minutes: grace,
            late_after_minutes: late
        };
        
        try {
            let response;
            if (id) {
                // Update existing shift
                response = await apiFetch(`${API_BASE}/api/shifts/${id}`, {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });
            } else {
                // Create new shift
                response = await apiFetch(`${API_BASE}/api/shifts`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });
            }
            
            if (!response.ok) {
                const errData = await response.json().catch(() => ({}));
                const detail = errData.detail || response.statusText || "Could not save shift config";
                throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
            }
            
            alert(id ? "Shift updated successfully!" : "New Shift created successfully!");
            resetShiftForm();
            loadAllShifts();
            
        } catch (error) {
            alert(`Error: ${error.message}`);
        }
    });
    
    // Cancel shift edit
    document.getElementById("btn-cancel-shift").addEventListener("click", () => {
        resetShiftForm();
    });
}

function resetShiftForm() {
    document.getElementById("shift-id").value = "";
    document.getElementById("shift-name").value = "";
    document.getElementById("shift-start").value = "";
    document.getElementById("shift-end").value = "";
    document.getElementById("shift-grace").value = 15;
    document.getElementById("shift-late").value = 30;
    
    document.getElementById("btn-save-shift").textContent = "Add Shift Configuration";
    document.getElementById("btn-cancel-shift").style.display = "none";
}

async function loadSettings() {
    try {
        const response = await apiFetch(`${API_BASE}/api/settings`);
        if (!response.ok) throw new Error("Could not load hardware settings");
        const settings = await response.json();
        
        document.getElementById("device-ip").value = settings.ip_address;
        document.getElementById("device-port").value = settings.port;
        document.getElementById("device-key").value = settings.comm_key;
        document.getElementById("sync-interval").value = settings.sync_interval_minutes;
        applyWorkWeekSettings(settings);
        
        // Update connection bar IP
        connectionIp.textContent = `${settings.ip_address}:${settings.port}`;
        
    } catch (error) {
        console.error(error);
    }
}

async function fetchAllShifts() {
    const response = await apiFetch(`${API_BASE}/api/shifts`);
    if (!response.ok) throw new Error("Could not load company shifts");
    allShifts = await response.json();
    return allShifts;
}

async function loadAllShifts() {
    const listEl = document.getElementById("shifts-list");
    if (listEl) listEl.innerHTML = "<p>Loading shifts...</p>";

    try {
        await fetchAllShifts();

        if (!listEl) return;

        listEl.innerHTML = "";

        if (allShifts.length === 0) {
            listEl.innerHTML = `<p class="page-subtitle">No custom shifts configured yet.</p>`;
            return;
        }

        allShifts.forEach(shift => {
            const item = document.createElement("div");
            item.className = "shift-item";
            item.innerHTML = `
                <div class="shift-info-left">
                    <span class="shift-item-name">${shift.name}</span>
                    <span class="shift-item-times">
                        ${shift.start_time.substring(0, 5)} - ${shift.end_time.substring(0, 5)} 
                        (Grace: ${shift.grace_period_minutes}m, Absent limit: ${shift.late_after_minutes}m)
                    </span>
                </div>
                <button class="btn btn-secondary" style="padding: 0.35rem 0.75rem; font-size:0.75rem;" onclick="editShift(${shift.id}, '${shift.name}', '${shift.start_time}', '${shift.end_time}', ${shift.grace_period_minutes}, ${shift.late_after_minutes})">
                    Modify Time
                </button>
            `;
            listEl.appendChild(item);
        });
        
    } catch (error) {
        console.error("Shifts fetch error:", error);
        listEl.innerHTML = `<p class="text-glow-red">Error: ${error.message}</p>`;
    }
}

window.editShift = function(id, name, start, end, grace, late) {
    document.getElementById("shift-id").value = id;
    document.getElementById("shift-name").value = name;
    document.getElementById("shift-start").value = start.substring(0, 5);
    document.getElementById("shift-end").value = end.substring(0, 5);
    document.getElementById("shift-grace").value = grace;
    document.getElementById("shift-late").value = late;
    
    document.getElementById("btn-save-shift").textContent = "Save Changes";
    document.getElementById("btn-cancel-shift").style.display = "inline-flex";
};

// 8. Department Management
function populateDepartmentSelect(selectId, includeBlank = false, selectedId = null, blankLabel = "Unassigned") {
    const select = document.getElementById(selectId);
    if (!select) return;
    const current = select.value;
    select.innerHTML = includeBlank ? `<option value="">${blankLabel}</option>` : "";
    allDepartments.forEach(dept => {
        const option = document.createElement("option");
        option.value = dept.id;
        option.textContent = dept.name;
        if (selectedId && dept.id === selectedId) option.selected = true;
        select.appendChild(option);
    });
    if (!selectedId && current) select.value = current;
}

async function loadAllDepartments() {
    try {
        const response = await apiFetch(`${API_BASE}/api/departments`);
        if (!response.ok) throw new Error("Could not load departments");
        allDepartments = await response.json();
        populateDepartmentSelect("filter-department", true, null, "All Departments");
        populateDepartmentSelect("leave-filter-dept", true, null, "All Departments");
    } catch (error) {
        console.error("Departments fetch error:", error);
    }
}

function setupDepartmentForm() {
    document.getElementById("department-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const id = document.getElementById("dept-id").value;
        const payload = {
            name: document.getElementById("dept-name").value,
            description: document.getElementById("dept-description").value || null,
            is_active: true
        };
        try {
            const response = await apiFetch(
                id ? `${API_BASE}/api/departments/${id}` : `${API_BASE}/api/departments`,
                {
                    method: id ? "PUT" : "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                }
            );
            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || "Could not save department");
            }
            resetDepartmentForm();
            await loadAllDepartments();
            loadDepartments();
        } catch (error) {
            alert(`Error: ${error.message}`);
        }
    });

    document.getElementById("btn-cancel-dept").addEventListener("click", resetDepartmentForm);
}

function resetDepartmentForm() {
    document.getElementById("dept-id").value = "";
    document.getElementById("dept-name").value = "";
    document.getElementById("dept-description").value = "";
    document.getElementById("btn-save-dept").textContent = "Add Department";
    document.getElementById("btn-cancel-dept").style.display = "none";
}

async function loadDepartments() {
    const listEl = document.getElementById("departments-list");
    listEl.innerHTML = "<p>Loading departments...</p>";
    await loadAllDepartments();
    listEl.innerHTML = "";
    if (allDepartments.length === 0) {
        listEl.innerHTML = `<p class="page-subtitle">No departments created yet.</p>`;
        return;
    }
    allDepartments.forEach(dept => {
        const item = document.createElement("div");
        item.className = "shift-item";
        item.innerHTML = `
            <div class="shift-info-left">
                <span class="shift-item-name">${dept.name}</span>
                <span class="shift-item-times">${dept.description || "No description"} · ${dept.employee_count} employee(s)</span>
            </div>
            <div style="display:flex; gap:0.5rem;">
                <button class="btn btn-secondary" style="padding:0.35rem 0.75rem; font-size:0.75rem;" onclick="editDepartment(${dept.id}, '${dept.name.replace(/'/g, "\\'")}', '${(dept.description || "").replace(/'/g, "\\'")}')">Edit</button>
                <button class="btn btn-secondary" style="padding:0.35rem 0.75rem; font-size:0.75rem;" onclick="deleteDepartment(${dept.id}, '${dept.name.replace(/'/g, "\\'")}')">Delete</button>
            </div>
        `;
        listEl.appendChild(item);
    });
}

window.editDepartment = function(id, name, description) {
    document.getElementById("dept-id").value = id;
    document.getElementById("dept-name").value = name;
    document.getElementById("dept-description").value = description;
    document.getElementById("btn-save-dept").textContent = "Save Changes";
    document.getElementById("btn-cancel-dept").style.display = "inline-flex";
};

window.deleteDepartment = async function(id, name) {
    if (!confirm(`Delete department "${name}"?`)) return;
    try {
        const response = await apiFetch(`${API_BASE}/api/departments/${id}`, { method: "DELETE" });
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || "Could not delete department");
        }
        await loadAllDepartments();
        loadDepartments();
    } catch (error) {
        alert(`Error: ${error.message}`);
    }
};

// 9. Leave Management
async function loadLeaveTypes() {
    try {
        const response = await apiFetch(`${API_BASE}/api/leave-types`);
        if (!response.ok) throw new Error("Could not load leave types");
        allLeaveTypes = await response.json();
        const select = document.getElementById("leave-type");
        if (select) {
            select.innerHTML = "";
            allLeaveTypes.forEach(lt => {
                const option = document.createElement("option");
                option.value = lt.id;
                option.textContent = lt.name;
                select.appendChild(option);
            });
        }
    } catch (error) {
        console.error("Leave types error:", error);
    }
}

async function loadLeaveFormOptions() {
    await loadLeaveTypes();
    await loadAllDepartments();
    try {
        const response = await apiFetch(`${API_BASE}/api/employees`);
        if (!response.ok) throw new Error("Could not load employees");
        allEmployeesCache = await response.json();
        const select = document.getElementById("leave-employee");
        select.innerHTML = "";
        allEmployeesCache.filter(e => e.is_active).forEach(emp => {
            const option = document.createElement("option");
            option.value = emp.id;
            const dept = emp.department ? emp.department.name : "No Dept";
            option.textContent = `${emp.name} (${emp.user_id}) — ${dept}`;
            select.appendChild(option);
        });
    } catch (error) {
        console.error("Employees for leave form:", error);
    }
    populateDepartmentSelect("leave-filter-dept", true, null, "All Departments");
}

function setupLeaveForm() {
    const halfDayToggle = document.getElementById("leave-half-day");
    halfDayToggle.addEventListener("change", () => {
        const group = document.getElementById("half-day-period-group");
        group.style.display = halfDayToggle.checked ? "block" : "none";
        if (halfDayToggle.checked) {
            const start = document.getElementById("leave-start").value;
            document.getElementById("leave-end").value = start;
        }
    });

    document.getElementById("leave-start").addEventListener("change", () => {
        if (halfDayToggle.checked) {
            document.getElementById("leave-end").value = document.getElementById("leave-start").value;
        }
    });

    document.getElementById("leave-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const id = document.getElementById("leave-id").value;
        const isHalfDay = document.getElementById("leave-half-day").checked;
        const payload = {
            employee_id: parseInt(document.getElementById("leave-employee").value),
            leave_type_id: parseInt(document.getElementById("leave-type").value),
            start_date: document.getElementById("leave-start").value,
            end_date: isHalfDay ? document.getElementById("leave-start").value : document.getElementById("leave-end").value,
            is_half_day: isHalfDay,
            half_day_period: isHalfDay ? document.getElementById("leave-half-period").value : null,
            reason: document.getElementById("leave-reason").value || null,
            application_received: document.getElementById("leave-app-received").checked,
            status: document.getElementById("leave-status").value,
            recorded_by: document.getElementById("leave-recorded-by").value || null,
            notes: document.getElementById("leave-notes").value || null
        };
        try {
            const response = await apiFetch(
                id ? `${API_BASE}/api/leaves/${id}` : `${API_BASE}/api/leaves`,
                {
                    method: id ? "PUT" : "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                }
            );
            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || "Could not save leave record");
            }
            resetLeaveForm();
            loadLeaves();
            if (currentTab === "dashboard") loadDashboardData();
            if (currentTab === "attendance") loadAttendanceLogs();
            alert(id ? "Leave record updated!" : "Leave record submitted!");
        } catch (error) {
            alert(`Error: ${error.message}`);
        }
    });

    document.getElementById("btn-cancel-leave").addEventListener("click", resetLeaveForm);
    document.getElementById("btn-refresh-leaves").addEventListener("click", loadLeaves);
}

function resetLeaveForm() {
    document.getElementById("leave-id").value = "";
    document.getElementById("leave-reason").value = "";
    document.getElementById("leave-recorded-by").value = "";
    document.getElementById("leave-notes").value = "";
    document.getElementById("leave-half-day").checked = false;
    document.getElementById("leave-app-received").checked = false;
    document.getElementById("leave-status").value = "Pending";
    document.getElementById("half-day-period-group").style.display = "none";
    document.getElementById("btn-save-leave").textContent = "Submit Leave Record";
    document.getElementById("btn-cancel-leave").style.display = "none";
    const today = new Date().toISOString().split("T")[0];
    document.getElementById("leave-start").value = today;
    document.getElementById("leave-end").value = today;
}

async function loadLeaves() {
    const tableBody = document.getElementById("leaves-table-body");
    tableBody.innerHTML = `<tr><td colspan="9" class="text-center">Loading leave records...</td></tr>`;

    const status = document.getElementById("leave-filter-status").value;
    const deptId = document.getElementById("leave-filter-dept").value;
    let url = `${API_BASE}/api/leaves?`;
    if (status) url += `status=${status}&`;
    if (deptId) url += `department_id=${deptId}&`;

    try {
        const response = await apiFetch(url);
        if (!response.ok) throw new Error("Could not load leave records");
        const leaves = await response.json();
        tableBody.innerHTML = "";
        if (leaves.length === 0) {
            tableBody.innerHTML = `<tr><td colspan="9" class="text-center">No leave records found.</td></tr>`;
            return;
        }
        leavesCache = leaves;
        leaves.forEach(leave => {
            const dateRange = leave.is_half_day
                ? `${leave.start_date} (${leave.half_day_period || "Half"})`
                : leave.start_date === leave.end_date
                    ? leave.start_date
                    : `${leave.start_date} → ${leave.end_date}`;

            let statusClass = "bg-orange-badge";
            if (leave.status === "Approved") statusClass = "bg-green-badge";
            if (leave.status === "Rejected") statusClass = "bg-red-badge";

            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td><strong>${leave.employee_name}</strong><br><small class="page-subtitle">${leave.employee_user_id}</small></td>
                <td>${leave.department_name || "-"}</td>
                <td>${leave.leave_type_name}</td>
                <td>${dateRange}</td>
                <td><small>${leave.reason || "-"}</small></td>
                <td>${leave.application_received ? "✓ Yes" : "✗ No"}</td>
                <td><span class="badge ${statusClass}">${leave.status}</span></td>
                <td><small>${leave.recorded_by || "-"}</small></td>
                <td>
                    <button class="btn btn-secondary" style="padding:0.3rem 0.6rem; font-size:0.7rem;" onclick="editLeaveById(${leave.id})">Edit</button>
                    <button class="btn btn-secondary" style="padding:0.3rem 0.6rem; font-size:0.7rem;" onclick="deleteLeave(${leave.id})">Delete</button>
                </td>
            `;
            tableBody.appendChild(tr);
        });
    } catch (error) {
        tableBody.innerHTML = `<tr><td colspan="9" class="text-center">Error: ${error.message}</td></tr>`;
    }
}

window.editLeaveById = function(id) {
    const leave = leavesCache.find(l => l.id === id);
    if (!leave) return;
    document.getElementById("leave-id").value = leave.id;
    document.getElementById("leave-employee").value = leave.employee_id;
    document.getElementById("leave-type").value = leave.leave_type_id;
    document.getElementById("leave-start").value = leave.start_date;
    document.getElementById("leave-end").value = leave.end_date;
    document.getElementById("leave-status").value = leave.status;
    document.getElementById("leave-half-day").checked = leave.is_half_day;
    document.getElementById("leave-half-period").value = leave.half_day_period || "AM";
    document.getElementById("half-day-period-group").style.display = leave.is_half_day ? "block" : "none";
    document.getElementById("leave-reason").value = leave.reason || "";
    document.getElementById("leave-app-received").checked = leave.application_received;
    document.getElementById("leave-recorded-by").value = leave.recorded_by || "";
    document.getElementById("leave-notes").value = leave.notes || "";
    document.getElementById("btn-save-leave").textContent = "Update Leave Record";
    document.getElementById("btn-cancel-leave").style.display = "inline-flex";
    window.scrollTo({ top: 0, behavior: "smooth" });
};

window.deleteLeave = async function(id) {
    if (!confirm("Delete this leave record? Attendance will be recalculated.")) return;
    try {
        const response = await apiFetch(`${API_BASE}/api/leaves/${id}`, { method: "DELETE" });
        if (!response.ok) throw new Error("Could not delete leave record");
        loadLeaves();
        if (currentTab === "dashboard") loadDashboardData();
        if (currentTab === "attendance") loadAttendanceLogs();
    } catch (error) {
        alert(`Error: ${error.message}`);
    }
};
