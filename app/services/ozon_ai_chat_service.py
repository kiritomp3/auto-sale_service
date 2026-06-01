from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from openai import OpenAI

from app.models import OzonAIChatResponse, OzonInsightsResponse

if TYPE_CHECKING:
    from app.services.ozon_insights_service import OzonInsightsService

# gpt-4o-mini — достаточно умный и очень дешёвый ($0.15/$0.60 за 1M токенов)
_MODEL = "gpt-4o-mini"

_SYSTEM_PROMPT = """Ты — опытный аналитик продаж на маркетплейсе Ozon. \
Тебе дают данные о метриках продавца за период и ты даёшь конкретные, полезные рекомендации.

Правила:
- Пиши по-русски, коротко и по делу
- Используй конкретные цифры из данных
- Не повторяй одни и те же советы — каждый должен быть про своё
- Если есть критические проблемы — скажи прямо и первым
- Структура ответа: краткое резюме → ключевые проблемы → конкретные действия
- Не используй markdown-заголовки уровней # и ## — только текст и буллеты
- Для каждого совета укажи ожидаемый эффект в процентах или рублях если возможно"""


def _fmt(value: float | None, fmt: str) -> str:
    if value is None:
        return "нет данных"
    if fmt == "currency":
        return f"{value:,.0f} ₽"
    if fmt == "percent":
        return f"{value*100:.1f}%"
    if fmt == "integer":
        return f"{value:,.0f}"
    return f"{value:.2f}"


def _build_prompt(insights: OzonInsightsResponse, question: str) -> str:
    lines: list[str] = []

    # Период
    cur = insights.current_period
    lines.append(f"=== ТЕКУЩИЙ ПЕРИОД: {cur.date_from} — {cur.date_to} ===")

    prev = insights.previous_period
    if prev:
        lines.append(f"=== ПРЕДЫДУЩИЙ ПЕРИОД: {prev.date_from} — {prev.date_to} ===")

    # Состояние здоровья
    lines.append(f"\nОЦЕНКА СОСТОЯНИЯ: {insights.health_score}/100 ({insights.health_label})")
    lines.append(f"Резюме: {insights.summary}")
    lines.append(f"Premium метрики: {'да' if insights.premium_available else 'нет'}")

    # Дельты
    lines.append("\n--- МЕТРИКИ (текущий / предыдущий / изменение) ---")
    for d in insights.deltas:
        delta_str = ""
        if d.delta_pct is not None:
            sign = "+" if d.delta_pct > 0 else ""
            delta_str = f" | {sign}{d.delta_pct*100:.1f}%"
            if not d.is_improvement and d.delta_pct != 0:
                delta_str += " ⚠️"
        prev_str = f"{d.previous:,.0f}" if d.previous is not None else "—"
        curr_str = f"{d.current:,.0f}" if d.current is not None else "—"
        lines.append(f"  {d.label}: {curr_str} / {prev_str}{delta_str}")

    # Derived KPI
    lines.append("\n--- РАСЧЁТНЫЕ ПОКАЗАТЕЛИ ---")
    for kpi in insights.derived_kpis:
        curr_str = _fmt(kpi.current, kpi.format)
        prev_str = _fmt(kpi.previous, kpi.format) if kpi.previous is not None else "—"
        delta_str = ""
        if kpi.delta_pct is not None:
            sign = "+" if kpi.delta_pct > 0 else ""
            delta_str = f" ({sign}{kpi.delta_pct*100:.1f}%)"
        lines.append(f"  {kpi.label}: {curr_str} / {prev_str}{delta_str}")

    # Алерты
    critical = [a for a in insights.alerts if a.severity == "critical"]
    warnings = [a for a in insights.alerts if a.severity == "warning"]
    if critical:
        lines.append("\n--- КРИТИЧЕСКИЕ АЛЕРТЫ ---")
        for a in critical:
            lines.append(f"  {a.emoji} {a.title}: {a.detail}")
    if warnings:
        lines.append("\n--- ПРЕДУПРЕЖДЕНИЯ ---")
        for a in warnings:
            lines.append(f"  {a.emoji} {a.title}: {a.detail}")

    # Советы по карточкам
    high_tips = [t for t in insights.card_tips if t.priority <= 2]
    if high_tips:
        lines.append("\n--- ПРОБЛЕМЫ С КАРТОЧКАМИ ТОВАРОВ ---")
        for t in high_tips:
            lines.append(f"  {t.emoji} {t.title}")
            lines.append(f"     Проблема: {t.issue}")
            lines.append(f"     Действие: {t.action}")
            lines.append(f"     Ожидаемый эффект: {t.expected_result}")

    # Товары-аутсайдеры
    if insights.bottom_products:
        lines.append("\n--- ТОВАРЫ С ПРОБЛЕМАМИ (низкий health score) ---")
        for p in insights.bottom_products[:3]:
            cr = f", отмены {p.cancellation_rate*100:.1f}%" if p.cancellation_rate else ""
            rr = f", возвраты {p.return_rate*100:.1f}%" if p.return_rate else ""
            rev = f", выручка {p.revenue:,.0f} ₽" if p.revenue else ""
            lines.append(f"  SKU {p.sku} «{p.name[:40]}» — score {p.health_score}{rev}{cr}{rr}")

    # Вопрос пользователя или задание на анализ
    lines.append("\n=== ЗАДАНИЕ ===")
    if question.strip():
        lines.append(f"Пользователь спрашивает: {question}")
    else:
        lines.append(
            "Дай полный анализ: что идёт хорошо, что плохо, и 5 конкретных действий "
            "которые дадут наибольший эффект в ближайшие 2-4 недели."
        )

    return "\n".join(lines)


class OzonAIChatService:
    def __init__(self, openai_api_key: str, insights_service: OzonInsightsService):
        self._openai = OpenAI(api_key=openai_api_key)
        self._insights = insights_service

    def chat(
        self,
        client_id: str,
        current_date_from: str,
        current_date_to: str,
        question: str = "",
        sku: str = "",
        analytics_service=None,
    ) -> OzonAIChatResponse:
        # Получаем данные через rule-based движок
        insights = self._insights.compare(
            client_id=client_id,
            current_date_from=current_date_from,
            current_date_to=current_date_to,
            previous_date_from=None,
            previous_date_to=None,
            sku=sku,
            analytics_service=analytics_service,
        )

        # Строим промпт с данными
        user_prompt = _build_prompt(insights, question)

        # Вызываем GPT-4o-mini
        response = self._openai.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=1200,
        )

        answer = response.choices[0].message.content or ""
        period = f"{current_date_from} — {current_date_to}"

        return OzonAIChatResponse(
            answer=answer,
            client_id=client_id,
            period=period,
            analyzed_at=datetime.now(timezone.utc),
        )
