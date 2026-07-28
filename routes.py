from fastapi import APIRouter, Depends, HTTPException, Query, File, UploadFile, WebSocket, Form

from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from pydantic import BaseModel
from database import get_db
import models
import schemas
from sync_service import SyncService, schedule_attendance_recalc
from auth import get_current_user
import datetime
from typing import List, Optional
import json
import ai_service

router = APIRouter(dependencies=[Depends(get_current_user)])

VALID_LEAVE_STATUSES = {"Pending", "Approved", "Rejected"}


def _department_response(dept: models.Department, db: Session) -> schemas.DepartmentResponse:
    count = db.query(models.Employee).filter_by(department_id=dept.id).count()
    return schemas.DepartmentResponse(
        id=dept.id,
        name=dept.name,
        description=dept.description,
        is_active=dept.is_active,
        created_at=dept.created_at,
        employee_count=count
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
    total_employees = db.query(models.Employee).filter_by(is_active=True).count()
    attendance_today = db.query(models.DailyAttendance).filter_by(date=today).all()

    present_today = 0
    late_today = 0
    left_early_today = 0
    absent_today = 0
    on_leave_today = 0
    half_day_today = 0
    total_hours = 0.0
    present_count_hours = 0

    working_statuses = {"Present", "Late", "Left Early"}
    for att in attendance_today:
        if att.status == "Absent":
            absent_today += 1
        elif att.status == "On Leave":
            on_leave_today += 1
        elif att.status == "Half Day":
            half_day_today += 1
        elif att.status in working_statuses:
            present_today += 1
            if att.status == "Late":
                late_today += 1
            elif att.status == "Left Early":
                left_early_today += 1
            if att.work_hours > 0:
                total_hours += att.work_hours
                present_count_hours += 1

    avg_hours = round(total_hours / present_count_hours, 2) if present_count_hours > 0 else 0.0

    recent_logs = db.query(models.AttendanceLog).order_by(models.AttendanceLog.timestamp.desc()).limit(10).all()
    emp_map = {e.user_id: e.name for e in db.query(models.Employee).all()}
    recent_punches = [
        schemas.RecentPunch(
            user_id=log.user_id,
            employee_name=emp_map.get(log.user_id, "Unknown User"),
            timestamp=log.timestamp,
            punch_type=log.punch_type
        )
        for log in recent_logs
    ]

    start_trend_date = today - datetime.timedelta(days=7)
    trend_recs = db.query(models.DailyAttendance).filter(
        models.DailyAttendance.date >= start_trend_date,
        models.DailyAttendance.date <= today
    ).all()

    trend_recs_by_date = {}
    for r in trend_recs:
        d_str = r.date.strftime("%Y-%m-%d")
        trend_recs_by_date.setdefault(d_str, []).append(r)

    weekly_trend = {}
    for day_offset in range(7, -1, -1):
        d = today - datetime.timedelta(days=day_offset)
        if d.weekday() >= 5:
            continue
        d_str = d.strftime("%Y-%m-%d")
        recs = trend_recs_by_date.get(d_str, [])
        weekly_trend[d_str] = {
            "present": sum(1 for r in recs if r.status in working_statuses),
            "late": sum(1 for r in recs if r.status == "Late"),
            "absent": sum(1 for r in recs if r.status == "Absent"),
            "on_leave": sum(1 for r in recs if r.status == "On Leave"),
        }

    # Calculate department stats for today
    dept_stats = []
    depts = db.query(models.Department).filter_by(is_active=True).all()
    for dept in depts:
        emp_ids = [e.id for e in dept.employees if e.is_active]
        if not emp_ids:
            continue
        dept_attendance = db.query(models.DailyAttendance).filter(
            models.DailyAttendance.date == today,
            models.DailyAttendance.employee_id.in_(emp_ids)
        ).all()
        
        dept_present = sum(1 for r in dept_attendance if r.status in working_statuses)
        dept_absent = sum(1 for r in dept_attendance if r.status == "Absent")
        dept_on_leave = sum(1 for r in dept_attendance if r.status == "On Leave")
        
        dept_stats.append({
            "department_name": dept.name,
            "total_employees": len(emp_ids),
            "present": dept_present,
            "absent": dept_absent,
            "on_leave": dept_on_leave
        })

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
    db: Session = Depends(get_db)
):
    if not start_date:
        start_date = datetime.date.today().replace(day=1)
    if not end_date:
        end_date = datetime.date.today()

    # Build daily summaries from raw punches for the requested range (lazy/on-demand)
    SyncService.process_daily_attendance(db, start_date, end_date)

    query = db.query(models.DailyAttendance).join(models.Employee)
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
    db.refresh(emp)
    schedule_attendance_recalc()
    return emp


# --- Departments ---
@router.get("/api/departments", response_model=List[schemas.DepartmentResponse])
def get_departments(db: Session = Depends(get_db)):
    departments = db.query(models.Department).order_by(models.Department.name).all()
    return [_department_response(d, db) for d in departments]


@router.post("/api/departments", response_model=schemas.DepartmentResponse)
def create_department(payload: schemas.DepartmentCreate, db: Session = Depends(get_db)):
    existing = db.query(models.Department).filter_by(name=payload.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Department with this name already exists")
    dept = models.Department(**payload.model_dump())
    db.add(dept)
    db.commit()
    db.refresh(dept)
    return _department_response(dept, db)


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
    return _department_response(dept, db)


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
    SyncService.process_daily_attendance(db, payload.start_date, payload.end_date)

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
    SyncService.process_daily_attendance(db, recalc_start, recalc_end)

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
    SyncService.process_daily_attendance(db, start, end)
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
def update_shift(shift_id: int, payload: schemas.ShiftUpdate, db: Session = Depends(get_db)):
    shift = db.query(models.Shift).filter_by(id=shift_id).first()
    if not shift:
        raise HTTPException(status_code=404, detail="Shift not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(shift, key, value)
    db.commit()
    db.refresh(shift)
    schedule_attendance_recalc()
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
    settings.ip_address = payload.ip_address
    settings.port = payload.port
    settings.comm_key = payload.comm_key
    settings.sync_interval_minutes = payload.sync_interval_minutes
    db.commit()
    db.refresh(settings)
    SyncService.sync(db)
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

    SyncService.process_daily_attendance(db, start_date, end_date)

    query = db.query(models.DailyAttendance).join(models.Employee)
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
