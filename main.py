from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn
import os

from database import engine, Base, SessionLocal
from migrate import run_migrations
from routes import router
from auth_routes import auth_router
from auth import seed_default_admin
from scheduler import start_scheduler, stop_scheduler
from sync_service import SyncService

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Create database tables and apply additive migrations
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    
    # 2. Ensure settings and shifts default setup
    db = SessionLocal()
    try:
        SyncService.initialize_defaults(db)
        seed_default_admin(db)
        try:
            SyncService.sync(db, full_recalc=True)
        except Exception as e:
            print(f"Startup initial sync skipped/failed: {e}")
    finally:
        db.close()
        
    # 3. Launch background sync timer
    await start_scheduler()
    
    yield
    
    # 4. Cleanup background scheduler task on shutdown
    await stop_scheduler()

app = FastAPI(
    title="Attendance Management System",
    description="Real-time Attendance Management System interfacing with ZKTeco uFace800",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API endpoints
app.include_router(auth_router)
app.include_router(router)


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "attendance-management"}

# Make sure static directories exist
os.makedirs("static/css", exist_ok=True)
os.makedirs("static/js", exist_ok=True)

# Mount front-end SPA
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8005, reload=True)
