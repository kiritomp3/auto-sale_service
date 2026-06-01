from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from app.models import (
    AlertSeverityType,
    CardOptimizationTip,
    DerivedKPI,
    DirectionType,
    MetricAlert,
    MetricDelta,
    MetricRecommendation,
    OzonAnalyticsSource,
    OzonInsightsResponse,
    ProductInsight,
)
from app.repositories.metrics_snapshot_repository import MetricsSnapshot, RedisMetricsSnapshotRepository

if TYPE_CHECKING:
    from app.services.ozon_analytics_service import OzonAnalyticsService

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

_NEGATIVE_METRICS = {"cancellations", "returns", "position_category"}

_ALL_METRIC_LABELS: dict[str, str] = {
    # Бесплатные
    "revenue":             "Заказано на сумму",
    "ordered_units":       "Заказано товаров",
    "delivered_units":     "Доставлено товаров",
    "cancellations":       "Отмены",
    "returns":             "Возвраты",
    # Premium — показы
    "hits_view_search":    "Показы в поиске",
    "hits_view_pdp":       "Просмотры карточки",
    "hits_view":           "Показы всего",
    # Premium — корзина
    "hits_tocart_search":  "В корзину из поиска",
    "hits_tocart_pdp":     "В корзину с карточки",
    "hits_tocart":         "В корзину всего",
    # Premium — сессии
    "session_view_search": "Сессии из поиска",
    "session_view_pdp":    "Сессии на карточке",
    "session_view":        "Сессии всего",
    # Premium — конверсия
    "conv_tocart_search":  "Конверсия из поиска в корзину",
    "conv_tocart_pdp":     "Конверсия с карточки в корзину",
    "conv_tocart":         "Конверсия в корзину",
    # Premium — позиция
    "position_category":   "Позиция в поиске",
}

_PREMIUM_KEYS = {
    "hits_view_search", "hits_view_pdp", "hits_view",
    "hits_tocart_search", "hits_tocart_pdp", "hits_tocart",
    "session_view_search", "session_view_pdp", "session_view",
    "conv_tocart_search", "conv_tocart_pdp", "conv_tocart",
    "position_category",
}

# Пороги — базовые
_REV_WARN = -0.10
_REV_CRIT = -0.20
_OU_WARN  = -0.15
_OU_CRIT  = -0.30
_CR_WARN  = 0.15
_CR_CRIT  = 0.25
_RR_WARN  = 0.08
_RR_CRIT  = 0.15
_DR_WARN  = 0.75
_DR_CRIT  = 0.60

# Пороги — воронка (Premium)
_SEARCH_CTR_LOW  = 0.03   # < 3% — плохой CTR в поиске
_SEARCH_CTR_MED  = 0.05
_PDC_LOW         = 0.05   # < 5% — плохая конверсия карточки в корзину
_PDC_MED         = 0.10
_CTO_LOW         = 0.60   # < 60% — плохая конверсия корзины в заказ
_POS_CRIT        = 50     # позиция > 50 — критично
_POS_WARN        = 20     # позиция > 20 — есть потенциал


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _rate(a: float | None, b: float | None) -> float | None:
    if a is not None and b is not None and b > 0:
        return a / b
    return None


def _pct_delta(curr_v: float | None, prev_v: float | None) -> float | None:
    if curr_v is not None and prev_v is not None and prev_v != 0:
        return (curr_v - prev_v) / abs(prev_v)
    return None


def _direction(delta: float | None, threshold: float = 0.01) -> DirectionType:
    if delta is None:
        return "unknown"
    if abs(delta) < threshold:
        return "stable"
    return "up" if delta > 0 else "down"


def _has_premium(totals: dict) -> bool:
    return any(
        k in totals and totals[k] is not None
        for k in _PREMIUM_KEYS
    )


# ---------------------------------------------------------------------------
# Сервис
# ---------------------------------------------------------------------------

