"""Lightweight SQLite migrations for additive schema changes."""
from sqlalchemy import inspect, text


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
