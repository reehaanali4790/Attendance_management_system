import datetime
from sqlalchemy import func
from sqlalchemy.orm import Session
from models import DeviceSettings, Employee, Shift, AttendanceLog, DailyAttendance, LeaveRequest, LeaveType, Department
from zk_service import ZKService
import logging

logger = logging.getLogger("SyncService")

DEFAULT_LEAVE_TYPES = [
    {"name": "Sick Leave", "description": "Medical or health-related absence", "is_paid": True},
    {"name": "Casual Leave", "description": "Personal or casual absence", "is_paid": True},
    {"name": "Annual Leave", "description": "Planned vacation or annual time off", "is_paid": True},
    {"name": "Emergency Leave", "description": "Urgent unplanned absence", "is_paid": True},
    {"name": "Unpaid Leave", "description": "Leave without pay", "is_paid": False},
]

ATTENDANCE_STATUSES = {"Present", "Late", "Absent", "Left Early", "Half Day", "On Leave"}


class SyncService:
    @staticmethod
    def initialize_defaults(db: Session):
        """Ensure default device settings, shift, and leave types exist in the DB."""
        default_shift = db.query(Shift).filter_by(name="General Shift").first()
        if not default_shift:
            default_shift = Shift(
                name="General Shift",
                start_time=datetime.time(9, 0),
                end_time=datetime.time(17, 0),
                grace_period_minutes=15,
                late_after_minutes=30
            )
            db.add(default_shift)
            db.commit()
            db.refresh(default_shift)
            logger.info("Initialized default shift: General Shift (09:00 - 17:00)")

        settings = db.query(DeviceSettings).first()
        if not settings:
            settings = DeviceSettings(
                ip_address="192.168.18.58",
                port=4370,
                comm_key=0,
                sync_interval_minutes=5
            )
            db.add(settings)
            db.commit()
            db.refresh(settings)
            logger.info("Initialized default device settings")

        for lt in DEFAULT_LEAVE_TYPES:
            if not db.query(LeaveType).filter_by(name=lt["name"]).first():
                db.add(LeaveType(**lt))
        db.commit()

        return settings, default_shift

    @staticmethod
    def _build_leave_map(db: Session, start_date: datetime.date, end_date: datetime.date) -> dict:
        """Map (employee_id, date) -> LeaveRequest for requests overlapping the range."""
        leave_requests = db.query(LeaveRequest).filter(
            LeaveRequest.start_date <= end_date,
            LeaveRequest.end_date >= start_date,
            LeaveRequest.status.in_(["Pending", "Approved"])
        ).all()

        leave_map = {}
        for lr in leave_requests:
            current = max(lr.start_date, start_date)
            last = min(lr.end_date, end_date)
            while current <= last:
                leave_map[(lr.employee_id, current)] = lr
                current += datetime.timedelta(days=1)
        return leave_map

    @staticmethod
    def _apply_leave_record(daily_rec, emp, target_date, leave: LeaveRequest, is_half_day_only: bool):
        leave_type_name = leave.leave_type.name if leave.leave_type else "Leave"
        if is_half_day_only:
            period = leave.half_day_period or "Half"
            remarks = leave.reason or f"Approved {leave_type_name} ({period})"
            if not daily_rec:
                daily_rec = DailyAttendance(
                    employee_id=emp.id,
                    date=target_date,
                    status="Half Day",
                    work_hours=0.0,
                    remarks=remarks
                )
            else:
                daily_rec.status = "Half Day"
                daily_rec.check_in = None
                daily_rec.check_out = None
                daily_rec.work_hours = 0.0
                daily_rec.late_minutes = 0
                daily_rec.early_leave_minutes = 0
                daily_rec.remarks = remarks
        else:
            remarks = leave.reason or f"Approved {leave_type_name}"
            if not daily_rec:
                daily_rec = DailyAttendance(
                    employee_id=emp.id,
                    date=target_date,
                    status="On Leave",
                    work_hours=0.0,
                    remarks=remarks
                )
            else:
                daily_rec.status = "On Leave"
                daily_rec.check_in = None
                daily_rec.check_out = None
                daily_rec.work_hours = 0.0
                daily_rec.late_minutes = 0
                daily_rec.early_leave_minutes = 0
                daily_rec.remarks = remarks
        return daily_rec

    @classmethod
    def sync(cls, db: Session, full_recalc: bool = False) -> dict:
        """Synchronize users and new device punches, then recalculate daily attendance.

        Regular sync (full_recalc=False):
          - Imports only recent device punches (since last sync, with 1-day buffer)
          - Recalculates daily attendance for yesterday + today only

        Full sync (full_recalc=True, or first-ever sync):
          - Imports all device punches
          - Recalculates daily attendance for the entire current month
        """
        settings, default_shift = cls.initialize_defaults(db)

        zk_srv = ZKService(
            ip=settings.ip_address,
            port=settings.port,
            comm_key=settings.comm_key
        )

        status_info = {
            "status": "Success",
            "users_synced": 0,
            "logs_synced": 0,
            "error": None
        }

        try:
            is_first_sync = settings.last_sync_time is None
            if is_first_sync:
                full_recalc = True

            zk_srv.connect()

            # --- Step 1: Sync employees (single bulk load, no per-user queries) ---
            zk_users = zk_srv.get_users()
            existing_emps = {e.user_id: e for e in db.query(Employee).all()}
            for zk_user in zk_users:
                emp = existing_emps.get(str(zk_user.user_id))
                if not emp:
                    emp = Employee(
                        user_id=str(zk_user.user_id),
                        name=zk_user.name,
                        privilege=zk_user.privilege,
                        card_number=str(zk_user.card) if zk_user.card else None,
                        is_active=True,
                        shift_id=default_shift.id
                    )
                    db.add(emp)
                    status_info["users_synced"] += 1
                elif emp.name != zk_user.name or emp.privilege != zk_user.privilege:
                    emp.name = zk_user.name
                    emp.privilege = zk_user.privilege
            db.commit()

            zk_srv.disconnect()
            import time
            time.sleep(1)

            # --- Step 2: Import raw punches from device ---
            zk_srv.connect()
            zk_logs = zk_srv.get_attendance()

            # Incremental sync: only process recent device logs
            log_cutoff = None
            if not full_recalc and settings.last_sync_time:
                log_cutoff = settings.last_sync_time - datetime.timedelta(days=1)
                filtered = []
                cutoff_naive = log_cutoff.replace(tzinfo=None, microsecond=0)
                for log in zk_logs:
                    ts = log.timestamp.replace(microsecond=0)
                    if ts.tzinfo:
                        ts = ts.replace(tzinfo=None)
                    if ts >= cutoff_naive:
                        filtered.append(log)
                zk_logs = filtered
                logger.info(f"Incremental log import: {len(zk_logs)} device record(s) since {log_cutoff.date()}")

            # Dedup against DB — only load timestamps in the import window
            if log_cutoff:
                existing_timestamps = set(
                    db.query(AttendanceLog.user_id, AttendanceLog.timestamp).filter(
                        AttendanceLog.timestamp >= log_cutoff
                    ).all()
                )
            else:
                existing_timestamps = set(
                    db.query(AttendanceLog.user_id, AttendanceLog.timestamp).all()
                )
                logger.info(f"Full log import: checking {len(zk_logs)} device record(s) against DB")

            new_logs_added = 0
            status_map = {0: "Check Out", 1: "Check In", 2: "Break Out", 3: "Break In"}
            for zk_log in zk_logs:
                ts = zk_log.timestamp.replace(microsecond=0)
                user_id = str(zk_log.user_id)
                if (user_id, ts) not in existing_timestamps:
                    punch_type = status_map.get(zk_log.status, "Check In" if zk_log.status % 2 != 0 else "Check Out")
                    log_entry = AttendanceLog(
                        user_id=user_id,
                        timestamp=ts,
                        punch_type=punch_type,
                        status_code=zk_log.status
                    )
                    db.add(log_entry)
                    new_logs_added += 1

            db.commit()
            status_info["logs_synced"] = new_logs_added

            # --- Step 3: Recalculate daily attendance ---
            today = datetime.date.today()
            if full_recalc:
                min_log_ts = db.query(func.min(AttendanceLog.timestamp)).scalar()
                start_date = min_log_ts.date() if min_log_ts else today.replace(day=1)
                cls.process_daily_attendance(db, start_date, today)
                logger.info(f"Daily attendance recalculated {start_date} → {today} (full sync)")
            else:
                yesterday = today - datetime.timedelta(days=1)
                cls.process_daily_attendance(db, yesterday, today)
                logger.info(f"Daily attendance recalculated for {yesterday} → {today} (incremental sync)")

            status_info["sync_mode"] = "full" if full_recalc else "incremental"

            settings.last_sync_time = datetime.datetime.now()
            settings.last_sync_status = "Success"
            db.commit()

        except Exception as e:
            logger.error(f"Sync error: {e}")
            settings.last_sync_time = datetime.datetime.now()
            settings.last_sync_status = f"Failed: {str(e)}"
            db.commit()
            status_info["status"] = "Failed"
            status_info["error"] = str(e)

        finally:
            zk_srv.disconnect()

        return status_info

    @classmethod
    def process_daily_attendance(cls, db: Session, start_date: datetime.date = None, end_date: datetime.date = None):
        """Process raw punches into daily attendance, respecting shifts and approved leave."""
        now = datetime.datetime.now()
        if not start_date:
            start_date = now.date().replace(day=1)
        if not end_date:
            end_date = now.date()

        employees = db.query(Employee).filter_by(is_active=True).all()
        if not employees:
            return

        start_dt = datetime.datetime.combine(start_date, datetime.time.min)
        end_dt = datetime.datetime.combine(end_date, datetime.time.max)
        raw_logs = db.query(AttendanceLog).filter(
            AttendanceLog.timestamp >= start_dt,
            AttendanceLog.timestamp <= end_dt
        ).order_by(AttendanceLog.timestamp).all()

        logs_by_user_date = {}
        for log in raw_logs:
            log_date = log.timestamp.date()
            key = (log.user_id, log_date)
            logs_by_user_date.setdefault(key, []).append(log)

        existing_recs = db.query(DailyAttendance).filter(
            DailyAttendance.date >= start_date,
            DailyAttendance.date <= end_date
        ).all()
        existing_recs_map = {(r.employee_id, r.date): r for r in existing_recs}

        leave_map = cls._build_leave_map(db, start_date, end_date)

        delta = end_date - start_date
        dates = [start_date + datetime.timedelta(days=i) for i in range(delta.days + 1)]

        for target_date in dates:
            if target_date.weekday() >= 5:
                continue

            for emp in employees:
                logs = logs_by_user_date.get((emp.user_id, target_date), [])
                shift = emp.shift
                if not shift:
                    continue

                daily_rec = existing_recs_map.get((emp.id, target_date))
                leave = leave_map.get((emp.id, target_date))

                is_past_day = target_date < now.date()
                shift_start_dt = datetime.datetime.combine(target_date, shift.start_time)
                late_limit_dt = shift_start_dt + datetime.timedelta(minutes=shift.late_after_minutes)
                is_today_and_late_passed = (target_date == now.date() and now > late_limit_dt)

                # Approved leave overrides absence when employee did not punch
                if leave and leave.status == "Approved" and not logs:
                    daily_rec = cls._apply_leave_record(
                        daily_rec, emp, target_date, leave, leave.is_half_day
                    )
                    if daily_rec and daily_rec not in existing_recs_map.values():
                        db.add(daily_rec)
                        existing_recs_map[(emp.id, target_date)] = daily_rec
                    continue

                if not logs:
                    if is_past_day or is_today_and_late_passed:
                        status = "Absent"
                        remarks = None
                        if leave and leave.status == "Pending":
                            if leave.application_received:
                                remarks = f"Leave pending approval ({leave.leave_type.name})"
                            else:
                                remarks = "Absent — no leave application on file"
                        elif leave and leave.status == "Rejected":
                            remarks = f"Leave rejected ({leave.leave_type.name})"

                        if not daily_rec:
                            daily_rec = DailyAttendance(
                                employee_id=emp.id,
                                date=target_date,
                                status=status,
                                work_hours=0.0,
                                remarks=remarks
                            )
                            db.add(daily_rec)
                        else:
                            daily_rec.status = status
                            daily_rec.work_hours = 0.0
                            daily_rec.check_in = None
                            daily_rec.check_out = None
                            daily_rec.late_minutes = 0
                            daily_rec.early_leave_minutes = 0
                            daily_rec.remarks = remarks
                    continue

                check_in = logs[0].timestamp
                check_out = logs[-1].timestamp if len(logs) > 1 else None

                ci_time_mins = check_in.hour * 60 + check_in.minute
                shift_start_mins = shift.start_time.hour * 60 + shift.start_time.minute
                late_mins = max(0, ci_time_mins - shift_start_mins)

                status = "Present"
                if late_mins > shift.grace_period_minutes:
                    status = "Late"

                early_leave_mins = 0
                work_hours = 0.0
                remarks = None

                if check_out:
                    co_time_mins = check_out.hour * 60 + check_out.minute
                    shift_end_mins = shift.end_time.hour * 60 + shift.end_time.minute
                    early_leave_mins = max(0, shift_end_mins - co_time_mins)

                    if early_leave_mins > 0 and status == "Present":
                        status = "Left Early"

                    work_hours = round((check_out - check_in).total_seconds() / 3600.0, 2)
                else:
                    if target_date < now.date():
                        remarks = "Missing Check Out"
                    else:
                        remarks = "Checked In (Pending Exit)"

                # Approved half-day leave + punch: note the leave in remarks
                if leave and leave.status == "Approved" and leave.is_half_day:
                    leave_note = leave.reason or f"Approved {leave.leave_type.name} ({leave.half_day_period or 'Half'})"
                    remarks = f"{remarks}; {leave_note}" if remarks else leave_note

                if not daily_rec:
                    daily_rec = DailyAttendance(
                        employee_id=emp.id,
                        date=target_date,
                        check_in=check_in,
                        check_out=check_out,
                        work_hours=work_hours,
                        status=status,
                        late_minutes=late_mins,
                        early_leave_minutes=early_leave_mins,
                        remarks=remarks
                    )
                    db.add(daily_rec)
                else:
                    daily_rec.check_in = check_in
                    daily_rec.check_out = check_out
                    daily_rec.work_hours = work_hours
                    daily_rec.status = status
                    daily_rec.late_minutes = late_mins
                    daily_rec.early_leave_minutes = early_leave_mins
                    daily_rec.remarks = remarks

        db.commit()
