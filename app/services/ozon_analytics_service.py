from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx

from app.clients.ozon_seller_client import OzonSellerClient
from app.models import (
    OzonAnalyticsDimension,
    OzonAnalyticsProduct,
    OzonAnalyticsResponse,
    OzonAnalyticsSource,
    OzonMetricDefinition,
    OzonMissingMetric,
)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Все метрики в том же порядке, что на фронтенде
METRIC_DEFINITIONS: list[OzonMetricDefinition] = [
    OzonMetricDefinition(id="revenue",              label="Заказано на сумму",                        group="Продажи",    availability="public",  format="currency"),
    OzonMetricDefinition(id="ordered_units",        label="Заказано товаров",                          group="Продажи",    availability="public",  format="integer"),
    OzonMetricDefinition(id="hits_view_search",     label="Показы в поиске и категории",               group="Показы",     availability="premium", format="integer"),
    OzonMetricDefinition(id="hits_view_pdp",        label="Показы на карточке товара",                 group="Показы",     availability="premium", format="integer"),
    OzonMetricDefinition(id="hits_view",            label="Показы всего",                              group="Показы",     availability="premium", format="integer"),
    OzonMetricDefinition(id="hits_tocart_search",   label="В корзину из поиска и категории",           group="Корзина",    availability="premium", format="integer"),
    OzonMetricDefinition(id="hits_tocart_pdp",      label="В корзину из карточки товара",              group="Корзина",    availability="premium", format="integer"),
    OzonMetricDefinition(id="hits_tocart",          label="В корзину всего",                           group="Корзина",    availability="premium", format="integer"),
    OzonMetricDefinition(id="session_view_search",  label="Сессии с показом в поиске и категории",     group="Посетители", availability="premium", format="integer"),
    OzonMetricDefinition(id="session_view_pdp",     label="Сессии с показом карточки товара",          group="Посетители", availability="premium", format="integer"),
    OzonMetricDefinition(id="session_view",         label="Сессии всего",                              group="Посетители", availability="premium", format="integer"),
    OzonMetricDefinition(id="conv_tocart_search",   label="Конверсия в корзину из поиска",             group="Конверсия",  availability="premium", format="percent"),
    OzonMetricDefinition(id="conv_tocart_pdp",      label="Конверсия в корзину из карточки",           group="Конверсия",  availability="premium", format="percent"),
    OzonMetricDefinition(id="conv_tocart",          label="Конверсия в корзину всего",                 group="Конверсия",  availability="premium", format="percent"),
    OzonMetricDefinition(id="returns",              label="Возвраты",                                  group="Статусы",    availability="premium", format="integer"),
    OzonMetricDefinition(id="cancellations",        label="Отмены",                                    group="Статусы",    availability="premium", format="integer"),
    OzonMetricDefinition(id="delivered_units",      label="Доставлено товаров",                        group="Статусы",    availability="premium", format="integer"),
    OzonMetricDefinition(id="position_category",    label="Позиция в поиске и категории",              group="Видимость",  availability="premium", format="decimal"),
]

_METRIC_BY_ID = {m.id: m for m in METRIC_DEFINITIONS}
_ALL_IDS = [m.id for m in METRIC_DEFINITIONS]
_MAX_METRICS_PER_REQUEST = 14
_PAGE_LIMIT = 1000
_MAX_ROWS = 10_000

_VALID_DIMENSIONS = {"sku", "spu", "title", "brand", "category1", "category2", "category3"}


def _chunk(lst: list, size: int) -> list[list]:
    return [lst[i:i + size] for i in range(0, len(lst), size)]


def _to_number_or_none(value: Any) -> float | None:
    try:
        n = float(value)
        return n if n == n else None  # filter NaN
    except (TypeError, ValueError):
        return None


