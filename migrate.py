"""Lightweight SQLite migrations for additive schema changes."""
from sqlalchemy import inspect, text


def _create_index_if_missing(conn, inspector, table_name: str, index_name: str, ddl: str):
    existing = {idx["name"] for idx in inspector.get_indexes(table_name)}
    if index_name not in existing:
        conn.execute(text(ddl))


def run_migrations(engine):
    inspector = inspect(engine)
    table_names = inspector.get_table_names()

    if "employees" in table_names:
        employee_cols = {col["name"] for col in inspector.get_columns("employees")}
        if "department_id" not in employee_cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE employees ADD COLUMN department_id INTEGER REFERENCES departments(id)"
                ))

    with engine.begin() as conn:
        inspector = inspect(conn)
        table_names = inspector.get_table_names()

        if "daily_attendance" in table_names:
            _create_index_if_missing(
                conn,
                inspector,
                "daily_attendance",
                "idx_daily_attendance_date_status",
                "CREATE INDEX IF NOT EXISTS idx_daily_attendance_date_status ON daily_attendance (date, status)",
            )
            _create_index_if_missing(
                conn,
                inspector,
                "daily_attendance",
                "idx_daily_attendance_employee_date",
                "CREATE INDEX IF NOT EXISTS idx_daily_attendance_employee_date ON daily_attendance (employee_id, date)",
            )

        if "employees" in table_names:
            if engine.dialect.name == "postgresql":
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_employees_active_true "
                    "ON employees (is_active) WHERE is_active IS TRUE"
                ))
            else:
                _create_index_if_missing(
                    conn,
                    inspector,
                    "employees",
                    "idx_employees_is_active",
                    "CREATE INDEX IF NOT EXISTS idx_employees_is_active ON employees (is_active)",
                )

        if "device_settings" in table_names:
            device_cols = {col["name"] for col in inspector.get_columns("device_settings")}
            additions = [
                ("saturday_is_working_day", "BOOLEAN DEFAULT TRUE"),
                ("saturday_start_time", "TIME DEFAULT '11:00:00'"),
                ("saturday_end_time", "TIME DEFAULT '16:00:00'"),
                ("saturday_grace_period_minutes", "INTEGER DEFAULT 15"),
                ("saturday_late_after_minutes", "INTEGER DEFAULT 30"),
                ("sunday_is_working_day", "BOOLEAN DEFAULT FALSE"),
            ]
            for col_name, col_type in additions:
                if col_name not in device_cols:
                    conn.execute(text(f"ALTER TABLE device_settings ADD COLUMN {col_name} {col_type}"))
