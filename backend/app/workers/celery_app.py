from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "releaseradar",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)
celery_app.conf.update(
    task_acks_late=True,  # re-deliver if a worker dies mid-review
    worker_prefetch_multiplier=1,  # reviews are long; don't hoard tasks
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
)
