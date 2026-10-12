from app.workers.celery_app import celery_app


@celery_app.task(name="releaseradar.ping")  # type: ignore[untyped-decorator]
def ping() -> str:
    """Smoke-test task; real review tasks arrive in M1/M2."""
    return "pong"
