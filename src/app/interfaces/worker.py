from celery import Celery
import os

broker_url = os.getenv("CELERY_BROKER_URL")
backend_url = os.getenv("CELERY_BACKEND_URL")
celery_app = Celery(
    "tasks_api",
    broker=broker_url,
    backend=backend_url,
)
celery_app.autodiscover_tasks(["app.tasks"])