class OzonAnalyticsService:
    def __init__(self, client: OzonSellerClient):
        self._client = client

    def fetch(
        self,
        date_from: str,
        date_to: str,
        dimension: list[str],
        sku: str = "",
        metric_ids: list[str] | None = None,
    ) -> OzonAnalyticsResponse:
        if not _DATE_RE.match(date_from) or not _DATE_RE.match(date_to):
            raise ValueError("Даты должны быть в формате YYYY-MM-DD")
        if date_from > date_to:
            raise ValueError("date_from не может быть позже date_to")

        dims = [d for d in dimension if d in _VALID_DIMENSIONS] or ["sku"]
        requested_ids = [mid for mid in (metric_ids or _ALL_IDS) if mid in _METRIC_BY_ID] or _ALL_IDS

        filters: list[dict[str, Any]] = []
        if sku.strip():
            filters.append({"key": "sku", "operation": "EQ", "value": sku.strip()})

        sort_key = "revenue" if "revenue" in requested_ids else requested_ids[0]
        sort = [{"key": sort_key, "order": "DESC"}]

        rows: dict[str, dict[str, Any]] = {}
        totals: dict[str, float | None] = {}
        available: list[str] = []
        missing: list[OzonMissingMetric] = []
        warnings: list[str] = []

        for chunk in _chunk(requested_ids, _MAX_METRICS_PER_REQUEST):
            chunk_sort = [s for s in sort if s["key"] in chunk or s["key"] in dims]
            if not chunk_sort:
                chunk_sort = [{"key": chunk[0], "order": "DESC"}]

            try:
                self._fetch_chunk(
                    date_from, date_to, chunk, dims, filters, chunk_sort,
                    rows, totals,
                )
                available.extend(chunk)
            except Exception as exc:
                if len(chunk) == 1:
                    mid = chunk[0]
                    missing.append(OzonMissingMetric(
                        id=mid,
                        label=_METRIC_BY_ID[mid].label,
                        error=str(exc),
                    ))
                    warnings.append(f"Метрика {mid} недоступна: {exc}")
                else:
                    # Распадаем чанк на одиночные запросы
                    for mid in chunk:
                        try:
                            self._fetch_chunk(
                                date_from, date_to, [mid], dims, filters,
                                [{"key": mid, "order": "DESC"}],
                                rows, totals,
                            )
                            available.append(mid)
                        except Exception as exc2:
                            missing.append(OzonMissingMetric(
                                id=mid,
                                label=_METRIC_BY_ID[mid].label,
                                error=str(exc2),
                            ))
                            warnings.append(f"Метрика {mid} недоступна: {exc2}")

        products = [
            OzonAnalyticsProduct(
                dimensions=[OzonAnalyticsDimension(id=d.get("id", ""), name=d.get("name", "")) for d in row.get("dimensions", [])],
                metrics=row.get("metrics", {}),
            )
            for row in rows.values()
        ]

        return OzonAnalyticsResponse(
            source=OzonAnalyticsSource(
                date_from=date_from,
                date_to=date_to,
                dimensions=dims,
                filters=filters,
            ),
            fetched_at=datetime.now(timezone.utc),
            metrics=[_METRIC_BY_ID[mid] for mid in requested_ids if mid in _METRIC_BY_ID],
            available_metrics=available,
            missing_metrics=missing,
            warnings=warnings,
            totals=totals,
            products=products,
        )

    def _fetch_chunk(
        self,
        date_from: str,
        date_to: str,
        metric_ids: list[str],
        dims: list[str],
        filters: list[dict[str, Any]],
        sort: list[dict[str, Any]],
        rows: dict[str, dict[str, Any]],
        totals: dict[str, float | None],
    ) -> None:
        offset = 0
        while offset < _MAX_ROWS:
            try:
                resp = self._client.get_analytics_data(
                    date_from=date_from,
                    date_to=date_to,
                    metrics=metric_ids,
                    dimension=dims,
                    filters=filters,
                    sort=sort,
                    limit=_PAGE_LIMIT,
                    offset=offset,
                )
            except httpx.HTTPStatusError as exc:
                try:
                    detail = exc.response.json()
                except Exception:
                    detail = exc.response.text
                raise RuntimeError(f"Ozon API {exc.response.status_code}: {detail}") from exc
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Сеть: {exc}") from exc

            result = resp.get("result") or {}
            data_rows = result.get("data") or []
            raw_totals = result.get("totals") or []

            for row in data_rows:
                dimensions = row.get("dimensions") or []
                key = "|".join(f"{d.get('id', '')}:{d.get('name', '')}" for d in dimensions) or "total"
                record = rows.get(key) or {"dimensions": dimensions, "metrics": {}}
                values = row.get("metrics") or []
                for i, mid in enumerate(metric_ids):
                    record["metrics"][mid] = _to_number_or_none(values[i] if i < len(values) else None)
                rows[key] = record

            for i, mid in enumerate(metric_ids):
                totals[mid] = _to_number_or_none(raw_totals[i] if i < len(raw_totals) else None)

            if len(data_rows) < _PAGE_LIMIT:
                break
            offset += _PAGE_LIMIT
