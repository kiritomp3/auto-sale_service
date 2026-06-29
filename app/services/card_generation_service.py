from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from app.clients.kie_ai_client import KieAIChatClient, KieAIImageClient
from app.config import DEFAULT_IMAGE_SIZE, DEFAULT_MARKETPLACE, normalize_image_size, normalize_marketplace
from app.prompts import (
    DEFAULT_STYLE,
    PRODUCT_LOCK_RULES,
    marketplace_card_guidance,
    marketplace_label,
    marketplace_listing_guidance,
)


DEFAULT_CARD_COUNT = 3
MIN_CARD_COUNT = 1
MAX_CARD_COUNT = 6
ALL_MARKETPLACE = "all"
ALL_MARKETPLACE_ALIASES = frozenset({"all", "all_marketplaces", "marketplaces", "multi", "vse", "vsyo", "все"})


def normalize_card_count(value: int | str | None) -> int:
    try:
        parsed = int(value) if value is not None else DEFAULT_CARD_COUNT
    except (TypeError, ValueError) as exc:
        raise RuntimeError("n_cards должен быть целым числом") from exc
    if parsed < MIN_CARD_COUNT or parsed > MAX_CARD_COUNT:
        raise RuntimeError(f"n_cards должен быть от {MIN_CARD_COUNT} до {MAX_CARD_COUNT}")
    return parsed


def normalize_card_marketplace(value: str | None) -> str:
    normalized = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in ALL_MARKETPLACE_ALIASES:
        return ALL_MARKETPLACE
    return normalize_marketplace(value)


