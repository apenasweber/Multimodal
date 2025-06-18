from fastapi import FastAPI

from app.interfaces.http import router as task_router

app = FastAPI(title="Async Tasks API", version="0.1.0")
app.include_router(task_router, prefix="/tasks")
