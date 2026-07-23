import asyncio
import datetime
from database import SessionLocal
from models import DeviceSettings
from sync_service import SyncService
import logging

logger = logging.getLogger("Scheduler")

_scheduler_task = None
_should_run = True

async def start_scheduler():
    global _scheduler_task, _should_run
    _should_run = True
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(_scheduler_loop())
        logger.info("Background sync scheduler started.")

async def stop_scheduler():
    global _should_run, _scheduler_task
    _should_run = False
    if _scheduler_task:
        logger.info("Signaled background sync scheduler to stop.")
        # Wait up to 3 seconds for task to stop cleanly
        for _ in range(3):
            if _scheduler_task.done():
                break
            await asyncio.sleep(1)
        _scheduler_task = None

async def _scheduler_loop():
    logger.info("Background sync scheduler loop initialized.")
    while _should_run:
        db = SessionLocal()
        try:
            settings = db.query(DeviceSettings).first()
            if not settings:
                settings, _ = SyncService.initialize_defaults(db)
            
            interval = settings.sync_interval_minutes or 5
            last_sync = settings.last_sync_time
            
            due = False
            if not last_sync:
                due = True
            else:
                elapsed = (datetime.datetime.now() - last_sync).total_seconds()
                if elapsed >= (interval * 60):
                    due = True
            
            if due:
                logger.info("Sync scheduler: Starting periodic device/simulator sync...")
                # Sync logic includes user update, raw log import, and daily calculation
                result = SyncService.sync(db)
                logger.info(f"Sync scheduler: Complete. Details: {result}")
        except Exception as e:
            logger.error(f"Error in background sync scheduler: {e}")
        finally:
            db.close()
            
        # Poll settings every 5 seconds, checking if we should shutdown
        for _ in range(5):
            if not _should_run:
                break
            await asyncio.sleep(1)
