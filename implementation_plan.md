# Reporting System, Excel Export & Performance Optimization

This plan details the implementation of a high-performance reporting system, an Excel exporter, and database N+1 query optimizations to make dashboard calculation instant. It also explains the absentee calculation logic.

## Absentee Count Logic Explanation

Currently, absentees are calculated and counted on the dashboard using a two-step process:

1. **Daily calculation (`SyncService.process_daily_attendance`)**:
   * For each active employee on a given workday (Monday to Friday), the backend checks if there are any raw punches (`AttendanceLog` rows) for that date.
   * If there are **no punches** and the date is in the past (or it is today and the shift start time + late threshold limit has passed), the employee is marked as `"Absent"`.
   * An entry is inserted/updated in the `DailyAttendance` table with `status = "Absent"` and `work_hours = 0.0`.
2. **Dashboard aggregation (`/api/dashboard`)**:
   * The dashboard endpoint queries the `DailyAttendance` table for the current date (`date = today`).
   * It aggregates today's records: any row matching today's date with a status of `"Absent"` increments the `absent_today` counter shown on the UI dashboard card.

---

## User Review Required

> [!NOTE]
> **New Dependency**: The Excel export functionality requires installing the standard `openpyxl` library inside the python virtual environment. We will execute the installation command using the terminal.

---

## Proposed Changes

We will refactor the backend and frontend to support fast bulk queries, default date filtering to the current month, and introduce the Excel exporter.

### Backend Refactoring

#### [MODIFY] [sync_service.py](file:///c:/attendance_management/sync_service.py)
* Refactor `SyncService.process_daily_attendance` to eliminate the N+1 query pattern.
  * Load all active employees in a single query.
  * Fetch raw punches for all active employees for the date range (defaults to the current month) in one query, then group them in-memory.
  * Fetch existing daily attendance records for the date range in one query.
  * Execute calculations entirely in-memory and write updates back in a single transaction block.

#### [MODIFY] [routes.py](file:///c:/attendance_management/routes.py)
* **Default Filters in `/api/attendance`**: If no `start_date` or `end_date` query parameters are provided, default to the start and end of the current month.
* **Optimize Dashboard Trend Endpoint**: Load weekly trend attendance records for the last 7 days in a single bulk query rather than executing separate queries in a loop.
* **[NEW] Add Excel Export Endpoint `/api/attendance/export`**:
  * Take the same query parameters (`start_date`, `end_date`, `status`, `search`).
  * Query the daily attendance table.
  * Assemble an Excel file (`.xlsx`) using `openpyxl` in-memory.
  * Stream it as a file download response (`StreamingResponse`).

---

### Front-End Interface Modifications

#### [MODIFY] [index.html](file:///c:/attendance_management/static/index.html)
* Add an "Export Excel" action button inside the filter actions panel of the **Attendance Log** tab pane.

#### [MODIFY] [style.css](file:///c:/attendance_management/static/css/style.css)
* Add `.btn-success` styles (green background/glow gradient) for the Excel export button.

#### [MODIFY] [app.js](file:///c:/attendance_management/static/js/app.js)
* **Default Date Range Filters**: Initialize the Start Date and End Date filters on the Attendance Log tab to default to the current month (1st of the month to today).
* **Excel Export Trigger**: Bind a click handler to the new Export Excel button that compiles the filter values and redirects the browser to the `/api/attendance/export` endpoint to trigger the native file download.

## Verification Plan

### Automated Verification
* Run `venv\Scripts\python.exe test_calc.py` to ensure core shift and status calculations remain correct.

### Manual Verification
1. Run server `python main.py` and open the app in a browser.
2. Load the Dashboard and verify connection status and trends render instantly.
3. Open the Attendance Log page and verify:
   * Dates are pre-populated with the current month.
   * Applying filters is immediate.
   * Clicking "Export Excel" downloads a valid spreadsheet matching the screen rows.
