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
# Определения метрик
#
# availability="public"  — бесплатно через /v1/analytics/data
# availability="premium" — требует Ozon Analytics Premium (те же имена, но
#                          API возвращает 400 "deprecated metrics used" без подписки)
#
# Для premium-метрик есть fallback-источники (Orders/Finance API):
#   fallback="orders"  — вычисляется из /v3/posting/fbo+fbs/list
#   fallback="finance" — вычисляется из /v3/finance/transaction/list
#   fallback=None      — бесплатного источника нет
# ---------------------------------------------------------------------------

METRIC_DEFINITIONS: list[OzonMetricDefinition] = [
    OzonMetricDefinition(id="revenue",             label="Заказано на сумму",                     group="Продажи",    availability="public",  format="currency"),
    OzonMetricDefinition(id="ordered_units",       label="Заказано товаров",                       group="Продажи",    availability="public",  format="integer"),
    OzonMetricDefinition(id="delivered_units",     label="Доставлено товаров",                     group="Статусы",    availability="premium", format="integer"),
    OzonMetricDefinition(id="cancellations",       label="Отмены",                                group="Статусы",    availability="premium", format="integer"),
    OzonMetricDefinition(id="returns",             label="Возвраты",                               group="Статусы",    availability="premium", format="integer"),
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

# Метрики с бесплатным fallback через Orders API
_ORDERS_FALLBACK = {"delivered_units", "cancellations"}
# Метрики с бесплатным fallback через Finance API
_FINANCE_FALLBACK = {"returns"}
# Метрики без какого-либо бесплатного источника (только Analytics Premium)
_PREMIUM_ONLY = {
    "hits_view_search", "hits_view_pdp", "hits_view",
    "hits_tocart_search", "hits_tocart_pdp", "hits_tocart",
    "session_view_search", "session_view_pdp", "session_view",
    "conv_tocart_search", "conv_tocart_pdp", "conv_tocart",
    "position_category",
}

_METRIC_BY_ID = {m.id: m for m in METRIC_DEFINITIONS}
_ALL_IDS = [m.id for m in METRIC_DEFINITIONS]

# Ozon принимает не более 14 метрик за запрос
_MAX_METRICS_PER_REQUEST = 14
_ANALYTICS_PAGE_LIMIT = 1000
_ORDERS_PAGE_LIMIT = 1000
_FINANCE_PAGE_LIMIT = 1000
_MAX_ROWS = 10_000

_VALID_DIMENSIONS = {"sku", "spu", "title", "brand", "category1", "category2", "category3"}

# Статусы заказов
_DELIVERED_STATUSES = {"delivered"}
_CANCELLED_STATUSES = {"cancelled", "not_accepted", "arbitration", "client_arbitration"}

# Типы финансовых транзакций для возвратов
_RETURN_TRANSACTION_TYPES = {
    "MarketplaceReturnAfterDeliveryWriteOff",
    "MarketplaceReturnWriteOff",
    "ReturnAgentOperation",
    "ClientReturnAgentOperation",
}

_RATE_LIMIT_PAUSE = 0.35


def _chunk(lst: list, size: int) -> list[list]:
    return [lst[i:i + size] for i in range(0, len(lst), size)]


def _to_number_or_none(value: Any) -> float | None:
    try:
        n = float(value)
        return n if n == n else None
    except (TypeError, ValueError):
        return None


def _is_premium_error(exc: httpx.HTTPStatusError) -> bool:
    """Ozon возвращает 400 'deprecated metrics used' когда метрика требует Premium."""
    if exc.response.status_code != 400:
        return False
    try:
        msg = exc.response.json().get("message", "")
        return "deprecated" in msg.lower()
    except Exception:
        return False


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

        # --- Шаг 1: пробуем ВСЕ метрики через /v1/analytics/data ---
        # Если есть Premium — получим их все здесь.
        # Если нет — часть упадёт с ошибкой "deprecated metrics used".
        analytics_failed: set[str] = set()
        self._fetch_via_analytics_api(
            date_from, date_to, requested_ids, dims, filters,
            rows, totals, available, analytics_failed, warnings,
        )

        # --- Шаг 2: для упавших метрик используем бесплатные источники ---
        needs_orders = analytics_failed & (_ORDERS_FALLBACK & set(requested_ids))
        needs_finance = analytics_failed & (_FINANCE_FALLBACK & set(requested_ids))

        if needs_orders:
            self._fetch_from_orders(date_from, date_to, sku, rows, totals, available, missing, warnings)

        if needs_finance:
            self._fetch_returns(date_from, date_to, rows, totals, available, missing, warnings)

        # --- Шаг 3: метрики без бесплатного fallback — честно говорим почему ---
        for mid in analytics_failed:
            if mid in _PREMIUM_ONLY and mid not in available:
                missing.append(OzonMissingMetric(
                    id=mid,
                    label=_METRIC_BY_ID[mid].label,
                    error=(
                        "Требует подписку Ozon Analytics Premium. "
                        "При наличии подписки метрика автоматически появится в результатах."
                    ),
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
    # Analytics API — пробуем всё, чанки по 14
    # ------------------------------------------------------------------

    def _fetch_via_analytics_api(
        self,
        date_from: str,
        date_to: str,
        metric_ids: list[str],
        dims: list[str],
        filters: list[dict[str, Any]],
        rows: dict,
        totals: dict,
        available: list,
        failed: set,
        warnings: list,
    ) -> None:
        sort_key = next((m for m in ("revenue", "ordered_units") if m in metric_ids), metric_ids[0])

        for chunk in _chunk(metric_ids, _MAX_METRICS_PER_REQUEST):
            chunk_sort = [{"key": sort_key, "order": "DESC"}] if sort_key in chunk else [{"key": chunk[0], "order": "DESC"}]
            try:
                self._fetch_analytics_chunk(
                    date_from, date_to, chunk, dims, filters, chunk_sort, rows, totals,
                )
                available.extend(m for m in chunk if m not in available)
            except httpx.HTTPStatusError as exc:
                if _is_premium_error(exc) and len(chunk) > 1:
                    # Разбиваем чанк — внутри могут быть и бесплатные, и Premium метрики
                    for mid in chunk:
                        time.sleep(_RATE_LIMIT_PAUSE)
                        try:
                            self._fetch_analytics_chunk(
                                date_from, date_to, [mid], dims, filters,
                                [{"key": mid, "order": "DESC"}], rows, totals,
                            )
                            if mid not in available:
                                available.append(mid)
                        except httpx.HTTPStatusError as exc2:
                            if _is_premium_error(exc2):
                                failed.add(mid)
                            else:
                                failed.add(mid)
                                warnings.append(f"{mid}: Ozon API {exc2.response.status_code}")
                        except httpx.HTTPError as exc2:
                            failed.add(mid)
                            warnings.append(f"{mid}: сеть — {exc2}")
                else:
                    for mid in chunk:
                        failed.add(mid)
                    if not _is_premium_error(exc):
                        try:
                            detail = exc.response.json()
                        except Exception:
                            detail = exc.response.text
                        warnings.append(f"Analytics API {exc.response.status_code}: {detail}")
            except httpx.HTTPError as exc:
                for mid in chunk:
                    failed.add(mid)
                warnings.append(f"Сеть: {exc}")

    def _fetch_analytics_chunk(
        self,
        date_from: str,
        date_to: str,
        metric_ids: list[str],
        dims: list[str],
        filters: list[dict[str, Any]],
        sort: list[dict[str, Any]],
        rows: dict,
        totals: dict,
    ) -> None:
        offset = 0
        while offset < _MAX_ROWS:
            time.sleep(_RATE_LIMIT_PAUSE)
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

            if len(data_rows) < _ANALYTICS_PAGE_LIMIT:
                break
            offset += _ANALYTICS_PAGE_LIMIT

    # ------------------------------------------------------------------
    # Fallback: Orders API → delivered_units, cancellations
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
                        schema=schema, since=since, to=to,
                        cursor=cursor, limit=_ORDERS_PAGE_LIMIT,
                    )
                except (httpx.HTTPStatusError, httpx.HTTPError) as exc:
                    warnings.append(f"Orders API ({schema}): {exc}")
                    break

                for posting in resp.get("postings") or []:
                    status = posting.get("status", "")
                    for product in posting.get("products") or []:
                        sku = str(product.get("sku") or "")
                        if not sku or (sku_filter.strip() and sku != sku_filter.strip()):
                            continue
                        qty = int(product.get("quantity") or 0)
                        sku_names.setdefault(sku, product.get("name") or "")
                        if status in _DELIVERED_STATUSES:
                            delivered[sku] += qty
                        elif status in _CANCELLED_STATUSES:
                            cancelled[sku] += qty

                fetched += len(resp.get("postings") or [])
                cursor = resp.get("cursor")
                if not resp.get("has_next") or not cursor:
                    break

        for sku in set(delivered) | set(cancelled):
            key = f"{sku}:{sku_names.get(sku, '')}"
            record = rows.setdefault(key, {
                "dimensions": [{"id": sku, "name": sku_names.get(sku, "")}],
                "metrics": {},
            })
            record["metrics"]["delivered_units"] = float(delivered.get(sku, 0))
            record["metrics"]["cancellations"] = float(cancelled.get(sku, 0))

        totals["delivered_units"] = float(sum(delivered.values()))
        totals["cancellations"] = float(sum(cancelled.values()))
        for mid in ("delivered_units", "cancellations"):
            if mid not in available:
                available.append(mid)

    # ------------------------------------------------------------------
    # Fallback: Finance API → returns
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
        while True:
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
                    id="returns", label="Возвраты",
                    error=f"Finance API недоступен: {exc}",
                ))
                return

            result = resp.get("result") or {}
            for op in result.get("operations") or []:
                if op.get("operation_type") not in _RETURN_TRANSACTION_TYPES:
                    continue
                for item in op.get("items") or []:
                    sku = str(item.get("sku") or "")
                    if sku:
                        returns[sku] += abs(int(item.get("quantity") or 1))

            if page >= (result.get("page_count") or 1):
                break
            page += 1

        for sku, qty in returns.items():
            key = f"{sku}:"
            record = rows.setdefault(key, {
                "dimensions": [{"id": sku, "name": ""}],
                "metrics": {},
            })
            record["metrics"]["returns"] = float(qty)

        totals["returns"] = float(sum(returns.values()))
        if "returns" not in available:
            available.append("returns")
