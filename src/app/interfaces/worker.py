from celery import Celery

celery_app = Celery(
    "tasks_api",
    broker="amqp://guest:guest@localhost:5672//",
    backend="db+postgresql://user:pass@localhost:5432/tasks",
)
celery_app.autodiscover_tasks(["app.tasks"])
