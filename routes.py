from fastapi import APIRouter, Depends, HTTPException, Query, File, UploadFile, WebSocket, Form, BackgroundTasks

from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, case
from pydantic import BaseModel
from database import get_db
import models
import schemas
from sync_service import SyncService, schedule_attendance_recalc, schedule_recent_attendance_recalc, is_working_day
from auth import get_current_user
import datetime
from typing import List, Optional
import json
import ai_service

router = APIRouter(dependencies=[Depends(get_current_user)])

VALID_LEAVE_STATUSES = {"Pending", "Approved", "Rejected"}


def _department_response(dept: models.Department, employee_count: int = 0) -> schemas.DepartmentResponse:
    return schemas.DepartmentResponse(
        id=dept.id,
        name=dept.name,
        description=dept.description,
        is_active=dept.is_active,
        created_at=dept.created_at,
        employee_count=employee_count
    )


def _leave_response(lr: models.LeaveRequest) -> schemas.LeaveRequestResponse:
    return schemas.LeaveRequestResponse(
        id=lr.id,
        employee_id=lr.employee_id,
        leave_type_id=lr.leave_type_id,
        start_date=lr.start_date,
        end_date=lr.end_date,
        is_half_day=lr.is_half_day,
        half_day_period=lr.half_day_period,
        reason=lr.reason,
        application_received=lr.application_received,
        status=lr.status,
        recorded_by=lr.recorded_by,
        notes=lr.notes,
        created_at=lr.created_at,
        updated_at=lr.updated_at,
        employee_name=lr.employee.name if lr.employee else "",
        employee_user_id=lr.employee.user_id if lr.employee else "",
        leave_type_name=lr.leave_type.name if lr.leave_type else "",
        department_name=lr.employee.department.name if lr.employee and lr.employee.department else None
    )


def _attendance_response(r: models.DailyAttendance) -> schemas.DailyAttendanceResponse:
    return schemas.DailyAttendanceResponse(
        id=r.id,
        employee_id=r.employee_id,
        employee_name=r.employee.name,
        employee_user_id=r.employee.user_id,
        department_name=r.employee.department.name if r.employee.department else None,
        date=r.date,
        check_in=r.check_in,
        check_out=r.check_out,
        work_hours=r.work_hours,
        status=r.status,
        late_minutes=r.late_minutes,
        early_leave_minutes=r.early_leave_minutes,
        remarks=r.remarks
    )


