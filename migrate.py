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
