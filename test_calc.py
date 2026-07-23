import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database import Base
import models
from sync_service import SyncService

def run_tests():
    print("Initializing test database...")
    # Create in-memory SQLite for testing
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    
    # Create tables
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    
    try:
        # 1. Initialize defaults
        print("Setting up shifts and settings...")
        settings, default_shift = SyncService.initialize_defaults(db)
        
        # 2. Add standard test employee
        employee = models.Employee(
            user_id="101",
            name="Test User",
            is_active=True,
            shift_id=default_shift.id
        )
        db.add(employee)
        db.commit()
        db.refresh(employee)
        
        # Dynamically calculate workdays (Monday-Friday) to avoid weekend skips
        workdays = []
        d = datetime.date.today()
        while len(workdays) < 6:
            d -= datetime.timedelta(days=1)
            if d.weekday() < 5:
                workdays.append(d)
                
        date_tc4 = workdays[0]
        date_tc3 = workdays[1]
        date_tc2 = workdays[2]
        date_tc1 = workdays[3]
        date_leave = workdays[4]
        date_pending = workdays[5]
        
        print(f"Dynamic test dates: TC1={date_tc1}, TC2={date_tc2}, TC3={date_tc3}, TC4={date_tc4}")

        # Test Case 1: Punch at 08:50 AM (Shift Start: 09:00 AM, Grace: 15m) -> Expect "Present"
        print("\n--- Test Case 1: On-Time check-in (8:50 AM) ---")
        punch_in_1 = datetime.datetime.combine(date_tc1, datetime.time(8, 50, 0))
        punch_out_1 = datetime.datetime.combine(date_tc1, datetime.time(17, 0, 0))
        
        db.add(models.AttendanceLog(user_id="101", timestamp=punch_in_1, punch_type="Check In", status_code=1))
        db.add(models.AttendanceLog(user_id="101", timestamp=punch_out_1, punch_type="Check Out", status_code=0))
        db.commit()
        
        SyncService.process_daily_attendance(db)
        
        att_tc1 = db.query(models.DailyAttendance).filter_by(employee_id=employee.id, date=date_tc1).first()
        assert att_tc1 is not None, "Error: attendance record TC1 was not created"
        print(f"Status calculated: {att_tc1.status}")
        print(f"Work hours: {att_tc1.work_hours}")
        assert att_tc1.status == "Present", f"Expected 'Present', got {att_tc1.status}"
        assert att_tc1.work_hours == 8.17, f"Expected 8.17 hours, got {att_tc1.work_hours}"
        print("Test Case 1 Passed!")
        
        # Test Case 2: Punch at 09:18 AM (Shift Start: 09:00 AM, Grace: 15m) -> Expect "Late"
        print("\n--- Test Case 2: Late check-in (9:18 AM) ---")
        punch_in_2 = datetime.datetime.combine(date_tc2, datetime.time(9, 18, 0))
        punch_out_2 = datetime.datetime.combine(date_tc2, datetime.time(17, 5, 0))
        
        db.add(models.AttendanceLog(user_id="101", timestamp=punch_in_2, punch_type="Check In", status_code=1))
        db.add(models.AttendanceLog(user_id="101", timestamp=punch_out_2, punch_type="Check Out", status_code=0))
        db.commit()
        
        SyncService.process_daily_attendance(db)
        
        att_tc2 = db.query(models.DailyAttendance).filter_by(employee_id=employee.id, date=date_tc2).first()
        assert att_tc2 is not None, "Error: attendance record TC2 was not created"
        print(f"Status calculated: {att_tc2.status}")
        print(f"Late minutes: {att_tc2.late_minutes}")
        assert att_tc2.status == "Late", f"Expected 'Late', got {att_tc2.status}"
        assert att_tc2.late_minutes == 18, f"Expected 18 late minutes, got {att_tc2.late_minutes}"
        print("Test Case 2 Passed!")
        
        # Test Case 3: Single Punch at 09:00 AM on a past day -> Expect "Missing Check Out"
        print("\n--- Test Case 3: Missing Check Out (Single Punch 9:00 AM on past day) ---")
        punch_in_3 = datetime.datetime.combine(date_tc3, datetime.time(9, 0, 0))
        
        db.add(models.AttendanceLog(user_id="101", timestamp=punch_in_3, punch_type="Check In", status_code=1))
        db.commit()
        
        SyncService.process_daily_attendance(db)
        
        att_tc3 = db.query(models.DailyAttendance).filter_by(employee_id=employee.id, date=date_tc3).first()
        assert att_tc3 is not None, "Error: attendance record TC3 was not created"
        print(f"Status calculated: {att_tc3.status}")
        print(f"Remarks: {att_tc3.remarks}")
        assert att_tc3.status == "Present", f"Expected 'Present' (since checked in on time), got {att_tc3.status}"
        assert att_tc3.remarks == "Missing Check Out", f"Expected 'Missing Check Out', got {att_tc3.remarks}"
        print("Test Case 3 Passed!")
        
        # Test Case 4: Punch out early at 04:30 PM (Shift End: 05:00 PM) -> Expect "Left Early"
        print("\n--- Test Case 4: Left Early (Check-in 9:00 AM, Check-out 4:30 PM) ---")
        punch_in_4 = datetime.datetime.combine(date_tc4, datetime.time(9, 0, 0))
        punch_out_4 = datetime.datetime.combine(date_tc4, datetime.time(16, 30, 0))
        
        db.add(models.AttendanceLog(user_id="101", timestamp=punch_in_4, punch_type="Check In", status_code=1))
        db.add(models.AttendanceLog(user_id="101", timestamp=punch_out_4, punch_type="Check Out", status_code=0))
        db.commit()
        
        SyncService.process_daily_attendance(db)
        
        att_tc4 = db.query(models.DailyAttendance).filter_by(employee_id=employee.id, date=date_tc4).first()
        assert att_tc4 is not None, "Error: attendance record TC4 was not created"
        print(f"Status calculated: {att_tc4.status}")
        print(f"Early leave minutes: {att_tc4.early_leave_minutes}")
        assert att_tc4.status == "Left Early", f"Expected 'Left Early', got {att_tc4.status}"
        assert att_tc4.early_leave_minutes == 30, f"Expected 30 early leave minutes, got {att_tc4.early_leave_minutes}"
        print("Test Case 4 Passed!")

        # Test Case 5: Approved full-day leave with no punch -> On Leave
        print("\n--- Test Case 5: Approved leave (no punch) -> On Leave ---")
        sick_leave = db.query(models.LeaveType).filter_by(name="Sick Leave").first()
        leave_date = date_leave
        db.add(models.LeaveRequest(
            employee_id=employee.id,
            leave_type_id=sick_leave.id,
            start_date=leave_date,
            end_date=leave_date,
            reason="Doctor appointment",
            application_received=True,
            status="Approved",
            recorded_by="HR Admin"
        ))
        db.commit()
        SyncService.process_daily_attendance(db, leave_date, leave_date)
        att_leave = db.query(models.DailyAttendance).filter_by(employee_id=employee.id, date=leave_date).first()
        assert att_leave is not None
        assert att_leave.status == "On Leave", f"Expected 'On Leave', got {att_leave.status}"
        print(f"Status calculated: {att_leave.status}")
        print("Test Case 5 Passed!")

        # Test Case 6: Pending leave without application -> Absent with remark
        print("\n--- Test Case 6: Pending leave, no application -> Absent ---")
        pending_date = date_pending
        db.add(models.LeaveRequest(
            employee_id=employee.id,
            leave_type_id=sick_leave.id,
            start_date=pending_date,
            end_date=pending_date,
            application_received=False,
            status="Pending"
        ))
        db.commit()
        SyncService.process_daily_attendance(db, pending_date, pending_date)
        att_pending = db.query(models.DailyAttendance).filter_by(employee_id=employee.id, date=pending_date).first()
        assert att_pending is not None
        assert att_pending.status == "Absent"
        assert "no leave application" in (att_pending.remarks or "").lower()
        print(f"Status: {att_pending.status}, Remarks: {att_pending.remarks}")
        print("Test Case 6 Passed!")
        
        print("\nAll automated tests completed successfully!")
        
    finally:
        db.close()

if __name__ == "__main__":
    run_tests()