@router.get("/api/dashboard", response_model=schemas.DashboardSummary)
def get_dashboard(db: Session = Depends(get_db)):
    settings = db.query(models.DeviceSettings).first()
    if not settings:
        settings, _ = SyncService.initialize_defaults(db)

    connection_status = "DISCONNECTED"
    if settings.last_sync_status == "Success":
        connection_status = "CONNECTED"
    elif settings.last_sync_status and "Failed" in settings.last_sync_status:
        connection_status = "DISCONNECTED"

    next_sync_seconds = 300
    if settings.last_sync_time:
        elapsed = (datetime.datetime.now() - settings.last_sync_time).total_seconds()
        remaining = (settings.sync_interval_minutes * 60) - elapsed
        next_sync_seconds = max(0, int(remaining))

    today = datetime.date.today()
    working_statuses = ("Present", "Late", "Left Early")

    total_employees = db.query(models.Employee).filter_by(is_active=True).count()

    status_rows = db.query(
        models.DailyAttendance.status,
        func.count(models.DailyAttendance.id),
    ).filter(
        models.DailyAttendance.date == today
    ).group_by(models.DailyAttendance.status).all()

    present_today = 0
    late_today = 0
    left_early_today = 0
    absent_today = 0
    on_leave_today = 0
    half_day_today = 0
    for status, cnt in status_rows:
        if status == "Absent":
            absent_today = cnt
        elif status == "On Leave":
            on_leave_today = cnt
        elif status == "Half Day":
            half_day_today = cnt
        elif status in working_statuses:
            present_today += cnt
            if status == "Late":
                late_today = cnt
            elif status == "Left Early":
                left_early_today = cnt

    avg_hours = db.query(func.avg(models.DailyAttendance.work_hours)).filter(
        models.DailyAttendance.date == today,
        models.DailyAttendance.status.in_(working_statuses),
        models.DailyAttendance.work_hours > 0,
    ).scalar()
    avg_hours = round(float(avg_hours), 2) if avg_hours else 0.0

    recent_logs = (
        db.query(models.AttendanceLog, models.Employee.name)
        .outerjoin(models.Employee, models.Employee.user_id == models.AttendanceLog.user_id)
        .order_by(models.AttendanceLog.timestamp.desc())
        .limit(10)
        .all()
    )
    recent_punches = [
        schemas.RecentPunch(
            user_id=log.user_id,
            employee_name=name or "Unknown User",
            timestamp=log.timestamp,
            punch_type=log.punch_type,
        )
        for log, name in recent_logs
    ]

    start_trend_date = today - datetime.timedelta(days=7)
    trend_rows = db.query(
        models.DailyAttendance.date,
        models.DailyAttendance.status,
        func.count(models.DailyAttendance.id),
    ).filter(
        models.DailyAttendance.date >= start_trend_date,
        models.DailyAttendance.date <= today,
    ).group_by(
        models.DailyAttendance.date,
        models.DailyAttendance.status,
    ).all()

    trend_by_date = {}
    for day, status, cnt in trend_rows:
        d_str = day.strftime("%Y-%m-%d")
        bucket = trend_by_date.setdefault(d_str, {"present": 0, "late": 0, "absent": 0, "on_leave": 0})
        if status in working_statuses:
            bucket["present"] += cnt
        if status == "Late":
            bucket["late"] += cnt
        if status == "Absent":
            bucket["absent"] += cnt
        if status == "On Leave":
            bucket["on_leave"] += cnt

    weekly_trend = {}
    for day_offset in range(7, -1, -1):
        d = today - datetime.timedelta(days=day_offset)
        if not is_working_day(d, settings):
            continue
        d_str = d.strftime("%Y-%m-%d")
        weekly_trend[d_str] = trend_by_date.get(
            d_str, {"present": 0, "late": 0, "absent": 0, "on_leave": 0}
        )

    dept_rows = db.query(
        models.Department.name.label("department_name"),
        func.count(func.distinct(models.Employee.id)).label("total_employees"),
        func.sum(case((models.DailyAttendance.status.in_(working_statuses), 1), else_=0)).label("present"),
        func.sum(case((models.DailyAttendance.status == "Absent", 1), else_=0)).label("absent"),
        func.sum(case((models.DailyAttendance.status == "On Leave", 1), else_=0)).label("on_leave"),
    ).join(
        models.Employee, models.Employee.department_id == models.Department.id
    ).outerjoin(
        models.DailyAttendance,
        (models.DailyAttendance.employee_id == models.Employee.id)
        & (models.DailyAttendance.date == today),
    ).filter(
        models.Department.is_active.is_(True),
        models.Employee.is_active.is_(True),
    ).group_by(models.Department.name).all()

    dept_stats = [
        {
            "department_name": row.department_name,
            "total_employees": int(row.total_employees or 0),
            "present": int(row.present or 0),
            "absent": int(row.absent or 0),
            "on_leave": int(row.on_leave or 0),
        }
        for row in dept_rows
    ]

    return schemas.DashboardSummary(
        total_employees=total_employees,
        present_today=present_today,
        late_today=late_today,
        absent_today=absent_today,
        left_early_today=left_early_today,
        on_leave_today=on_leave_today,
        half_day_today=half_day_today,
        avg_work_hours_today=avg_hours,
        connection_status=connection_status,
        last_sync_time=settings.last_sync_time,
        next_sync_in_seconds=next_sync_seconds,
        recent_punches=recent_punches,
        weekly_trend=weekly_trend,
        department_stats=dept_stats
    )


@router.get("/api/attendance", response_model=List[schemas.DailyAttendanceResponse])
def get_attendance(
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    department_id: Optional[int] = None,
    recalculate: bool = False,
    db: Session = Depends(get_db)
):
    if not start_date:
        start_date = datetime.date.today().replace(day=1)
    if not end_date:
        end_date = datetime.date.today()

    if recalculate:
        schedule_attendance_recalc(start_date, end_date)

    query = db.query(models.DailyAttendance).options(
        joinedload(models.DailyAttendance.employee).joinedload(models.Employee.department)
    ).join(models.Employee)
    query = query.filter(models.DailyAttendance.date >= start_date)
    query = query.filter(models.DailyAttendance.date <= end_date)
    if status:
        query = query.filter(models.DailyAttendance.status == status)
    if search:
        query = query.filter(
            (models.Employee.name.like(f"%{search}%")) |
            (models.Employee.user_id == search)
        )
    if department_id:
        query = query.filter(models.Employee.department_id == department_id)

    records = query.order_by(models.DailyAttendance.date.desc(), models.Employee.name).all()
    return [_attendance_response(r) for r in records]


