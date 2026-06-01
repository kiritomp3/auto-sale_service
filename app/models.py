from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


JobStatus = Literal["queued", "processing", "done", "failed"]

MetricFormat = Literal["currency", "integer", "decimal", "percent"]
MetricAvailability = Literal["public", "premium"]

DirectionType = Literal["up", "down", "stable", "unknown"]
AlertSeverityType = Literal["ok", "warning", "critical"]
HealthLabelType = Literal["excellent", "good", "warning", "critical"]


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


# ---------------------------------------------------------------------------
# Insights / analytics comparison models
# ---------------------------------------------------------------------------

CardTipCategory = Literal["photo", "title", "keywords", "description", "price", "stock", "logistics", "reviews"]


class CardOptimizationTip(BaseModel):
    id: str
    priority: int
    category: CardTipCategory
    emoji: str
    title: str
    issue: str
    action: str
    expected_result: str
    metric_evidence: list[str]
    requires_premium: bool

class MetricDelta(BaseModel):
    metric_id: str
    label: str
    current: Optional[float]
    previous: Optional[float]
    delta_abs: Optional[float]
    delta_pct: Optional[float]
    direction: DirectionType
    is_improvement: bool


class DerivedKPI(BaseModel):
    id: str
    label: str
    description: str
    current: Optional[float]
    previous: Optional[float]
    delta_pct: Optional[float]
    format: MetricFormat
    direction: DirectionType


class MetricAlert(BaseModel):
    id: str
    severity: AlertSeverityType
    emoji: str
    title: str
    detail: str
    metric_ids: list[str]


class MetricRecommendation(BaseModel):
    priority: int
    title: str
    action: str
    expected_impact: str
    affected_metrics: list[str]


class ProductInsight(BaseModel):
    sku: str
    name: str
    health_score: int
    revenue: Optional[float]
    ordered_units: Optional[float]
    cancellation_rate: Optional[float]
    return_rate: Optional[float]
    revenue_delta_pct: Optional[float]
    alert_level: AlertSeverityType


class OzonInsightsResponse(BaseModel):
    ok: bool = True
    client_id: str
    current_period: OzonAnalyticsSource
    previous_period: Optional[OzonAnalyticsSource]
    premium_available: bool
    health_score: int
    health_label: HealthLabelType
    summary: str
    deltas: list[MetricDelta]
    derived_kpis: list[DerivedKPI]
    alerts: list[MetricAlert]
    recommendations: list[MetricRecommendation]
    card_tips: list[CardOptimizationTip]
    top_products: list[ProductInsight]
    bottom_products: list[ProductInsight]
    analyzed_at: datetime


class OzonCompareRequest(BaseModel):
    current_date_from: str = Field(description="YYYY-MM-DD")
    current_date_to: str = Field(description="YYYY-MM-DD")
    previous_date_from: Optional[str] = Field(default=None, description="YYYY-MM-DD, авто если не указано")
    previous_date_to: Optional[str] = Field(default=None, description="YYYY-MM-DD, авто если не указано")
    sku: str = Field(default="", description="Фильтр по SKU")


class OzonAIChatRequest(BaseModel):
    current_date_from: str = Field(description="YYYY-MM-DD")
    current_date_to: str = Field(description="YYYY-MM-DD")
    question: str = Field(default="", description="Вопрос пользователя (пусто = общий анализ)")
    sku: str = Field(default="")


class OzonAIChatResponse(BaseModel):
    ok: bool = True
    answer: str
    client_id: str
    period: str
    analyzed_at: datetime


class OzonHistoryEntry(BaseModel):
    date_from: str
    date_to: str
    fetched_at: str
    snapshot_key: str


class OzonHistoryResponse(BaseModel):
    ok: bool = True
    client_id: str
    snapshots: list[OzonHistoryEntry]
    total: int
