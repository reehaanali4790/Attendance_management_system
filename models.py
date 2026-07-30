from sqlalchemy import Column, Integer, String, Boolean, DateTime, Time, Date, Float, ForeignKey, UniqueConstraint, Text
from sqlalchemy.orm import relationship
import datetime
from database import Base

class DeviceSettings(Base):
    __tablename__ = "device_settings"
    
    id = Column(Integer, primary_key=True, index=True)
    ip_address = Column(String, default="192.168.1.201")
    port = Column(Integer, default=4370)
    comm_key = Column(Integer, default=0)
    sync_interval_minutes = Column(Integer, default=5)
    last_sync_time = Column(DateTime, nullable=True)
    last_sync_status = Column(String, nullable=True)
    # Saturday half-day policy (office works Sat 11:00–16:00 by default; Sunday off)
    saturday_is_working_day = Column(Boolean, default=True)
    saturday_start_time = Column(Time, default=datetime.time(11, 0))
    saturday_end_time = Column(Time, default=datetime.time(16, 0))
    saturday_grace_period_minutes = Column(Integer, default=15)
    saturday_late_after_minutes = Column(Integer, default=30)
    sunday_is_working_day = Column(Boolean, default=False)

class Department(Base):
    __tablename__ = "departments"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True)
    description = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    employees = relationship("Employee", back_populates="department")

class Shift(Base):
    __tablename__ = "shifts"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    grace_period_minutes = Column(Integer, default=15)
    late_after_minutes = Column(Integer, default=30)
    
    employees = relationship("Employee", back_populates="shift")

class Employee(Base):
    __tablename__ = "employees"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    privilege = Column(Integer, default=0)
    card_number = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    shift_id = Column(Integer, ForeignKey("shifts.id"), nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    
    shift = relationship("Shift", back_populates="employees")
    department = relationship("Department", back_populates="employees")
    daily_attendance = relationship("DailyAttendance", back_populates="employee", cascade="all, delete-orphan")
    leave_requests = relationship("LeaveRequest", back_populates="employee", cascade="all, delete-orphan")

class LeaveType(Base):
    __tablename__ = "leave_types"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True)
    description = Column(String, nullable=True)
    is_paid = Column(Boolean, default=True)
    requires_application = Column(Boolean, default=True)

    leave_requests = relationship("LeaveRequest", back_populates="leave_type")

class LeaveRequest(Base):
    __tablename__ = "leave_requests"

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    leave_type_id = Column(Integer, ForeignKey("leave_types.id"), nullable=False)
    start_date = Column(Date, nullable=False, index=True)
    end_date = Column(Date, nullable=False, index=True)
    is_half_day = Column(Boolean, default=False)
    half_day_period = Column(String, nullable=True)  # "AM" or "PM"
    reason = Column(Text, nullable=True)
    application_received = Column(Boolean, default=False)
    status = Column(String, default="Pending")  # Pending, Approved, Rejected
    recorded_by = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    employee = relationship("Employee", back_populates="leave_requests")
    leave_type = relationship("LeaveType", back_populates="leave_requests")

class AttendanceLog(Base):
    __tablename__ = "attendance_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=False)
    timestamp = Column(DateTime, index=True, nullable=False)
    punch_type = Column(String, nullable=False)
    status_code = Column(Integer, nullable=False)
    imported_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    __table_args__ = (
        UniqueConstraint('user_id', 'timestamp', name='_user_timestamp_uc'),
    )

class DailyAttendance(Base):
    __tablename__ = "daily_attendance"
    
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    date = Column(Date, index=True, nullable=False)
    check_in = Column(DateTime, nullable=True)
    check_out = Column(DateTime, nullable=True)
    work_hours = Column(Float, default=0.0)
    status = Column(String, nullable=False)  # Present, Late, Absent, Left Early, Half Day, On Leave
    late_minutes = Column(Integer, default=0)
    early_leave_minutes = Column(Integer, default=0)
    remarks = Column(String, nullable=True)
    
    employee = relationship("Employee", back_populates="daily_attendance")
    
    __table_args__ = (
        UniqueConstraint('employee_id', 'date', name='_employee_date_uc'),
    )


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
