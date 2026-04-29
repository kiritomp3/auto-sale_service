from typing import Any, Literal

from pydantic import BaseModel


JobStatus = Literal["queued", "processing", "done", "failed"]


class CardJobResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobState(BaseModel):
    job_id: str
    status: JobStatus
    input: dict[str, Any] | None = None
    analysis: dict[str, Any] | None = None
    cards: list[str] | None = None
    listing_content: dict[str, Any] | None = None
    error: str | None = None

