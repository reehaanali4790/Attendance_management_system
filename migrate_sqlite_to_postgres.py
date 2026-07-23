"""
One-time migration: copy all data from local SQLite (attendance.db) to PostgreSQL (DATABASE_URL).
"""
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.inspection import inspect as sa_inspect

load_dotenv()

SQLITE_URL = "sqlite:///./attendance.db"
POSTGRES_URL = os.getenv("DATABASE_URL")

if not POSTGRES_URL:
    print("ERROR: DATABASE_URL not set in .env")
    sys.exit(1)

if POSTGRES_URL.startswith("postgres://"):
    POSTGRES_URL = POSTGRES_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif POSTGRES_URL.startswith("postgresql://") and "+psycopg2" not in POSTGRES_URL:
    POSTGRES_URL = POSTGRES_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

from database import Base
from models import (
    DeviceSettings,
    Department,
    Shift,
    LeaveType,
    Employee,
    LeaveRequest,
    AttendanceLog,
    DailyAttendance,
)

MODEL_ORDER = [
    DeviceSettings,
    Department,
    Shift,
    LeaveType,
    Employee,
    LeaveRequest,
    AttendanceLog,
    DailyAttendance,
]


def main():
    if not os.path.exists("attendance.db"):
        print("ERROR: attendance.db not found")
        sys.exit(1)

    src_engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})
    dst_engine = create_engine(POSTGRES_URL, pool_pre_ping=True)

    Base.metadata.create_all(bind=dst_engine)

    SrcSession = sessionmaker(bind=src_engine)
    DstSession = sessionmaker(bind=dst_engine)

    src = SrcSession()
    dst = DstSession()

    try:
        # Clear child tables first (reverse order), then copy in FK order
        for model in reversed(MODEL_ORDER):
            dst.query(model).delete()
        dst.commit()

        for model in MODEL_ORDER:
            table = model.__tablename__
            rows = src.query(model).order_by(model.id).all()
            for row in rows:
                data = {
                    col.key: getattr(row, col.key)
                    for col in sa_inspect(model).mapper.column_attrs
                }
                dst.add(model(**data))
            dst.commit()
            print(f"  {table}: {len(rows)} rows")

        # Reset Postgres serial sequences
        if dst_engine.dialect.name == "postgresql":
            with dst_engine.begin() as conn:
                for model in MODEL_ORDER:
                    table = model.__tablename__
                    pk_cols = inspect(dst_engine).get_pk_constraint(table)["constrained_columns"] or []
                    if len(pk_cols) != 1:
                        continue
                    pk = pk_cols[0]
                    seq = conn.execute(
                        text("SELECT pg_get_serial_sequence(:table, :col)"),
                        {"table": table, "col": pk},
                    ).scalar()
                    if seq:
                        conn.execute(
                            text(
                                f"SELECT setval(:seq, COALESCE((SELECT MAX({pk}) FROM {table}), 1), true)"
                            ),
                            {"seq": seq},
                        )
    finally:
        src.close()
        dst.close()

    print("Migration complete.")


if __name__ == "__main__":
    main()