@router.get("/api/employees", response_model=List[schemas.EmployeeResponse])
def get_employees(
    department_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    query = db.query(models.Employee).options(
        joinedload(models.Employee.shift),
        joinedload(models.Employee.department)
    )
    if department_id:
        query = query.filter(models.Employee.department_id == department_id)
    return query.all()


@router.put("/api/employees/{emp_id}", response_model=schemas.EmployeeResponse)
def update_employee(emp_id: int, payload: schemas.EmployeeUpdate, db: Session = Depends(get_db)):
    emp = db.query(models.Employee).filter_by(id=emp_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if payload.name is not None:
        emp.name = payload.name
    if payload.is_active is not None:
        emp.is_active = payload.is_active
    if payload.shift_id is not None:
        if payload.shift_id == 0:
            emp.shift_id = None
        else:
            shift = db.query(models.Shift).filter_by(id=payload.shift_id).first()
            if not shift:
                raise HTTPException(status_code=400, detail="Shift not found")
            emp.shift_id = payload.shift_id
    if payload.department_id is not None:
        if payload.department_id == 0:
            emp.department_id = None
        else:
            dept = db.query(models.Department).filter_by(id=payload.department_id).first()
            if not dept:
                raise HTTPException(status_code=400, detail="Department not found")
            emp.department_id = payload.department_id

    db.commit()

    updated = db.query(models.Employee).options(
        joinedload(models.Employee.shift),
        joinedload(models.Employee.department),
    ).filter_by(id=emp_id).first()
    return updated


# --- Departments ---
@router.get("/api/departments", response_model=List[schemas.DepartmentResponse])
def get_departments(db: Session = Depends(get_db)):
    departments = db.query(models.Department).order_by(models.Department.name).all()
    count_rows = db.query(
        models.Employee.department_id,
        func.count(models.Employee.id),
    ).filter(
        models.Employee.is_active.is_(True),
        models.Employee.department_id.isnot(None),
    ).group_by(models.Employee.department_id).all()
    employee_counts = {dept_id: cnt for dept_id, cnt in count_rows}
    return [_department_response(d, employee_counts.get(d.id, 0)) for d in departments]


@router.post("/api/departments", response_model=schemas.DepartmentResponse)
def create_department(payload: schemas.DepartmentCreate, db: Session = Depends(get_db)):
    existing = db.query(models.Department).filter_by(name=payload.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Department with this name already exists")
    dept = models.Department(**payload.model_dump())
    db.add(dept)
    db.commit()
    db.refresh(dept)
    return _department_response(dept, 0)


@router.put("/api/departments/{dept_id}", response_model=schemas.DepartmentResponse)
def update_department(dept_id: int, payload: schemas.DepartmentUpdate, db: Session = Depends(get_db)):
    dept = db.query(models.Department).filter_by(id=dept_id).first()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    if payload.name is not None:
        conflict = db.query(models.Department).filter(
            models.Department.name == payload.name,
            models.Department.id != dept_id
        ).first()
        if conflict:
            raise HTTPException(status_code=400, detail="Department name already in use")
        dept.name = payload.name
    if payload.description is not None:
        dept.description = payload.description
    if payload.is_active is not None:
        dept.is_active = payload.is_active
    db.commit()
    db.refresh(dept)
    count = db.query(models.Employee).filter_by(department_id=dept.id, is_active=True).count()
    return _department_response(dept, count)


@router.delete("/api/departments/{dept_id}")
def delete_department(dept_id: int, db: Session = Depends(get_db)):
    dept = db.query(models.Department).filter_by(id=dept_id).first()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    assigned = db.query(models.Employee).filter_by(department_id=dept_id).count()
    if assigned > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete department with {assigned} assigned employee(s). Reassign them first."
        )
    db.delete(dept)
    db.commit()
    return {"status": "deleted"}


# --- Leave Types ---
@router.get("/api/leave-types", response_model=List[schemas.LeaveTypeResponse])
def get_leave_types(db: Session = Depends(get_db)):
    SyncService.initialize_defaults(db)
    return db.query(models.LeaveType).order_by(models.LeaveType.name).all()


@router.post("/api/leave-types", response_model=schemas.LeaveTypeResponse)
def create_leave_type(payload: schemas.LeaveTypeCreate, db: Session = Depends(get_db)):
    existing = db.query(models.LeaveType).filter_by(name=payload.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Leave type already exists")
    lt = models.LeaveType(**payload.model_dump())
    db.add(lt)
    db.commit()
    db.refresh(lt)
    return lt


# --- Leave Requests ---
@router.get("/api/leaves", response_model=List[schemas.LeaveRequestResponse])
def get_leaves(
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
    status: Optional[str] = None,
    employee_id: Optional[int] = None,
    department_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    query = db.query(models.LeaveRequest).options(
        joinedload(models.LeaveRequest.employee).joinedload(models.Employee.department),
        joinedload(models.LeaveRequest.leave_type)
    )
    if start_date:
        query = query.filter(models.LeaveRequest.end_date >= start_date)
    if end_date:
        query = query.filter(models.LeaveRequest.start_date <= end_date)
    if status:
        query = query.filter(models.LeaveRequest.status == status)
    if employee_id:
        query = query.filter(models.LeaveRequest.employee_id == employee_id)
    if department_id:
        query = query.join(models.Employee).filter(models.Employee.department_id == department_id)

    records = query.order_by(models.LeaveRequest.start_date.desc()).all()
    return [_leave_response(lr) for lr in records]


@router.post("/api/leaves", response_model=schemas.LeaveRequestResponse)
def create_leave(payload: schemas.LeaveRequestCreate, db: Session = Depends(get_db)):
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="End date cannot be before start date")
    if payload.status not in VALID_LEAVE_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid leave status")
    if payload.is_half_day and payload.start_date != payload.end_date:
        raise HTTPException(status_code=400, detail="Half-day leave must be a single date")
    if payload.is_half_day and payload.half_day_period not in ("AM", "PM", None):
        raise HTTPException(status_code=400, detail="Half-day period must be AM or PM")

    emp = db.query(models.Employee).filter_by(id=payload.employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    lt = db.query(models.LeaveType).filter_by(id=payload.leave_type_id).first()
    if not lt:
        raise HTTPException(status_code=404, detail="Leave type not found")

    lr = models.LeaveRequest(**payload.model_dump())
    db.add(lr)
    db.commit()
    db.refresh(lr)
    schedule_attendance_recalc(payload.start_date, payload.end_date)

    lr = db.query(models.LeaveRequest).options(
        joinedload(models.LeaveRequest.employee).joinedload(models.Employee.department),
        joinedload(models.LeaveRequest.leave_type)
    ).filter_by(id=lr.id).first()
    return _leave_response(lr)


@router.put("/api/leaves/{leave_id}", response_model=schemas.LeaveRequestResponse)
def update_leave(leave_id: int, payload: schemas.LeaveRequestUpdate, db: Session = Depends(get_db)):
    lr = db.query(models.LeaveRequest).filter_by(id=leave_id).first()
    if not lr:
        raise HTTPException(status_code=404, detail="Leave request not found")

    data = payload.model_dump(exclude_unset=True)
    if "status" in data and data["status"] not in VALID_LEAVE_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid leave status")

    start = data.get("start_date", lr.start_date)
    end = data.get("end_date", lr.end_date)
    if end < start:
        raise HTTPException(status_code=400, detail="End date cannot be before start date")

    is_half = data.get("is_half_day", lr.is_half_day)
    if is_half and start != end:
        raise HTTPException(status_code=400, detail="Half-day leave must be a single date")

    for key, value in data.items():
        setattr(lr, key, value)
    lr.updated_at = datetime.datetime.utcnow()

    db.commit()
    recalc_start = min(lr.start_date, start)
    recalc_end = max(lr.end_date, end)
    schedule_attendance_recalc(recalc_start, recalc_end)

    lr = db.query(models.LeaveRequest).options(
        joinedload(models.LeaveRequest.employee).joinedload(models.Employee.department),
        joinedload(models.LeaveRequest.leave_type)
    ).filter_by(id=leave_id).first()
    return _leave_response(lr)


@router.delete("/api/leaves/{leave_id}")
def delete_leave(leave_id: int, db: Session = Depends(get_db)):
    lr = db.query(models.LeaveRequest).filter_by(id=leave_id).first()
    if not lr:
        raise HTTPException(status_code=404, detail="Leave request not found")
    start, end = lr.start_date, lr.end_date
    db.delete(lr)
    db.commit()
    schedule_attendance_recalc(start, end)
    return {"status": "deleted"}


# --- Shifts ---
@router.get("/api/shifts", response_model=List[schemas.ShiftResponse])
def get_shifts(db: Session = Depends(get_db)):
    return db.query(models.Shift).all()


@router.post("/api/shifts", response_model=schemas.ShiftResponse)
def create_shift(payload: schemas.ShiftCreate, db: Session = Depends(get_db)):
    existing = db.query(models.Shift).filter_by(name=payload.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Shift with this name already exists")
    shift = models.Shift(**payload.model_dump())
    db.add(shift)
    db.commit()
    db.refresh(shift)
    return shift


@router.put("/api/shifts/{shift_id}", response_model=schemas.ShiftResponse)
def update_shift(
    shift_id: int,
    payload: schemas.ShiftUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    shift = db.query(models.Shift).filter_by(id=shift_id).first()
    if not shift:
        raise HTTPException(status_code=404, detail="Shift not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(shift, key, value)
    db.commit()
    db.refresh(shift)
    background_tasks.add_task(schedule_recent_attendance_recalc)
    return shift


@router.get("/api/settings", response_model=schemas.DeviceSettingsResponse)
def get_settings(db: Session = Depends(get_db)):
    settings = db.query(models.DeviceSettings).first()
    if not settings:
        settings, _ = SyncService.initialize_defaults(db)
    return settings


@router.post("/api/settings", response_model=schemas.DeviceSettingsResponse)
def update_settings(payload: schemas.DeviceSettingsUpdate, db: Session = Depends(get_db)):
    settings = db.query(models.DeviceSettings).first()
    if not settings:
        settings = models.DeviceSettings()
        db.add(settings)

    hardware_changed = (
        settings.ip_address != payload.ip_address
        or settings.port != payload.port
        or settings.comm_key != payload.comm_key
        or settings.sync_interval_minutes != payload.sync_interval_minutes
    )
    work_week_changed = (
        settings.saturday_is_working_day != payload.saturday_is_working_day
        or settings.saturday_start_time != payload.saturday_start_time
        or settings.saturday_end_time != payload.saturday_end_time
        or settings.saturday_grace_period_minutes != payload.saturday_grace_period_minutes
        or settings.saturday_late_after_minutes != payload.saturday_late_after_minutes
        or settings.sunday_is_working_day != payload.sunday_is_working_day
    )

    settings.ip_address = payload.ip_address
    settings.port = payload.port
    settings.comm_key = payload.comm_key
    settings.sync_interval_minutes = payload.sync_interval_minutes
    settings.saturday_is_working_day = payload.saturday_is_working_day
    settings.saturday_start_time = payload.saturday_start_time
    settings.saturday_end_time = payload.saturday_end_time
    settings.saturday_grace_period_minutes = payload.saturday_grace_period_minutes
    settings.saturday_late_after_minutes = payload.saturday_late_after_minutes
    settings.sunday_is_working_day = payload.sunday_is_working_day
    db.commit()
    db.refresh(settings)

    if hardware_changed:
        SyncService.sync(db)
    elif work_week_changed:
        today = datetime.date.today()
        schedule_attendance_recalc(today.replace(day=1), today)

    return settings


@router.post("/api/sync")
def force_sync(full: bool = False, db: Session = Depends(get_db)):
    result = SyncService.sync(db, full_recalc=full, manual_full=full)
    if result["status"] == "Failed":
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@router.get("/api/attendance/export")
def export_attendance(
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    department_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    if not start_date:
        start_date = datetime.date.today().replace(day=1)
    if not end_date:
        end_date = datetime.date.today()

    query = db.query(models.DailyAttendance).options(
        joinedload(models.DailyAttendance.employee).joinedload(models.Employee.department)
    ).join(models.Employee)
    query = query.filter(models.DailyAttendance.date >= start_date)
    query = query.filter(models.DailyAttendance.date <= end_date)
    
    if status:
        query = query.filter(models.DailyAttendance.status == status)
    if search:
        query = query.filter(
            (models.Employee.name.like(f"%{search}%")) |
            (models.Employee.user_id == search)
        )
    if department_id:
        query = query.filter(models.Employee.department_id == department_id)

    records = query.order_by(models.DailyAttendance.date.desc(), models.Employee.name).all()

    import io
    from openpyxl import Workbook
    from fastapi.responses import StreamingResponse

    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance Logs"
    headers = [
        "Date", "Employee Name", "Department", "Device ID", "Check In", "Check Out",
        "Work Hours", "Status", "Late Minutes", "Early Leave Minutes", "Remarks"
    ]
    ws.append(headers)

    for r in records:
        ws.append([
            r.date.strftime("%Y-%m-%d"),
            r.employee.name,
            r.employee.department.name if r.employee.department else "",
            r.employee.user_id,
            r.check_in.strftime("%Y-%m-%d %H:%M:%S") if r.check_in else "",
            r.check_out.strftime("%Y-%m-%d %H:%M:%S") if r.check_out else "",
            r.work_hours,
            r.status,
            r.late_minutes,
            r.early_leave_minutes,
            r.remarks or ""
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = max(max_len + 3, 10)

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    filename = f"attendance_report_{start_date}_to_{end_date}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# --- AI Chat / Voice Assistant Endpoints ---

class ConversationMessage(BaseModel):
    role: str
    content: str

class TextQueryRequest(BaseModel):
    query: str
    history: Optional[List[ConversationMessage]] = []

class UpdateVoiceRequest(BaseModel):
    voice: str

class SpeakRequest(BaseModel):
    text: str
    language: Optional[str] = None

@router.get("/api/ai/status")
def ai_status():
    return ai_service.get_ai_status()

@router.post("/api/ai/update-voice")
def ai_update_voice(payload: UpdateVoiceRequest):
    voice = payload.voice.lower().strip()
    valid_voices = ["sage", "nova", "onyx", "alloy", "echo", "fable", "shimmer", "coral", "ash"]
    if voice not in valid_voices:
        raise HTTPException(status_code=400, detail=f"Invalid voice. Valid voices are: {valid_voices}")
    os.environ["OPENAI_TTS_VOICE"] = voice
    return ai_service.get_ai_status()

@router.post("/api/ai/speak")
def ai_speak(payload: SpeakRequest):
    try:
        speech_text = ai_service.prepare_speech_text(payload.text, payload.language)
        audio_base64 = ai_service.generate_speech(
            payload.text,
            language=payload.language,
            speech_text=speech_text,
        )
        return {"audio": audio_base64, "speech_text": speech_text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/ai/query-text")
def ai_query_text(payload: TextQueryRequest, include_audio: bool = Query(default=False)):
    try:
        history = ai_service.trim_conversation_history(
            [{"role": m.role, "content": m.content} for m in (payload.history or [])]
        )
        result = ai_service.process_user_query(payload.query, conversation_history=history)

        if include_audio:
            ai_service.attach_audio_to_result(result)

        return result
    except Exception as e:
        print(f"[query-text error] {e}")
        return {
            "question": payload.query,
            "sql": None,
            "answer": ai_service.friendly_user_error(payload.query, reason="default"),
            "audio": "",
            "speech_text": "",
            "explanation": "",
            "candidate_matches": [],
            "query_results": [],
            "understanding": {"language": ai_service.detect_query_language(payload.query)},
        }

@router.post("/api/ai/query-voice")
async def ai_query_voice(
    file: UploadFile = File(...),
    history: str = Form(default="[]"),
    include_audio: str = Form(default="false"),
):
    try:
        try:
            history_list = ai_service.trim_conversation_history(json.loads(history or "[]"))
        except json.JSONDecodeError:
            history_list = []

        # Read file bytes
        file_bytes = await file.read()
        
        # 1. Transcribe audio to text
        query_text = ai_service.transcribe_audio(file_bytes, file.filename)
        if not query_text or not query_text.strip():
            fallback_answer = "I'm sorry, I couldn't hear or transcribe any clear speech. Please try speaking again."
            audio_base64 = ""
            try:
                audio_base64 = ai_service.generate_speech(fallback_answer)
            except Exception:
                pass
            return {
                "question": "Voice Question",
                "sql": None,
                "answer": fallback_answer,
                "audio": audio_base64
            }
            
        # 2. Run full AI pipeline
        result = ai_service.process_user_query(query_text, conversation_history=history_list)

        if want_audio := str(include_audio).strip().lower() in {"1", "true", "yes"}:
            ai_service.attach_audio_to_result(result)

        return result
    except Exception as e:
        print(f"[query-voice error] {e}")
        return {
            "question": "Voice Question",
            "sql": None,
            "answer": ai_service.friendly_user_error("", reason="default"),
            "audio": "",
            "speech_text": "",
            "explanation": "",
            "candidate_matches": [],
            "query_results": [],
            "understanding": {},
        }
