from __future__ import annotations

import re
import time
from collections import defaultdict
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

# ---------------------------------------------------------------------------
# Метрики и их источники
#
# source="analytics" — /v1/analytics/data (подтверждено работающим)
# source="orders"    — вычисляется из /v3/posting/fbo+fbs/list (бесплатно)
# source="finance"   — вычисляется из /v3/finance/transaction/list (бесплатно)
# source="none"      — нет бесплатного источника (Ozon задепрекейтил)
# ---------------------------------------------------------------------------

METRIC_DEFINITIONS: list[OzonMetricDefinition] = [
    OzonMetricDefinition(id="revenue",             label="Заказано на сумму",                     group="Продажи",    availability="public",  format="currency"),
    OzonMetricDefinition(id="ordered_units",       label="Заказано товаров",                       group="Продажи",    availability="public",  format="integer"),
    OzonMetricDefinition(id="delivered_units",     label="Доставлено товаров",                     group="Статусы",    availability="public",  format="integer"),
    OzonMetricDefinition(id="cancellations",       label="Отмены",                                group="Статусы",    availability="public",  format="integer"),
    OzonMetricDefinition(id="returns",             label="Возвраты",                               group="Статусы",    availability="public",  format="integer"),
    OzonMetricDefinition(id="hits_view_search",    label="Показы в поиске и категории",            group="Показы",     availability="premium", format="integer"),
    OzonMetricDefinition(id="hits_view_pdp",       label="Показы на карточке товара",              group="Показы",     availability="premium", format="integer"),
    OzonMetricDefinition(id="hits_view",           label="Показы всего",                           group="Показы",     availability="premium", format="integer"),
    OzonMetricDefinition(id="hits_tocart_search",  label="В корзину из поиска и категории",        group="Корзина",    availability="premium", format="integer"),
    OzonMetricDefinition(id="hits_tocart_pdp",     label="В корзину из карточки товара",           group="Корзина",    availability="premium", format="integer"),
    OzonMetricDefinition(id="hits_tocart",         label="В корзину всего",                        group="Корзина",    availability="premium", format="integer"),
    OzonMetricDefinition(id="session_view_search", label="Сессии с показом в поиске и категории",  group="Посетители", availability="premium", format="integer"),
    OzonMetricDefinition(id="session_view_pdp",    label="Сессии с показом карточки товара",       group="Посетители", availability="premium", format="integer"),
    OzonMetricDefinition(id="session_view",        label="Сессии всего",                           group="Посетители", availability="premium", format="integer"),
    OzonMetricDefinition(id="conv_tocart_search",  label="Конверсия в корзину из поиска",          group="Конверсия",  availability="premium", format="percent"),
    OzonMetricDefinition(id="conv_tocart_pdp",     label="Конверсия в корзину из карточки",        group="Конверсия",  availability="premium", format="percent"),
    OzonMetricDefinition(id="conv_tocart",         label="Конверсия в корзину всего",              group="Конверсия",  availability="premium", format="percent"),
    OzonMetricDefinition(id="position_category",   label="Позиция в поиске и категории",           group="Видимость",  availability="premium", format="decimal"),
]

# Метрики, подтверждённо работающие через /v1/analytics/data
_ANALYTICS_API_METRICS = {"revenue", "ordered_units"}

# Метрики, которые Ozon задепрекейтил — нет бесплатного API-источника
_DEPRECATED_BY_OZON = {
    "hits_view_search", "hits_view_pdp", "hits_view",
    "hits_tocart_search", "hits_tocart_pdp", "hits_tocart",
    "session_view_search", "session_view_pdp", "session_view",
    "conv_tocart_search", "conv_tocart_pdp", "conv_tocart",
    "position_category",
}

# Метрики, вычисляемые из Orders API бесплатно
_ORDERS_METRICS = {"delivered_units", "cancellations", "ordered_units_orders", "revenue_orders"}
# Метрики, вычисляемые из Finance API
_FINANCE_METRICS = {"returns"}

_METRIC_BY_ID = {m.id: m for m in METRIC_DEFINITIONS}
_ALL_IDS = [m.id for m in METRIC_DEFINITIONS]

_ANALYTICS_PAGE_LIMIT = 1000
_ORDERS_PAGE_LIMIT = 1000
_FINANCE_PAGE_LIMIT = 1000
_MAX_ROWS = 10_000

_VALID_DIMENSIONS = {"sku", "spu", "title", "brand", "category1", "category2", "category3"}

# Статусы заказов для расчёта метрик
_DELIVERED_STATUSES = {"delivered"}
_CANCELLED_STATUSES = {"cancelled", "not_accepted", "arbitration", "client_arbitration"}

