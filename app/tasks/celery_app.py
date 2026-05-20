"""
Celery configuration and app initialization
"""

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_postrun, task_prerun

from app.core.config import settings

# Create Celery app
celery_app = Celery(
    "requi_health",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.ingestion",
        "app.tasks.gap_resolution",
        "app.tasks.daily_update",
        "app.tasks.trial_emails",
    ],
)

# Celery configuration (beat crontab hours use trial_reminder_timezone below)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone=settings.trial_reminder_timezone,
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,  # 1 hour
    worker_prefetch_multiplier=1,
    worker_concurrency=settings.celery_worker_concurrency,
)


@task_prerun.connect
def task_prerun_handler(task_id, task, args, kwargs, **extras):
    """Log task start"""
    print(f"Starting task {task.name}[{task_id}]")


@task_postrun.connect
def task_postrun_handler(task_id, task, args, kwargs, retval, state, **extras):
    """Log task completion"""
    print(f"Task {task.name}[{task_id}] finished with state: {state}")


# Beat schedule for periodic tasks (hour/minute in celery_app.conf.timezone)
_trial_reminder_schedule = crontab(
    hour=settings.trial_reminder_cron_hour,
    minute=settings.trial_reminder_cron_minute,
)

celery_app.conf.beat_schedule = {
    "daily-knowledge-update": {
        "task": "app.tasks.daily_update.run_daily_update",
        "schedule": 86400.0,  # 24 hours
    },
    "trial-reminder-two-days-left": {
        "task": "app.tasks.trial_emails.send_trial_two_day_reminders",
        "schedule": _trial_reminder_schedule,
    },
}
