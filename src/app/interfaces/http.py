from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

router = APIRouter()


class TaskCreateIn(BaseModel):
    text: str
    language: str = "en"


class TaskOut(BaseModel):
    task_id: str
    status: str
    queued_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result_url: str | None = None
    error: str | None = None


@router.post("/", response_model=TaskOut, status_code=201)
async def submit_task(
    payload: TaskCreateIn, idempotency_key: str | None = Header(default=None)
):
    # TODO: call application layer
    raise HTTPException(501, "Not implemented yet")
