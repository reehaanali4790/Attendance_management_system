import datetime

from sqlalchemy.orm import Session

from models import CompanyHoliday


def load_holiday_map(
    db: Session,
    start_date: datetime.date,
    end_date: datetime.date,
) -> dict[datetime.date, str]:
    rows = (
        db.query(CompanyHoliday)
        .filter(
            CompanyHoliday.holiday_date >= start_date,
            CompanyHoliday.holiday_date <= end_date,
        )
        .order_by(CompanyHoliday.holiday_date)
        .all()
    )
    return {row.holiday_date: (row.name or "Holiday") for row in rows}


def load_holiday_dates(
    db: Session,
    start_date: datetime.date,
    end_date: datetime.date,
) -> set[datetime.date]:
    return set(load_holiday_map(db, start_date, end_date).keys())