class CardGenerationService:
    def __init__(
        self,
        text_client: KieAIChatClient,
        image_client: KieAIImageClient,
        output_dir: Path,
        image_size: str = DEFAULT_IMAGE_SIZE,
    ):
        self._text_client = text_client
        self._image_client = image_client
        self._output_dir = output_dir
        self._image_size = normalize_image_size(image_size)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _build_analyze_prompt(refinement_prompt: str) -> str:
        return f"""
Проанализируй фото товара и верни строго JSON без markdown.

Нужно определить и заполнить поля:
- product_name
- product_type
- category
- brand
- key_features (list)
- materials (list)
- colors (list)
- target_audience (list)
- usage_scenarios (list)
- selling_points (list)
- main_offer
- short_title

Если часть данных неочевидна, сделай осторожное предположение.
Уточнение от пользователя: {refinement_prompt or 'нет'}
""".strip()

    @staticmethod
    def _build_listing_prompt(analysis: dict[str, Any], refinement_prompt: str, marketplace: str) -> str:
        if marketplace == ALL_MARKETPLACE:
            return f"""
Сгенерируй контент карточки товара сразу для WB, Ozon и Авито на русском языке.
{marketplace_listing_guidance(marketplace)}
Верни строго JSON без markdown:
{{
  "title": str,
  "subtitle": str,
  "bullet_points": list[str],
  "full_description": str,
  "specifications": list[str],
  "seo_keywords": list[str],
  "search_queries": list[str],
  "marketplaces": {{
    "wildberries": {{
      "title": str,
      "subtitle": str,
      "bullet_points": list[str],
      "full_description": str,
      "specifications": list[str],
      "seo_keywords": list[str],
      "search_queries": list[str]
    }},
    "ozon": {{
      "title": str,
      "subtitle": str,
      "bullet_points": list[str],
      "full_description": str,
      "specifications": list[str],
      "seo_keywords": list[str],
      "search_queries": list[str]
    }},
    "avito": {{
      "title": str,
      "subtitle": str,
      "bullet_points": list[str],
      "full_description": str,
      "specifications": list[str],
      "seo_keywords": list[str],
      "search_queries": list[str],
      "category": str,
      "price_hint": str,
      "location_hint": str,
      "attributes": dict
    }}
  }}
}}

Верхний уровень сделай универсальным. В marketplaces адаптируй тексты под конкретные площадки:
- wildberries: короткий продающий заголовок и SEO для WB.
- ozon: более подробный заголовок и структурированные характеристики.
- avito: объявление в стиле классифайда с категорией, ценой, локацией и честным описанием.

Используй данные анализа:
{json.dumps(analysis, ensure_ascii=False, indent=2)}

Уточнение от пользователя: {refinement_prompt or 'нет'}
""".strip()

        if marketplace == "avito":
            return f"""
Сгенерируй контент объявления для Авито на русском языке.
{marketplace_listing_guidance(marketplace)}
Верни строго JSON без markdown:
{{
  "title": str,
  "subtitle": str,
  "bullet_points": list[str],
  "full_description": str,
  "specifications": list[str],
  "seo_keywords": list[str],
  "search_queries": list[str],
  "category": str,
  "price": number | null,
  "price_hint": str,
  "location_hint": str,
  "condition": str,
  "attributes": dict,
  "publication_notes": list[str]
}}

Сделай объявление честным и пригодным для проверки перед публикацией. Если точную цену или город нельзя определить по фото, верни null в price и объясни в price_hint/location_hint, что пользователю нужно подтвердить значение.

Используй данные анализа:
{json.dumps(analysis, ensure_ascii=False, indent=2)}

Уточнение от пользователя: {refinement_prompt or 'нет'}
""".strip()

        return f"""
Сгенерируй контент карточки товара для маркетплейса {marketplace_label(marketplace)} на русском языке.
{marketplace_listing_guidance(marketplace)}
Верни строго JSON без markdown:
{{
  "title": str,
  "subtitle": str,
  "bullet_points": list[str],
  "full_description": str,
  "specifications": list[str],
  "seo_keywords": list[str],
  "search_queries": list[str]
}}

Используй данные анализа:
{json.dumps(analysis, ensure_ascii=False, indent=2)}

Уточнение от пользователя: {refinement_prompt or 'нет'}
""".strip()

    @staticmethod
    def _build_card_prompt(analysis: dict[str, Any], card_index: int, refinement_prompt: str, marketplace: str) -> str:
        product_name = analysis.get("product_name") or analysis.get("product_type") or "товар"
        short_title = analysis.get("short_title") or f"{product_name} для ежедневного использования"
        marketplace_name = marketplace_label(marketplace)
        selling_points = list(analysis.get("selling_points", []))
        while len(selling_points) < 6:
            selling_points.append("Качественные материалы")

        offer = analysis.get("main_offer") or "Хит продаж"
        style = refinement_prompt.strip() if refinement_prompt and refinement_prompt.strip() else DEFAULT_STYLE

        variants = [
            {
                "name": "Card 1 HERO",
                "layout": "80% площади — товар крупным планом. Минимум текста: заголовок + 2 плашки + оффер.",
                "title": short_title,
                "points": [selling_points[0], selling_points[1]],
                "offer": offer,
            },
            {
                "name": "Card 2 INFO",
                "layout": "Товар 45-55% площади, вокруг 4 плашки преимуществ с визуальными указателями к деталям товара.",
                "title": f"Преимущества {product_name}",
                "points": [selling_points[2], selling_points[3], selling_points[4], selling_points[5]],
                "offer": "4 ключевых плюса",
            },
            {
                "name": "Card 3 OFFER",
                "layout": "Товар 60-65% площади, крупный оффер-бейдж, блок «для кого / где использовать», акцент на выгоде.",
                "title": f"{product_name}: максимум пользы",
                "points": [selling_points[0], selling_points[3], selling_points[5]],
                "offer": offer,
            },
            {
                "name": "Card 4 COMPARE",
                "layout": "Товар 40-50% площади сбоку, рядом аккуратная таблица-сравнение характеристик с галочками-преимуществами.",
                "title": f"Характеристики {product_name}",
                "points": [selling_points[1], selling_points[2], selling_points[4], selling_points[5]],
                "offer": "Сравните сами",
            },
            {
                "name": "Card 5 LIFESTYLE",
                "layout": "Товар в реалистичном сценарии применения (lifestyle-сцена), 70% площади — окружение использования, минимум плашек.",
                "title": f"{product_name} в деле",
                "points": [selling_points[0], selling_points[4]],
                "offer": "Удобно каждый день",
            },
            {
                "name": "Card 6 SOCIAL",
                "layout": "Товар 50% площади, блок социального доказательства: бейджи «хит продаж», звёзды-рейтинг, короткие плашки-отзывы.",
                "title": f"Почему выбирают {product_name}",
                "points": [selling_points[2], selling_points[3], selling_points[5]],
                "offer": "Выбор покупателей",
            },
        ]
        selected = variants[card_index % len(variants)]
        points_text = ", ".join(selected["points"])

        return f"""
Используй товар с прикреплённого фото как визуальный референс для создания рекламной карточки маркетплейса {marketplace_name}.

{marketplace_card_guidance(marketplace)}
СТИЛЬ: {style}

СЦЕНАРИЙ {selected['name']}: {selected['layout']}
Заголовок: {selected['title']}
Преимущества: {points_text}
Оффер-бейдж: {selected['offer']}

{PRODUCT_LOCK_RULES}

Формат: вертикальный 4:5, весь текст на русском, без орфографических ошибок, без чужих брендов.
""".strip()

    def _generate_single_card(
        self,
        analysis: dict[str, Any],
        image_path: Path,
        refinement_prompt: str,
        job_id: str,
        index: int,
        image_size: str,
        marketplace: str,
    ) -> tuple[int, str]:
        prompt = self._build_card_prompt(analysis, index, refinement_prompt, marketplace)
        generated_b64 = self._image_client.generate_card_image_b64(prompt, image_path, image_size)
        output_path = self._output_dir / f"{job_id}_card_{index + 1}.png"
        output_path.write_bytes(__import__("base64").b64decode(generated_b64))
        return index, str(output_path)

    def _generate_cards(
        self,
        analysis: dict[str, Any],
        image_path: Path,
        refinement_prompt: str,
        job_id: str,
        image_size: str,
        marketplace: str,
        n_cards: int = DEFAULT_CARD_COUNT,
    ) -> list[str]:
        result_paths: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=n_cards) as executor:
            futures = {
                executor.submit(
                    self._generate_single_card,
                    analysis,
                    image_path,
                    refinement_prompt,
                    job_id,
                    i,
                    image_size,
                    marketplace,
                ): i
                for i in range(n_cards)
            }
            for future in as_completed(futures):
                index, path = future.result()
                result_paths[index] = path
        return [result_paths[i] for i in range(n_cards)]

    def build_result(
        self,
        image_path: Path,
        refinement_prompt: str,
        job_id: str,
        image_size: str | None = None,
        marketplace: str | None = None,
        n_cards: int | str | None = DEFAULT_CARD_COUNT,
    ) -> dict[str, Any]:
        normalized_image_size = normalize_image_size(image_size or self._image_size)
        normalized_marketplace = normalize_card_marketplace(marketplace or DEFAULT_MARKETPLACE)
        normalized_n_cards = normalize_card_count(n_cards)
        analysis_prompt = self._build_analyze_prompt(refinement_prompt)
        analysis = self._text_client.analyze_product(image_path=image_path, prompt=analysis_prompt)
        cards = self._generate_cards(
            analysis,
            image_path,
            refinement_prompt,
            job_id,
            normalized_image_size,
            normalized_marketplace,
            normalized_n_cards,
        )
        listing_prompt = self._build_listing_prompt(analysis, refinement_prompt, normalized_marketplace)
        listing_content = self._text_client.generate_listing(listing_prompt)

        return {
            "job_id": job_id,
            "status": "done",
            "input": {
                "image_path": str(image_path),
                "refinement_prompt": refinement_prompt,
                "image_size": normalized_image_size,
                "marketplace": normalized_marketplace,
                "n_cards": normalized_n_cards,
            },
            "analysis": analysis,
            "cards": cards,
            "listing_content": listing_content,
        }
