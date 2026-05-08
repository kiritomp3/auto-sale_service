from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


JobStatus = Literal["queued", "processing", "done", "failed"]


class CardJobResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobState(BaseModel):
    job_id: str
    status: JobStatus
    input: Optional[dict[str, Any]] = None
    analysis: Optional[dict[str, Any]] = None
    cards: Optional[list[str]] = None
    listing_content: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class OzonAuthLoginRequest(BaseModel):
    ozon_client_id: str = Field(min_length=1)
    ozon_api_key: str = Field(min_length=1)


class OzonAuthLoginResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    expires_at: datetime


class OzonAuthLogoutResponse(BaseModel):
    ok: bool = True


class OzonDraftCreateRequest(BaseModel):
    items: list[dict[str, Any]] = Field(
        min_length=1,
        description="Список товаров в формате Ozon /v2/product/import -> items",
    )


class OzonDraftCreateResponse(BaseModel):
    result: dict[str, Any]
