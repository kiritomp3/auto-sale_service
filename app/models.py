from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


JobStatus = Literal["queued", "processing", "done", "failed"]

MetricFormat = Literal["currency", "integer", "decimal", "percent"]
MetricAvailability = Literal["public", "premium"]


class OzonMetricDefinition(BaseModel):
    id: str
    label: str
    group: str
    availability: MetricAvailability
    format: MetricFormat


class OzonMissingMetric(BaseModel):
    id: str
    label: str
    error: str


class OzonAnalyticsDimension(BaseModel):
    id: str
    name: str


class OzonAnalyticsProduct(BaseModel):
    dimensions: list[OzonAnalyticsDimension]
    metrics: dict[str, Optional[float]]


class OzonAnalyticsSource(BaseModel):
    date_from: str
    date_to: str
    dimensions: list[str]
    filters: list[dict[str, Any]]


class OzonAnalyticsRequest(BaseModel):
    date_from: str = Field(description="YYYY-MM-DD")
    date_to: str = Field(description="YYYY-MM-DD")
    dimension: list[str] = Field(default=["sku"])
    sku: str = Field(default="", description="Фильтр по SKU (опционально)")
    metrics: list[str] = Field(default=[], description="Список метрик (пусто = все)")


class OzonAnalyticsResponse(BaseModel):
    ok: bool = True
    source: OzonAnalyticsSource
    fetched_at: datetime
    metrics: list[OzonMetricDefinition]
    available_metrics: list[str]
    missing_metrics: list[OzonMissingMetric]
    warnings: list[str]
    totals: dict[str, Optional[float]]
    products: list[OzonAnalyticsProduct]


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
