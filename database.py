import os
import re
from sqlalchemy import create_engine, event
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

SUPABASE_HOST_RE = re.compile(
    r"(db\.[a-z0-9]+\.supabase\.co|aws-0-[a-z0-9-]+\.pooler\.supabase\.com):(\d+)(/.*)?$",
    re.IGNORECASE,
)


def _looks_like_broken_host(host: str | None) -> bool:
    if not host:
        return True
    return "*" in host or host[0].isdigit() or "." not in host


def _repair_postgres_url(url: str) -> str:
    """Fix URLs where special chars in the password (e.g. @) break host parsing."""
    match = re.match(r"^(postgresql(?:\+psycopg2)?|postgres)://([^:]+):(.+)$", url, re.IGNORECASE)
    if not match:
        return url

    user, remainder = match.group(2), match.group(3)
    host_match = SUPABASE_HOST_RE.search(remainder)
    if not host_match:
        return url

    password = remainder[: host_match.start()].rstrip("@")
    host = host_match.group(1)
    port = int(host_match.group(2))
    database = (host_match.group(3) or "/postgres").lstrip("/")

    return str(
        URL.create(
            drivername="postgresql+psycopg2",
            username=user,
            password=password,
            host=host,
            port=port,
            database=database,
        )
    )


def _database_url_from_parts() -> str | None:
    host = os.getenv("POSTGRES_HOST")
    user = os.getenv("POSTGRES_USER")
    password = os.getenv("POSTGRES_PASSWORD")
    if not (host and user and password):
        return None

    return str(
        URL.create(
            drivername="postgresql+psycopg2",
            username=user,
            password=password,
            host=host,
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            database=os.getenv("POSTGRES_DB", "postgres"),
        )
    )


def resolve_database_url() -> str:
    url = _database_url_from_parts() or os.getenv("DATABASE_URL", "sqlite:///./attendance.db")
    if url.startswith("sqlite"):
        return url

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    try:
        parsed = make_url(url)
        if _looks_like_broken_host(parsed.host):
            url = _repair_postgres_url(url)
    except Exception:
        url = _repair_postgres_url(url)

    if url.startswith("postgresql://") and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)

    return url


DATABASE_URL = resolve_database_url()

connect_args = {}
engine_kwargs = {"pool_pre_ping": True}

if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=connect_args, **engine_kwargs)

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