# Типы финансовых транзакций для возвратов
_RETURN_TRANSACTION_TYPES = {
    "MarketplaceReturnAfterDeliveryWriteOff",
    "MarketplaceReturnWriteOff",
    "ReturnAgentOperation",
    "ClientReturnAgentOperation",
}

_RATE_LIMIT_PAUSE = 0.35  # секунд между запросами к analytics/data


def _to_number_or_none(value: Any) -> float | None:
    try:
        n = float(value)
        return n if n == n else None
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

        rows: dict[str, dict[str, Any]] = {}
        totals: dict[str, float | None] = {}
        available: list[str] = []
        missing: list[OzonMissingMetric] = []
        warnings: list[str] = []

        # --- Источник 1: /v1/analytics/data ---
        analytics_ids = [mid for mid in requested_ids if mid in _ANALYTICS_API_METRICS]
        if analytics_ids:
            self._fetch_from_analytics_api(
                date_from, date_to, analytics_ids, dims, filters,
                rows, totals, available, missing, warnings,
            )

        # --- Источник 2: Orders API (FBO + FBS) ---
        orders_ids = [mid for mid in requested_ids if mid in {"delivered_units", "cancellations"}]
        if orders_ids:
            self._fetch_from_orders(
                date_from, date_to, sku,
                rows, totals, available, missing, warnings,
            )

        # --- Источник 3: Finance API (возвраты) ---
        if "returns" in requested_ids:
            self._fetch_returns(
                date_from, date_to,
                rows, totals, available, missing, warnings,
            )

        # --- Метрики без бесплатного источника ---
        for mid in requested_ids:
            if mid in _DEPRECATED_BY_OZON and mid not in available:
                missing.append(OzonMissingMetric(
                    id=mid,
                    label=_METRIC_BY_ID[mid].label,
                    error="Ozon задепрекейтил эту метрику в бесплатном API. "
                          "Доступна только с подпиской на Ozon Analytics Premium.",
                ))

        products = [
            OzonAnalyticsProduct(
                dimensions=[
                    OzonAnalyticsDimension(id=d.get("id", ""), name=d.get("name", ""))
                    for d in row.get("dimensions", [])
                ],
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

    # ------------------------------------------------------------------
    # Источник 1 — /v1/analytics/data
    # ------------------------------------------------------------------

    def _fetch_from_analytics_api(
        self,
        date_from: str,
        date_to: str,
        metric_ids: list[str],
        dims: list[str],
        filters: list[dict[str, Any]],
        rows: dict,
        totals: dict,
        available: list,
        missing: list,
        warnings: list,
    ) -> None:
        sort_key = "revenue" if "revenue" in metric_ids else metric_ids[0]
        sort = [{"key": sort_key, "order": "DESC"}]
        offset = 0
        while offset < _MAX_ROWS:
            time.sleep(_RATE_LIMIT_PAUSE)
            try:
                resp = self._client.get_analytics_data(
                    date_from=date_from,
                    date_to=date_to,
                    metrics=metric_ids,
                    dimension=dims,
                    filters=filters,
                    sort=sort,
                    limit=_ANALYTICS_PAGE_LIMIT,
                    offset=offset,
                )
            except httpx.HTTPStatusError as exc:
                try:
                    detail = exc.response.json()
                except Exception:
                    detail = exc.response.text
                for mid in metric_ids:
                    missing.append(OzonMissingMetric(
                        id=mid,
                        label=_METRIC_BY_ID[mid].label,
                        error=f"Ozon API {exc.response.status_code}: {detail}",
                    ))
                return
            except httpx.HTTPError as exc:
                for mid in metric_ids:
                    missing.append(OzonMissingMetric(
                        id=mid,
                        label=_METRIC_BY_ID[mid].label,
                        error=f"Сеть: {exc}",
                    ))
                return

            result = resp.get("result") or {}
            data_rows = result.get("data") or []
            raw_totals = result.get("totals") or []

            for row in data_rows:
                dimensions = row.get("dimensions") or []
                key = "|".join(f"{d.get('id', '')}:{d.get('name', '')}" for d in dimensions) or "total"
                record = rows.setdefault(key, {"dimensions": dimensions, "metrics": {}})
                values = row.get("metrics") or []
                for i, mid in enumerate(metric_ids):
                    record["metrics"][mid] = _to_number_or_none(values[i] if i < len(values) else None)

            for i, mid in enumerate(metric_ids):
                totals[mid] = _to_number_or_none(raw_totals[i] if i < len(raw_totals) else None)

            if data_rows and mid not in available:
                available.extend(metric_ids)

            if len(data_rows) < _ANALYTICS_PAGE_LIMIT:
                break
            offset += _ANALYTICS_PAGE_LIMIT

    # ------------------------------------------------------------------
    # Источник 2 — Orders API (FBO + FBS)
    # Считаем: delivered_units, cancellations
    # ------------------------------------------------------------------

    def _fetch_from_orders(
        self,
        date_from: str,
        date_to: str,
        sku_filter: str,
        rows: dict,
        totals: dict,
        available: list,
        missing: list,
        warnings: list,
    ) -> None:
        # sku_str → {delivered: int, cancelled: int}
        delivered: dict[str, int] = defaultdict(int)
        cancelled: dict[str, int] = defaultdict(int)
        sku_names: dict[str, str] = {}

        since = f"{date_from}T00:00:00Z"
        to = f"{date_to}T23:59:59Z"

        for schema in ("fbo", "fbs"):
            cursor = None
            fetched = 0
            while fetched < _MAX_ROWS:
                time.sleep(_RATE_LIMIT_PAUSE)
                try:
                    resp = self._client.list_postings(
                        schema=schema,
                        since=since,
                        to=to,
                        cursor=cursor,
                        limit=_ORDERS_PAGE_LIMIT,
                    )
                except (httpx.HTTPStatusError, httpx.HTTPError):
                    break

                postings = resp.get("postings") or []
                for posting in postings:
                    status = posting.get("status", "")
                    for product in posting.get("products") or []:
                        sku = str(product.get("sku") or "")
                        if not sku:
                            continue
                        if sku_filter and sku != sku_filter.strip():
                            continue
                        name = product.get("name") or ""
                        qty = int(product.get("quantity") or 0)
                        sku_names[sku] = sku_names.get(sku) or name
                        if status in _DELIVERED_STATUSES:
                            delivered[sku] += qty
                        elif status in _CANCELLED_STATUSES:
                            cancelled[sku] += qty

                fetched += len(postings)
                cursor = resp.get("cursor")
                if not resp.get("has_next") or not cursor:
                    break

        # Записываем в rows
        all_skus = set(delivered) | set(cancelled)
        for sku in all_skus:
            key = self._sku_key(sku, sku_names.get(sku, ""))
            record = rows.setdefault(key, {
                "dimensions": [{"id": sku, "name": sku_names.get(sku, "")}],
                "metrics": {},
            })
            record["metrics"]["delivered_units"] = float(delivered.get(sku, 0))
            record["metrics"]["cancellations"] = float(cancelled.get(sku, 0))

        totals["delivered_units"] = float(sum(delivered.values()))
        totals["cancellations"] = float(sum(cancelled.values()))

        if "delivered_units" not in available:
            available.append("delivered_units")
        if "cancellations" not in available:
            available.append("cancellations")

    # ------------------------------------------------------------------
    # Источник 3 — Finance API (возвраты)
    # ------------------------------------------------------------------

    def _fetch_returns(
        self,
        date_from: str,
        date_to: str,
        rows: dict,
        totals: dict,
        available: list,
        missing: list,
        warnings: list,
    ) -> None:
        returns: dict[str, int] = defaultdict(int)
        page = 1
        fetched = 0

        while fetched < _MAX_ROWS:
            time.sleep(_RATE_LIMIT_PAUSE)
            try:
                resp = self._client.list_finance_transactions(
                    date_from=f"{date_from}T00:00:00Z",
                    date_to=f"{date_to}T23:59:59Z",
                    page=page,
                    page_size=_FINANCE_PAGE_LIMIT,
                )
            except (httpx.HTTPStatusError, httpx.HTTPError) as exc:
                missing.append(OzonMissingMetric(
                    id="returns",
                    label="Возвраты",
                    error=f"Finance API недоступен: {exc}",
                ))
                return

            result = resp.get("result") or {}
            operations = result.get("operations") or []

            for op in operations:
                if op.get("operation_type") not in _RETURN_TRANSACTION_TYPES:
                    continue
                for item in op.get("items") or []:
                    sku = str(item.get("sku") or "")
                    if not sku:
                        continue
                    qty = abs(int(item.get("quantity") or 1))
                    returns[sku] += qty

            fetched += len(operations)
            page_count = result.get("page_count") or 1
            if page >= page_count:
                break
            page += 1

        for sku, qty in returns.items():
            key = self._sku_key(sku, "")
            record = rows.setdefault(key, {
                "dimensions": [{"id": sku, "name": ""}],
                "metrics": {},
            })
            record["metrics"]["returns"] = float(qty)

        totals["returns"] = float(sum(returns.values()))

        if "returns" not in available:
            available.append("returns")

    @staticmethod
    def _sku_key(sku: str, name: str) -> str:
        return f"{sku}:{name}"
