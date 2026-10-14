# app/services/background_runner.py
import threading
import time
import logging
from flask import Flask
from app.services.slot_engine import expire_stale_holds
from app.services.notifier import send_reminders

logger = logging.getLogger(__name__)
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


def run_maintenance_cycle(app: Flask) -> dict:
    """Executes one full pass of stale hold cleanup and WhatsApp reminder dispatch."""
    with app.app_context():
        expired_count = expire_stale_holds()
        reminder_stats = send_reminders()
        return {
            "expired_holds_cleaned": expired_count,
            **reminder_stats,
        }


def start_background_scheduler(app: Flask, interval_seconds: int = 300) -> None:
    """Starts a non-blocking daemon thread that runs maintenance every 5 minutes."""
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True

    def _loop():
        logger.info(f"SlotEngine Background Scheduler started (interval={interval_seconds}s)")
        while True:
            try:
                stats = run_maintenance_cycle(app)
                logger.info(f"Background maintenance cycle completed: {stats}")
            except Exception as exc:
                logger.error(f"Background maintenance error: {exc}")
            time.sleep(interval_seconds)

    thread = threading.Thread(target=_loop, daemon=True, name="SlotEngineMaintenanceWorker")
    thread.start()