class OzonInsightsService:
    def __init__(self, snapshot_repository: RedisMetricsSnapshotRepository):
        self._repo = snapshot_repository

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def get_history(self, client_id: str, limit: int = 50) -> list[dict]:
        return self._repo.get_history(client_id, limit=limit)

    def get_insights(self, client_id: str) -> OzonInsightsResponse:
        history = self._repo.get_history(client_id, limit=20)
        if not history:
            raise ValueError(
                "Нет сохранённых снапшотов. "
                "Сначала запросите аналитику через POST /ozon/analytics"
            )
        current_entry = history[0]
        current = self._repo.get(client_id, current_entry["date_from"], current_entry["date_to"])
        if current is None:
            raise ValueError("Снапшот в истории есть, но данные истекли. Повторите запрос аналитики.")

        previous: MetricsSnapshot | None = None
        for entry in history[1:]:
            if entry["date_to"] < current_entry["date_from"]:
                snap = self._repo.get(client_id, entry["date_from"], entry["date_to"])
                if snap is not None:
                    previous = snap
                    break

        return self._analyze(client_id, current, previous)

    def compare(
        self,
        client_id: str,
        current_date_from: str,
        current_date_to: str,
        previous_date_from: str | None,
        previous_date_to: str | None,
        sku: str = "",
        analytics_service: OzonAnalyticsService | None = None,
    ) -> OzonInsightsResponse:
        if not previous_date_from or not previous_date_to:
            d_from = date.fromisoformat(current_date_from)
            d_to = date.fromisoformat(current_date_to)
            duration = (d_to - d_from).days
            auto_prev_to = d_from - timedelta(days=1)
            auto_prev_from = auto_prev_to - timedelta(days=duration)
            previous_date_from = auto_prev_from.isoformat()
            previous_date_to = auto_prev_to.isoformat()

        current = self._get_or_fetch(
            client_id, current_date_from, current_date_to, sku, analytics_service
        )
        if current is None:
            raise ValueError(
                f"Нет данных за {current_date_from}—{current_date_to}. "
                "Передайте активную сессию или предварительно запросите аналитику."
            )

        previous = self._get_or_fetch(
            client_id, previous_date_from, previous_date_to, sku, analytics_service
        )

        return self._analyze(client_id, current, previous)

    # ------------------------------------------------------------------
    # Получить снапшот из кэша или запросить в Ozon API
    # ------------------------------------------------------------------

    def _get_or_fetch(
        self,
        client_id: str,
        date_from: str,
        date_to: str,
        sku: str,
        analytics_service: OzonAnalyticsService | None,
    ) -> MetricsSnapshot | None:
        snap = self._repo.get(client_id, date_from, date_to)
        if snap is not None:
            return snap
        if analytics_service is not None:
            result = analytics_service.fetch(
                date_from=date_from,
                date_to=date_to,
                dimension=["sku"],
                sku=sku,
            )
            self._repo.save(client_id, result)
            return self._repo.get(client_id, date_from, date_to)
        return None

    # ------------------------------------------------------------------
    # Главный анализ
    # ------------------------------------------------------------------

    def _analyze(
        self,
        client_id: str,
        current: MetricsSnapshot,
        previous: MetricsSnapshot | None,
    ) -> OzonInsightsResponse:
        curr_totals: dict = current.payload.get("totals") or {}
        prev_totals: dict = previous.payload.get("totals") or {} if previous else {}

        curr_source_raw: dict = current.payload.get("source") or {}
        prev_source_raw: dict = previous.payload.get("source") or {} if previous else {}

        premium = _has_premium(curr_totals)

        deltas = self._compute_deltas(curr_totals, prev_totals)
        derived_kpis = self._compute_derived_kpis(curr_totals, prev_totals, premium)
        alerts = self._compute_alerts(curr_totals, prev_totals, derived_kpis, premium)
        health_score = self._compute_health_score(alerts)
        health_label = _health_label(health_score)
        recommendations = self._compute_recommendations(
            curr_totals, prev_totals, derived_kpis, alerts, premium
        )
        card_tips = self._compute_card_tips(curr_totals, derived_kpis, premium)

        curr_products = current.payload.get("products") or []
        prev_products_map: dict[str, dict] = {}
        if previous:
            for p in previous.payload.get("products") or []:
                dims = p.get("dimensions") or []
                if dims:
                    sku_id = dims[0].get("id", "")
                    if sku_id:
                        prev_products_map[sku_id] = p.get("metrics") or {}

        product_insights = self._compute_product_insights(curr_products, prev_products_map, premium)
        product_insights.sort(key=lambda x: x.health_score)
        bottom_products = product_insights[:5]
        top_products = sorted(product_insights, key=lambda x: x.health_score, reverse=True)[:5]

        summary = self._generate_summary(health_label, alerts, curr_totals, premium)

        def _src(raw: dict) -> OzonAnalyticsSource:
            return OzonAnalyticsSource(
                date_from=raw.get("date_from", ""),
                date_to=raw.get("date_to", ""),
                dimensions=raw.get("dimensions") or ["sku"],
                filters=raw.get("filters") or [],
            )

        return OzonInsightsResponse(
            client_id=client_id,
            current_period=_src(curr_source_raw),
            previous_period=_src(prev_source_raw) if previous else None,
            premium_available=premium,
            health_score=health_score,
            health_label=health_label,
            summary=summary,
            deltas=deltas,
            derived_kpis=derived_kpis,
            alerts=alerts,
            recommendations=recommendations,
            card_tips=card_tips,
            top_products=top_products,
            bottom_products=bottom_products,
            analyzed_at=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Дельты по всем доступным метрикам
    # ------------------------------------------------------------------

    def _compute_deltas(self, curr: dict, prev: dict) -> list[MetricDelta]:
        deltas = []
        # Показываем дельты по всем метрикам, которые есть хотя бы в текущем периоде
        for mid, label in _ALL_METRIC_LABELS.items():
            curr_v = curr.get(mid)
            if curr_v is None:
                continue
            prev_v = prev.get(mid) if prev else None
            d_abs = None
            d_pct = None
            if prev_v is not None and prev_v != 0:
                d_abs = curr_v - prev_v
                d_pct = d_abs / abs(prev_v)
            direc = _direction(d_pct)
            is_improvement = (
                (direc == "up" and mid not in _NEGATIVE_METRICS)
                or (direc == "down" and mid in _NEGATIVE_METRICS)
            )
            deltas.append(MetricDelta(
                metric_id=mid,
                label=label,
                current=curr_v,
                previous=prev_v,
                delta_abs=d_abs,
                delta_pct=d_pct,
                direction=direc,
                is_improvement=is_improvement,
            ))
        return deltas

    # ------------------------------------------------------------------
    # Производные KPI
    # ------------------------------------------------------------------

    def _compute_derived_kpis(self, curr: dict, prev: dict, premium: bool) -> list[DerivedKPI]:
        kpis: list[DerivedKPI] = []

        def _kpi(kpi_id, label, desc, curr_v, prev_v, fmt: str) -> DerivedKPI:
            d = _pct_delta(curr_v, prev_v)
            return DerivedKPI(
                id=kpi_id, label=label, description=desc,
                current=curr_v, previous=prev_v, delta_pct=d,
                format=fmt,  # type: ignore[arg-type]
                direction=_direction(d),
            )

        # --- Базовые KPI (всегда) ---

        curr_cr = _rate(curr.get("cancellations"), curr.get("ordered_units"))
        prev_cr = _rate(prev.get("cancellations"), prev.get("ordered_units"))
        kpis.append(_kpi("cancellation_rate", "Процент отмен",
                          "cancellations / ordered_units", curr_cr, prev_cr, "percent"))

        curr_rr = _rate(curr.get("returns"), curr.get("delivered_units"))
        prev_rr = _rate(prev.get("returns"), prev.get("delivered_units"))
        kpis.append(_kpi("return_rate", "Процент возвратов",
                          "returns / delivered_units", curr_rr, prev_rr, "percent"))

        curr_dr = _rate(curr.get("delivered_units"), curr.get("ordered_units"))
        prev_dr = _rate(prev.get("delivered_units"), prev.get("ordered_units"))
        kpis.append(_kpi("delivery_rate", "Процент доставки",
                          "delivered_units / ordered_units", curr_dr, prev_dr, "percent"))

        curr_aov = _rate(curr.get("revenue"), curr.get("ordered_units"))
        prev_aov = _rate(prev.get("revenue"), prev.get("ordered_units"))
        kpis.append(_kpi("avg_order_value", "Средний чек",
                          "revenue / ordered_units", curr_aov, prev_aov, "currency"))

        curr_net: float | None = None
        prev_net: float | None = None
        if curr.get("revenue") is not None and curr_aov and curr.get("returns") is not None:
            curr_net = curr["revenue"] - curr["returns"] * curr_aov  # type: ignore[operator]
        if prev.get("revenue") is not None and prev_aov and prev.get("returns") is not None:
            prev_net = prev["revenue"] - prev["returns"] * prev_aov  # type: ignore[operator]
        kpis.append(_kpi("net_revenue", "Чистая выручка (оценочно)",
                          "revenue − returns × avg_order_value", curr_net, prev_net, "currency"))

        delivered = curr.get("delivered_units")
        returns = curr.get("returns")
        ordered = curr.get("ordered_units")
        curr_fr: float | None = None
        if delivered is not None and returns is not None and ordered and ordered > 0:
            curr_fr = max(0.0, delivered - returns) / ordered
        prev_fr: float | None = None
        p_del = prev.get("delivered_units")
        p_ret = prev.get("returns")
        p_ord = prev.get("ordered_units")
        if p_del is not None and p_ret is not None and p_ord and p_ord > 0:
            prev_fr = max(0.0, p_del - p_ret) / p_ord
        kpis.append(_kpi("fulfillment_rate", "Чистый % выполнения",
                          "(delivered − returns) / ordered_units", curr_fr, prev_fr, "percent"))

        # --- Воронка (Premium) ---
        if premium:
            # CTR в поиске = просмотры карточки / показы в поиске
            curr_sctr = _rate(curr.get("hits_view_pdp"), curr.get("hits_view_search"))
            prev_sctr = _rate(prev.get("hits_view_pdp"), prev.get("hits_view_search"))
            kpis.append(_kpi("search_ctr", "CTR в поиске",
                              "hits_view_pdp / hits_view_search", curr_sctr, prev_sctr, "percent"))

            # Конверсия карточки → корзина
            curr_p2c = _rate(curr.get("hits_tocart_pdp"), curr.get("hits_view_pdp"))
            prev_p2c = _rate(prev.get("hits_tocart_pdp"), prev.get("hits_view_pdp"))
            kpis.append(_kpi("pdp_to_cart_rate", "Конверсия карточки → корзина",
                              "hits_tocart_pdp / hits_view_pdp", curr_p2c, prev_p2c, "percent"))

            # Конверсия корзина → заказ
            curr_c2o = _rate(curr.get("ordered_units"), curr.get("hits_tocart"))
            prev_c2o = _rate(prev.get("ordered_units"), prev.get("hits_tocart"))
            kpis.append(_kpi("cart_to_order_rate", "Конверсия корзина → заказ",
                              "ordered_units / hits_tocart", curr_c2o, prev_c2o, "percent"))

            # Выручка на сессию
            curr_rps = _rate(curr.get("revenue"), curr.get("session_view"))
            prev_rps = _rate(prev.get("revenue"), prev.get("session_view"))
            kpis.append(_kpi("revenue_per_session", "Выручка на сессию",
                              "revenue / session_view", curr_rps, prev_rps, "currency"))

            # Общая конверсия поиск → заказ (end-to-end)
            curr_e2e = _rate(curr.get("ordered_units"), curr.get("hits_view_search"))
            prev_e2e = _rate(prev.get("ordered_units"), prev.get("hits_view_search"))
            kpis.append(_kpi("search_to_order_rate", "Конверсия поиск → заказ",
                              "ordered_units / hits_view_search", curr_e2e, prev_e2e, "percent"))

        return kpis

    # ------------------------------------------------------------------
    # Алерты
    # ------------------------------------------------------------------

    def _compute_alerts(
        self,
        curr: dict,
        prev: dict,
        kpis: list[DerivedKPI],
        premium: bool,
    ) -> list[MetricAlert]:
        alerts: list[MetricAlert] = []
        kpi_map = {k.id: k for k in kpis}

        rev_d = _pct_delta(curr.get("revenue"), prev.get("revenue"))
        ou_d = _pct_delta(curr.get("ordered_units"), prev.get("ordered_units"))
        cr = kpi_map.get("cancellation_rate")
        rr = kpi_map.get("return_rate")
        dr = kpi_map.get("delivery_rate")

        # Выручка
        if rev_d is not None:
            if rev_d <= _REV_CRIT:
                alerts.append(MetricAlert(
                    id="revenue_critical_drop", severity="critical", emoji="🔴",
                    title="Критическое падение выручки",
                    detail=f"Выручка упала на {abs(rev_d)*100:.1f}% по сравнению с предыдущим периодом.",
                    metric_ids=["revenue"],
                ))
            elif rev_d <= _REV_WARN:
                alerts.append(MetricAlert(
                    id="revenue_warning_drop", severity="warning", emoji="🟡",
                    title="Падение выручки",
                    detail=f"Выручка снизилась на {abs(rev_d)*100:.1f}%.",
                    metric_ids=["revenue"],
                ))

        # Заказы
        if ou_d is not None:
            if ou_d <= _OU_CRIT:
                alerts.append(MetricAlert(
                    id="orders_critical_drop", severity="critical", emoji="🔴",
                    title="Критическое падение заказов",
                    detail=f"Количество заказов упало на {abs(ou_d)*100:.1f}%.",
                    metric_ids=["ordered_units"],
                ))
            elif ou_d <= _OU_WARN:
                alerts.append(MetricAlert(
                    id="orders_warning_drop", severity="warning", emoji="🟡",
                    title="Снижение количества заказов",
                    detail=f"Количество заказов снизилось на {abs(ou_d)*100:.1f}%.",
                    metric_ids=["ordered_units"],
                ))

        # Процент отмен
        if cr and cr.current is not None:
            if cr.current >= _CR_CRIT:
                alerts.append(MetricAlert(
                    id="cancellation_rate_critical", severity="critical", emoji="🔴",
                    title="Критически высокий процент отмен",
                    detail=f"Процент отмен {cr.current*100:.1f}% (норма 5–10%). Вероятно out-of-stock.",
                    metric_ids=["cancellations", "ordered_units"],
                ))
            elif cr.current >= _CR_WARN:
                alerts.append(MetricAlert(
                    id="cancellation_rate_warning", severity="warning", emoji="🟡",
                    title="Высокий процент отмен",
                    detail=f"Процент отмен {cr.current*100:.1f}% — проверьте остатки.",
                    metric_ids=["cancellations", "ordered_units"],
                ))
            elif cr.delta_pct is not None and cr.delta_pct >= 0.30 and cr.current >= 0.05:
                alerts.append(MetricAlert(
                    id="cancellation_rate_growing", severity="warning", emoji="🟡",
                    title="Растущий процент отмен",
                    detail=f"Процент отмен вырос на {cr.delta_pct*100:.1f}% относительно предыдущего периода.",
                    metric_ids=["cancellations", "ordered_units"],
                ))

        # Процент возвратов
        if rr and rr.current is not None:
            if rr.current >= _RR_CRIT:
                alerts.append(MetricAlert(
                    id="return_rate_critical", severity="critical", emoji="🔴",
                    title="Критически высокий процент возвратов",
                    detail=f"Процент возвратов {rr.current*100:.1f}% — проблема с карточкой или качеством.",
                    metric_ids=["returns", "delivered_units"],
                ))
            elif rr.current >= _RR_WARN:
                alerts.append(MetricAlert(
                    id="return_rate_warning", severity="warning", emoji="🟡",
                    title="Повышенный процент возвратов",
                    detail=f"Процент возвратов {rr.current*100:.1f}% — проверьте описание и фото.",
                    metric_ids=["returns", "delivered_units"],
                ))

        # Процент доставки
        if dr and dr.current is not None:
            if dr.current < _DR_CRIT:
                alerts.append(MetricAlert(
                    id="delivery_rate_critical", severity="critical", emoji="🔴",
                    title="Критически низкий процент доставки",
                    detail=f"Доставлено только {dr.current*100:.1f}% заказов.",
                    metric_ids=["delivered_units", "ordered_units"],
                ))
            elif dr.current < _DR_WARN:
                alerts.append(MetricAlert(
                    id="delivery_rate_warning", severity="warning", emoji="🟡",
                    title="Низкий процент доставки",
                    detail=f"Доставлено {dr.current*100:.1f}% заказов.",
                    metric_ids=["delivered_units", "ordered_units"],
                ))

        # Premium — воронка
        if premium:
            sctr = kpi_map.get("search_ctr")
            p2c = kpi_map.get("pdp_to_cart_rate")
            c2o = kpi_map.get("cart_to_order_rate")
            pos = curr.get("position_category")

            if pos is not None:
                if pos > _POS_CRIT:
                    alerts.append(MetricAlert(
                        id="position_critical", severity="critical", emoji="🔴",
                        title="Критически низкая позиция в поиске",
                        detail=f"Средняя позиция {pos:.0f} — товар практически не виден покупателям.",
                        metric_ids=["position_category", "hits_view_search"],
                    ))
                elif pos > _POS_WARN:
                    alerts.append(MetricAlert(
                        id="position_warning", severity="warning", emoji="🟡",
                        title="Низкая позиция в поиске",
                        detail=f"Средняя позиция {pos:.0f} — есть потенциал для роста.",
                        metric_ids=["position_category", "hits_view_search"],
                    ))

            if sctr and sctr.current is not None and sctr.current < _SEARCH_CTR_LOW:
                alerts.append(MetricAlert(
                    id="search_ctr_low", severity="warning", emoji="🟡",
                    title="Низкий CTR в поиске",
                    detail=f"CTR {sctr.current*100:.1f}% — покупатели видят, но не кликают на карточку.",
                    metric_ids=["hits_view_search", "hits_view_pdp"],
                ))

            if p2c and p2c.current is not None and p2c.current < _PDC_LOW:
                alerts.append(MetricAlert(
                    id="pdp_to_cart_low", severity="warning", emoji="🟡",
                    title="Низкая конверсия карточки",
                    detail=f"Только {p2c.current*100:.1f}% посетителей карточки добавляют в корзину.",
                    metric_ids=["hits_view_pdp", "hits_tocart_pdp"],
                ))

            if c2o and c2o.current is not None and c2o.current < _CTO_LOW:
                alerts.append(MetricAlert(
                    id="cart_to_order_low", severity="warning", emoji="🟡",
                    title="Низкая конверсия из корзины в заказ",
                    detail=f"Только {c2o.current*100:.1f}% добавлений в корзину переходят в заказ — возможно цена или рейтинг.",
                    metric_ids=["hits_tocart", "ordered_units"],
                ))

        if not alerts:
            alerts.append(MetricAlert(
                id="all_ok", severity="ok", emoji="🟢",
                title="Все показатели в норме",
                detail="Критических отклонений не обнаружено.",
                metric_ids=[],
            ))

        return alerts

    # ------------------------------------------------------------------
    # Health score
    # ------------------------------------------------------------------

    def _compute_health_score(self, alerts: list[MetricAlert]) -> int:
        score = 100
        for alert in alerts:
            if alert.severity == "critical":
                score -= 25
            elif alert.severity == "warning":
                score -= 10
        return max(0, min(100, score))

    # ------------------------------------------------------------------
    # Рекомендации по бизнесу
    # ------------------------------------------------------------------

    def _compute_recommendations(
        self,
        curr: dict,
        prev: dict,
        kpis: list[DerivedKPI],
        alerts: list[MetricAlert],
        premium: bool,
    ) -> list[MetricRecommendation]:
        recs: list[MetricRecommendation] = []
        kpi_map = {k.id: k for k in kpis}

        cr = kpi_map.get("cancellation_rate")
        rr = kpi_map.get("return_rate")
        dr = kpi_map.get("delivery_rate")
        rev_d = _pct_delta(curr.get("revenue"), prev.get("revenue"))
        ou_d = _pct_delta(curr.get("ordered_units"), prev.get("ordered_units"))

        if cr and cr.current and cr.current >= _CR_WARN:
            if rr is None or rr.current is None or rr.current < _RR_WARN:
                recs.append(MetricRecommendation(
                    priority=1,
                    title="Пополните склад и проверьте фулфилмент",
                    action=(
                        "Высокий процент отмен при нормальных возвратах — признак out-of-stock. "
                        "Проверьте остатки FBO/FBS, убедитесь что товары активны. "
                        "При FBS — проверьте сроки сборки."
                    ),
                    expected_impact="Снижение отмен на 40–60%",
                    affected_metrics=["cancellations", "ordered_units"],
                ))

        if rr and rr.current and rr.current >= _RR_WARN:
            recs.append(MetricRecommendation(
                priority=1 if rr.current >= _RR_CRIT else 2,
                title="Обновите карточку товара",
                action=(
                    "Покупатели возвращают товар чаще нормы. Проверьте: фото, описание, "
                    "размерную таблицу. Добавьте видеообзор. "
                    "Изучите тексты причин возврата в ЛК Ozon."
                ),
                expected_impact="Снижение возвратов на 20–40%",
                affected_metrics=["returns", "delivered_units"],
            ))

        if rev_d and rev_d < _REV_WARN and (ou_d is None or abs(ou_d) < 0.10):
            recs.append(MetricRecommendation(
                priority=2,
                title="Проанализируйте ценовую политику",
                action=(
                    "Выручка падает, заказы стабильны — снизился средний чек. "
                    "Проверьте цены конкурентов, уберите лишние скидки."
                ),
                expected_impact="Рост выручки без роста маркетинговых затрат",
                affected_metrics=["revenue", "ordered_units"],
            ))
        elif rev_d and rev_d < _REV_WARN and ou_d and ou_d < _OU_WARN:
            recs.append(MetricRecommendation(
                priority=1,
                title="Восстановите видимость в поиске",
                action=(
                    "Падают и выручка, и заказы — потеря видимости. "
                    "Проверьте позицию по ключевым запросам, рейтинг продавца, "
                    "участие в акциях Ozon."
                ),
                expected_impact="Восстановление органического трафика",
                affected_metrics=["revenue", "ordered_units"],
            ))

        if dr and dr.current and dr.current < _DR_WARN:
            recs.append(MetricRecommendation(
                priority=2,
                title="Переведите топ-товары на FBO",
                action=(
                    f"Процент доставки {dr.current*100:.0f}% — FBO даёт 95%+ и буст в ранжировании. "
                    "Оцените юнит-экономику перевода топ-5 SKU по выручке."
                ),
                expected_impact="Рост доставляемости и улучшение позиций",
                affected_metrics=["delivered_units", "ordered_units"],
            ))

        # Premium рекомендации
        if premium:
            pos = curr.get("position_category")
            sctr = kpi_map.get("search_ctr")
            if pos and pos > _POS_WARN:
                recs.append(MetricRecommendation(
                    priority=1,
                    title="Улучшите SEO-оптимизацию карточки",
                    action=(
                        f"Позиция {pos:.0f} — вынесите ключевые слова в начало названия, "
                        "заполните все атрибуты. Рассмотрите подключение рекламы (трафареты)."
                    ),
                    expected_impact="Рост позиции и органических показов",
                    affected_metrics=["position_category", "hits_view_search"],
                ))

        if not recs:
            recs.append(MetricRecommendation(
                priority=3,
                title="Поддерживайте и развивайте текущие результаты",
                action=(
                    "Показатели в норме. Участвуйте в акциях Ozon, "
                    "обновляйте фото и описание, отвечайте на отзывы."
                ),
                expected_impact="Стабильный органический рост продаж",
                affected_metrics=["revenue", "ordered_units"],
            ))

        recs.sort(key=lambda r: r.priority)
        return recs

    # ------------------------------------------------------------------
    # Советы по карточкам товаров (chat-рекомендации)
    # ------------------------------------------------------------------

    def _compute_card_tips(
        self,
        curr: dict,
        kpis: list[DerivedKPI],
        premium: bool,
    ) -> list[CardOptimizationTip]:
        tips: list[CardOptimizationTip] = []
        kpi_map = {k.id: k for k in kpis}

        cr = kpi_map.get("cancellation_rate")
        rr = kpi_map.get("return_rate")
        dr = kpi_map.get("delivery_rate")

        # ---- Советы на основе базовых метрик (всегда) ----

        # Высокие возвраты → фото не передают реальность
        if rr and rr.current and rr.current >= _RR_WARN:
            tips.append(CardOptimizationTip(
                id="photo_match_reality",
                priority=1 if rr.current >= _RR_CRIT else 2,
                category="photo",
                emoji="📸",
                title="Фото не соответствует товару",
                issue=f"Процент возвратов {rr.current*100:.1f}% — покупатели получают не то, что ожидали.",
                action=(
                    "Переснимите на нейтральном фоне. Добавьте фото всех комплектующих. "
                    "Покажите товар в руках для масштаба. Используйте цветокоррекцию — "
                    "не делайте цвет ярче реального."
                ),
                expected_result="Снижение возвратов на 20–40%",
                metric_evidence=["returns", "delivered_units"],
                requires_premium=False,
            ))
            tips.append(CardOptimizationTip(
                id="description_accuracy",
                priority=2,
                category="description",
                emoji="📝",
                title="Уточните описание и характеристики",
                issue="Возвраты часто вызваны несовпадением характеристик с ожиданиями.",
                action=(
                    "Укажите точные размеры в сантиметрах и вес. Добавьте таблицу размеров "
                    "если применимо. Перечислите состав материала. "
                    "Изучите тексты причин возврата в ЛК Ozon — там конкретные жалобы."
                ),
                expected_result="Более осознанные покупки, меньше недовольных клиентов",
                metric_evidence=["returns", "delivered_units"],
                requires_premium=False,
            ))

        # Высокие отмены → склад
        if cr and cr.current and cr.current >= _CR_WARN:
            if rr is None or rr.current is None or rr.current < _RR_WARN:
                tips.append(CardOptimizationTip(
                    id="stock_replenishment",
                    priority=1,
                    category="stock",
                    emoji="📦",
                    title="Пополните остатки на складе",
                    issue=f"Процент отмен {cr.current*100:.1f}% при нормальных возвратах — out-of-stock.",
                    action=(
                        "Проверьте текущие остатки в ЛК Ozon → Склад. "
                        "Настройте уведомления о низком остатке. "
                        "Рассмотрите увеличение страхового запаса до 30–45 дней продаж."
                    ),
                    expected_result="Снижение отмен на 40–60%",
                    metric_evidence=["cancellations", "ordered_units"],
                    requires_premium=False,
                ))

        # Низкая доставляемость → FBO
        if dr and dr.current and dr.current < _DR_WARN:
            tips.append(CardOptimizationTip(
                id="switch_fbo",
                priority=2,
                category="logistics",
                emoji="🏭",
                title="Переведите на склад Ozon (FBO)",
                issue=f"Доставляется {dr.current*100:.0f}% заказов — FBO обеспечивает 95%+ и даёт буст в ранжировании.",
                action=(
                    "Посчитайте юнит-экономику FBO для топ-SKU. "
                    "Начните с самых продаваемых позиций. "
                    "FBO также улучшает позицию в поиске — Ozon приоритизирует свои склады."
                ),
                expected_result="Рост доставляемости + улучшение позиции в поиске",
                metric_evidence=["delivered_units", "ordered_units"],
                requires_premium=False,
            ))

        # ---- Советы на основе Premium метрик (воронка) ----

        if premium:
            pos = curr.get("position_category")
            hits_view_search = curr.get("hits_view_search")
            hits_view_pdp = curr.get("hits_view_pdp")
            hits_tocart = curr.get("hits_tocart")
            hits_tocart_pdp = curr.get("hits_tocart_pdp")
            ordered_units = curr.get("ordered_units")

            sctr = kpi_map.get("search_ctr")
            p2c = kpi_map.get("pdp_to_cart_rate")
            c2o = kpi_map.get("cart_to_order_rate")

            # Позиция → SEO
            if pos is not None and pos > _POS_WARN:
                severity = 1 if pos > _POS_CRIT else 2
                tips.append(CardOptimizationTip(
                    id="seo_title_optimization",
                    priority=severity,
                    category="keywords",
                    emoji="🔍",
                    title="Оптимизируйте название для поиска",
                    issue=f"Позиция в поиске: {pos:.0f} — товар находится на {pos:.0f}-й строке выдачи.",
                    action=(
                        "Шаблон названия: [Тип товара] [Бренд] [Ключевой атрибут] [Вторичный атрибут]. "
                        "Пример: «Кроссовки Nike Air Max мужские белые 42 размер». "
                        "Заполните ВСЕ атрибуты карточки — незаполненные снижают ранжирование. "
                        "Не используйте заглавные буквы и спецсимволы в названии."
                    ),
                    expected_result=f"Рост позиции с {pos:.0f} до топ-{max(5, int(pos*0.4)):.0f}",
                    metric_evidence=["position_category", "hits_view_search"],
                    requires_premium=True,
                ))

            # Низкий CTR → главное фото
            if sctr and sctr.current is not None and sctr.current < _SEARCH_CTR_MED:
                priority = 1 if sctr.current < _SEARCH_CTR_LOW else 2
                tips.append(CardOptimizationTip(
                    id="main_photo_ctr",
                    priority=priority,
                    category="photo",
                    emoji="🖼️",
                    title="Замените главное фото",
                    issue=(
                        f"CTR в поиске {sctr.current*100:.1f}% — "
                        f"{'критически низкий' if sctr.current < _SEARCH_CTR_LOW else 'ниже среднего'}. "
                        "Покупатели видят карточку, но не кликают."
                    ),
                    action=(
                        "Тестируйте варианты: белый фон + товар крупно, lifestyle-фото, "
                        "инфографика с ключевым преимуществом прямо на фото. "
                        "Первое место в выдаче — первые 0.3 секунды решают всё. "
                        "Проверьте у конкурентов в топе — чем их фото отличаются."
                    ),
                    expected_result=f"Рост CTR с {sctr.current*100:.1f}% до 4–6%",
                    metric_evidence=["hits_view_search", "hits_view_pdp"],
                    requires_premium=True,
                ))

            # Низкая конверсия карточки → галерея и описание
            if p2c and p2c.current is not None and p2c.current < _PDC_MED:
                priority = 1 if p2c.current < _PDC_LOW else 2
                tips.append(CardOptimizationTip(
                    id="pdp_conversion_gallery",
                    priority=priority,
                    category="photo",
                    emoji="🖼️",
                    title="Улучшите галерею карточки",
                    issue=(
                        f"Конверсия карточки → корзина: {p2c.current*100:.1f}%. "
                        "Покупатели приходят, но не добавляют в корзину."
                    ),
                    action=(
                        "Добавьте 8–10 фото: главное, все стороны, детали, в использовании, "
                        "размерная сетка, упаковка. Видеообзор снижает возвраты и повышает "
                        "конверсию на 15–30%. Первые 3 фото — самые важные."
                    ),
                    expected_result=f"Рост конверсии карточки с {p2c.current*100:.1f}% до 8–12%",
                    metric_evidence=["hits_view_pdp", "hits_tocart_pdp"],
                    requires_premium=True,
                ))
                tips.append(CardOptimizationTip(
                    id="pdp_conversion_description",
                    priority=priority + 1,
                    category="description",
                    emoji="📝",
                    title="Структурируйте описание",
                    issue="Текстовое описание не убеждает покупателей добавить в корзину.",
                    action=(
                        "Структура: 1) Главная выгода (1 предложение), "
                        "2) Для кого этот товар, "
                        "3) 5–7 буллетов с конкретными характеристиками, "
                        "4) Что в комплекте, "
                        "5) Гарантия / возврат. "
                        "Используйте rich-content Ozon если доступно."
                    ),
                    expected_result="Рост конверсии карточки на 15–25%",
                    metric_evidence=["hits_view_pdp", "hits_tocart_pdp"],
                    requires_premium=True,
                ))

            # Низкая конверсия корзина → заказ
            if c2o and c2o.current is not None and c2o.current < _CTO_LOW:
                tips.append(CardOptimizationTip(
                    id="cart_to_order_price",
                    priority=2,
                    category="price",
                    emoji="💰",
                    title="Скорректируйте цену или участвуйте в акциях",
                    issue=(
                        f"Конверсия корзина → заказ {c2o.current*100:.1f}%. "
                        "Покупатели откладывают товар, но не оформляют заказ — "
                        "сравнивают цены или сомневаются."
                    ),
                    action=(
                        "Проверьте цену у топ-5 конкурентов по основному ключевому запросу. "
                        "Подключите акцию «Бестселлер» или «Скидка от объёма». "
                        "Убедитесь что рейтинг продавца выше 4.5 — это напрямую влияет "
                        "на решение о покупке из корзины."
                    ),
                    expected_result="Рост конверсии из корзины на 15–25%",
                    metric_evidence=["hits_tocart", "ordered_units"],
                    requires_premium=True,
                ))
                tips.append(CardOptimizationTip(
                    id="cart_to_order_reviews",
                    priority=3,
                    category="reviews",
                    emoji="⭐",
                    title="Работайте с рейтингом и отзывами",
                    issue="Низкая конверсия из корзины часто связана с недоверием — мало отзывов или низкий рейтинг.",
                    action=(
                        "Отвечайте на все отзывы — особенно негативные, публично и вежливо. "
                        "Подключите программу Ozon для получения первых отзывов. "
                        "Цель: минимум 20 отзывов и рейтинг 4.7+."
                    ),
                    expected_result="Рост доверия покупателей и конверсии из корзины",
                    metric_evidence=["hits_tocart", "ordered_units"],
                    requires_premium=True,
                ))

        # Если советов нет — общий
        if not tips:
            tips.append(CardOptimizationTip(
                id="maintain_quality",
                priority=3,
                category="photo",
                emoji="✅",
                title="Карточка в хорошем состоянии",
                issue="Все метрики в норме — конкретных проблем с карточкой не обнаружено.",
                action=(
                    "Для дальнейшего роста: добавьте видеообзор если его нет, "
                    "обновляйте фото раз в квартал, следите за конкурентами в топе выдачи."
                ),
                expected_result="Поддержание конкурентоспособности карточки",
                metric_evidence=[],
                requires_premium=False,
            ))

        tips.sort(key=lambda t: t.priority)
        return tips

    # ------------------------------------------------------------------
    # Per-SKU инсайты
    # ------------------------------------------------------------------

    def _compute_product_insights(
        self,
        curr_products: list[dict],
        prev_products_map: dict[str, dict],
        premium: bool,
    ) -> list[ProductInsight]:
        insights: list[ProductInsight] = []
        for p in curr_products:
            dims = p.get("dimensions") or []
            if not dims:
                continue
            sku_id = dims[0].get("id", "")
            name = dims[0].get("name", "") or sku_id
            m = p.get("metrics") or {}
            prev_m = prev_products_map.get(sku_id, {})

            revenue = m.get("revenue")
            ordered = m.get("ordered_units")
            delivered = m.get("delivered_units")
            cancellations = m.get("cancellations")
            returns = m.get("returns")
            prev_revenue = prev_m.get("revenue")

            cr = _rate(cancellations, ordered)
            rr = _rate(returns, delivered)
            rev_delta = _pct_delta(revenue, prev_revenue)

            score = 100
            if cr is not None:
                if cr >= _CR_CRIT:
                    score -= 30
                elif cr >= _CR_WARN:
                    score -= 15
            if rr is not None:
                if rr >= _RR_CRIT:
                    score -= 30
                elif rr >= _RR_WARN:
                    score -= 15
            if rev_delta is not None:
                if rev_delta <= _REV_CRIT:
                    score -= 20
                elif rev_delta <= _REV_WARN:
                    score -= 10

            # Premium: позиция в поиске по SKU
            if premium:
                pos = m.get("position_category")
                if pos is not None:
                    if pos > _POS_CRIT:
                        score -= 15
                    elif pos > _POS_WARN:
                        score -= 5

            score = max(0, score)
            alert_level: AlertSeverityType = "ok"
            if score < 50:
                alert_level = "critical"
            elif score < 75:
                alert_level = "warning"

            insights.append(ProductInsight(
                sku=sku_id,
                name=name,
                health_score=score,
                revenue=revenue,
                ordered_units=ordered,
                cancellation_rate=cr,
                return_rate=rr,
                revenue_delta_pct=rev_delta,
                alert_level=alert_level,
            ))
        return insights

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _generate_summary(
        self,
        health_label: str,
        alerts: list[MetricAlert],
        curr: dict,
        premium: bool,
    ) -> str:
        critical = [a for a in alerts if a.severity == "critical"]
        warnings = [a for a in alerts if a.severity == "warning"]
        rev = curr.get("revenue")
        rev_str = f" Выручка: {rev:,.0f} ₽." if rev else ""

        if health_label == "excellent":
            premium_str = " Анализ включает данные воронки конверсий." if premium else ""
            return f"Отличные показатели — бизнес работает стабильно.{rev_str}{premium_str}"
        elif health_label == "good":
            w_str = f" Есть {len(warnings)} предупреждени{'е' if len(warnings) == 1 else 'я'}." if warnings else ""
            return f"Показатели хорошие.{rev_str}{w_str}"
        elif health_label == "warning":
            titles = "; ".join(a.title for a in warnings[:2])
            return f"Требует внимания: {titles}."
        else:
            titles = "; ".join(a.title for a in critical[:2])
            return f"Критические проблемы: {titles}. Требуется срочное вмешательство."


def _health_label(score: int) -> str:
    if score >= 85:
        return "excellent"
    if score >= 65:
        return "good"
    if score >= 40:
        return "warning"
    return "critical"
