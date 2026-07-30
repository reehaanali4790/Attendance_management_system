from pydantic import BaseModel, Field
from typing import Optional, List
import datetime

# --- Device Settings ---
class DeviceSettingsBase(BaseModel):
    ip_address: str
    port: int
    comm_key: int
    sync_interval_minutes: int
    saturday_is_working_day: bool = True
    saturday_start_time: datetime.time = datetime.time(11, 0)
    saturday_end_time: datetime.time = datetime.time(16, 0)
    saturday_grace_period_minutes: int = 15
    saturday_late_after_minutes: int = 30
    sunday_is_working_day: bool = False

class DeviceSettingsUpdate(DeviceSettingsBase):
    pass

class DeviceSettingsResponse(DeviceSettingsBase):
    id: int
    last_sync_time: Optional[datetime.datetime] = None
    last_sync_status: Optional[str] = None

    class Config:
        from_attributes = True

# --- Department ---
class DepartmentBase(BaseModel):
    name: str
    description: Optional[str] = None
    is_active: bool = True

class DepartmentCreate(DepartmentBase):
    pass

class DepartmentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None

class DepartmentResponse(DepartmentBase):
    id: int
    created_at: Optional[datetime.datetime] = None
    employee_count: int = 0

    class Config:
        from_attributes = True

# --- Shift ---
class ShiftBase(BaseModel):
    name: str
    start_time: datetime.time
    end_time: datetime.time
    grace_period_minutes: int
    late_after_minutes: int

class ShiftCreate(ShiftBase):
    pass

class ShiftUpdate(ShiftBase):
    pass

class ShiftResponse(ShiftBase):
    id: int

    class Config:
        from_attributes = True

# --- Employee ---
class EmployeeBase(BaseModel):
    user_id: str
    name: str
    privilege: int
    card_number: Optional[str] = None
    is_active: bool
    shift_id: Optional[int] = None
    department_id: Optional[int] = None

class EmployeeUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None
    shift_id: Optional[int] = None
    department_id: Optional[int] = None

class EmployeeResponse(EmployeeBase):
    id: int
    shift: Optional[ShiftResponse] = None
    department: Optional[DepartmentResponse] = None

    class Config:
        from_attributes = True

# --- Leave Type ---
class LeaveTypeBase(BaseModel):
    name: str
    description: Optional[str] = None
    is_paid: bool = True
    requires_application: bool = True

class LeaveTypeCreate(LeaveTypeBase):
    pass

class LeaveTypeResponse(LeaveTypeBase):
    id: int

    class Config:
        from_attributes = True

# --- Leave Request ---
class LeaveRequestBase(BaseModel):
    employee_id: int
    leave_type_id: int
    start_date: datetime.date
    end_date: datetime.date
    is_half_day: bool = False
    half_day_period: Optional[str] = None
    reason: Optional[str] = None
    application_received: bool = False
    recorded_by: Optional[str] = None
    notes: Optional[str] = None

class LeaveRequestCreate(LeaveRequestBase):
    status: str = "Pending"

class LeaveRequestUpdate(BaseModel):
    leave_type_id: Optional[int] = None
    start_date: Optional[datetime.date] = None
    end_date: Optional[datetime.date] = None
    is_half_day: Optional[bool] = None
    half_day_period: Optional[str] = None
    reason: Optional[str] = None
    application_received: Optional[bool] = None
    status: Optional[str] = None
    recorded_by: Optional[str] = None
    notes: Optional[str] = None

class LeaveRequestResponse(LeaveRequestBase):
    id: int
    status: str
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None
    employee_name: str = ""
    employee_user_id: str = ""
    leave_type_name: str = ""
    department_name: Optional[str] = None

    class Config:
        from_attributes = True

# --- Raw Attendance Log ---
class AttendanceLogResponse(BaseModel):
    id: int
    user_id: str
    timestamp: datetime.datetime
    punch_type: str
    status_code: int
    imported_at: datetime.datetime

    class Config:
        from_attributes = True

# --- Processed Daily Attendance ---
class DailyAttendanceResponse(BaseModel):
    id: int
    employee_id: int
    employee_name: str = ""
    employee_user_id: str = ""
    department_name: Optional[str] = None
    date: datetime.date
    check_in: Optional[datetime.datetime] = None
    check_out: Optional[datetime.datetime] = None
    work_hours: float
    status: str
    late_minutes: int
    early_leave_minutes: int
    remarks: Optional[str] = None

    class Config:
        from_attributes = True

# --- Dashboard Metrics ---
class RecentPunch(BaseModel):
    user_id: str
    employee_name: str
    timestamp: datetime.datetime
    punch_type: str

class DashboardSummary(BaseModel):
    total_employees: int
    present_today: int
    late_today: int
    absent_today: int
    left_early_today: int
    on_leave_today: int = 0
    half_day_today: int = 0
    avg_work_hours_today: float
    connection_status: str
    last_sync_time: Optional[datetime.datetime] = None
    next_sync_in_seconds: int
    recent_punches: List[RecentPunch]
    weekly_trend: dict
    department_stats: Optional[List[dict]] = None


# --- Company Holidays ---
class CompanyHolidayCreate(BaseModel):
    holiday_date: datetime.date
    name: Optional[str] = None


class CompanyHolidayResponse(BaseModel):
    id: int
    holiday_date: datetime.date
    name: Optional[str] = None

    class Config:
        from_attributes = True


# --- Authentication ---
class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: int
    username: str
    full_name: Optional[str] = None
    is_active: bool

    class Config:
        from_attributes = True
