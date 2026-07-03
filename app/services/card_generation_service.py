from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from app.clients.kie_ai_client import KieAIChatClient, KieAIImageClient
from app.config import DEFAULT_IMAGE_SIZE, DEFAULT_MARKETPLACE, normalize_image_size, normalize_marketplace
from app.marketplace_categories import (
    ALL_MARKETPLACE_TARGETS,
    coerce_marketplace_category,
    coerce_marketplace_fixed_values,
    all_marketplace_category_prompt,
    all_marketplace_fixed_values_prompt,
    marketplace_category_prompt,
    marketplace_fixed_values_prompt,
    marketplace_output_id,
)
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
HASHTAG_MARKETPLACES = frozenset({"ozon"})
HASHTAG_KEYS = ("hashtags", "hash_tags", "tags")
HASHTAG_SOURCE_KEYS = (
    "hashtags",
    "hash_tags",
    "tags",
    "seo_keywords",
    "search_queries",
    "keywords",
    "title",
    "name",
    "product_name",
    "product_title",
    "category",
    "category_name",
    "product_category",
    "brand",
)
HASHTAG_STOP_WORDS = frozenset(
    {
        "ozon",
        "wildberries",
        "wb",
        "avito",
        "yandex",
        "market",
        "megamarket",
        "маркет",
        "маркетплейс",
        "мегамаркет",
        "авито",
        "для",
        "или",
        "and",
        "the",
        "with",
    }
)
MAX_HASHTAGS = 8
MARKETPLACE_LISTING_ALIASES = {
    "wb": ("wildberries", "wb"),
    "ozon": ("ozon",),
    "avito": ("avito",),
    "yandex_market": ("yandex_market", "yandexMarket", "ymarket", "yandex"),
    "megamarket": (
        "megamarket",
        "mega_market",
        "megaMarket",
        "sbermegamarket",
        "sberMegaMarket",
        "sber_market",
    ),
}
COMMON_LISTING_KEYS = frozenset(
    {
        "title",
        "subtitle",
        "bullet_points",
        "full_description",
        "specifications",
        "seo_keywords",
        "search_queries",
        "hashtags",
        "hash_tags",
        "tags",
        "brand",
        "price",
        "price_hint",
        "location_hint",
        "condition",
        "attributes",
        "vat",
        "currency",
        "currencyId",
        "currency_code",
        "dimension_unit",
        "weight_unit",
    }
)


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
    def _hashtags_instruction(marketplace: str) -> str:
        if marketplace == ALL_MARKETPLACE:
            return "Для блока ozon обязательно заполни hashtags: 5-8 релевантных тегов без символа #, без пробелов и без выдуманных брендов."
        if marketplace in HASHTAG_MARKETPLACES:
            return "Заполни hashtags: 5-8 релевантных тегов для поиска, без символа #, без пробелов и без выдуманных брендов."
        return ""

    @staticmethod
    def _build_listing_prompt(analysis: dict[str, Any], refinement_prompt: str, marketplace: str) -> str:
        marketplace = normalize_card_marketplace(marketplace)
        if marketplace == ALL_MARKETPLACE:
            category_guidance = all_marketplace_category_prompt()
            fixed_values_guidance = all_marketplace_fixed_values_prompt()
            hashtags_instruction = CardGenerationService._hashtags_instruction(marketplace)
            return f"""
Сгенерируй контент карточки товара сразу для WB, Ozon, Авито, Яндекс Маркета и Мегамаркета на русском языке.
{marketplace_listing_guidance(marketplace)}
{category_guidance}
{fixed_values_guidance}
{hashtags_instruction}
Верни строго JSON без markdown:
{{
  "title": str,
  "subtitle": str,
  "category": str,
  "bullet_points": list[str],
  "full_description": str,
  "specifications": list[str],
  "seo_keywords": list[str],
  "search_queries": list[str],
  "marketplaces": {{
    "wildberries": {{
      "title": str,
      "subtitle": str,
      "category": str,
      "bullet_points": list[str],
      "full_description": str,
      "specifications": list[str],
      "seo_keywords": list[str],
      "search_queries": list[str]
    }},
    "ozon": {{
      "title": str,
      "subtitle": str,
      "category": str,
      "bullet_points": list[str],
      "full_description": str,
      "specifications": list[str],
      "seo_keywords": list[str],
      "search_queries": list[str],
      "hashtags": list[str]
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
    }},
    "yandex_market": {{
      "title": str,
      "subtitle": str,
      "category": str,
      "bullet_points": list[str],
      "full_description": str,
      "specifications": list[str],
      "seo_keywords": list[str],
      "search_queries": list[str],
      "attributes": dict
    }},
    "megamarket": {{
      "title": str,
      "subtitle": str,
      "category": str,
      "bullet_points": list[str],
      "full_description": str,
      "specifications": list[str],
      "seo_keywords": list[str],
      "search_queries": list[str],
      "attributes": dict
    }}
  }}
}}

Верхний уровень сделай универсальным. В marketplaces адаптируй тексты под конкретные площадки:
- wildberries: короткий продающий заголовок и SEO для WB.
- ozon: более подробный заголовок, структурированные характеристики и хештеги.
- avito: объявление в стиле классифайда с категорией, ценой, локацией и честным описанием.
- yandex_market: оффер для Яндекс Маркета с категорией и параметрами товара.
- megamarket: карточка/фид Мегамаркета с категорией и параметрами товара.
Категорию в каждом блоке marketplaces выбери только из списка этой площадки и верни точное написание.

Используй данные анализа:
{json.dumps(analysis, ensure_ascii=False, indent=2)}

Уточнение от пользователя: {refinement_prompt or 'нет'}
""".strip()

        if marketplace == "avito":
            category_guidance = marketplace_category_prompt(marketplace)
            fixed_values_guidance = marketplace_fixed_values_prompt(marketplace)
            return f"""
Сгенерируй контент объявления для Авито на русском языке.
{marketplace_listing_guidance(marketplace)}
{category_guidance}
{fixed_values_guidance}
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
Категория обязана быть ровно одним значением из списка Авито выше.

Используй данные анализа:
{json.dumps(analysis, ensure_ascii=False, indent=2)}

Уточнение от пользователя: {refinement_prompt or 'нет'}
""".strip()

        category_guidance = marketplace_category_prompt(marketplace)
        fixed_values_guidance = marketplace_fixed_values_prompt(marketplace)
        hashtags_instruction = CardGenerationService._hashtags_instruction(marketplace)
        hashtags_schema = ',\n  "hashtags": list[str]' if marketplace in HASHTAG_MARKETPLACES else ""
        return f"""
Сгенерируй контент карточки товара для маркетплейса {marketplace_label(marketplace)} на русском языке.
{marketplace_listing_guidance(marketplace)}
{category_guidance}
{fixed_values_guidance}
{hashtags_instruction}
Верни строго JSON без markdown:
{{
  "title": str,
  "subtitle": str,
  "category": str,
  "bullet_points": list[str],
  "full_description": str,
  "specifications": list[str],
  "seo_keywords": list[str],
  "search_queries": list[str]{hashtags_schema}
}}
Категория обязана быть ровно одним значением из списка выше.

Используй данные анализа:
{json.dumps(analysis, ensure_ascii=False, indent=2)}

Уточнение от пользователя: {refinement_prompt or 'нет'}
""".strip()

    @staticmethod
    def _as_record(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _get_by_keys(source: dict[str, Any], keys: tuple[str, ...]) -> Any:
        if not source:
            return None
        for key in keys:
            if key in source:
                return source[key]
            matched_key = next((candidate for candidate in source if str(candidate).lower() == key.lower()), None)
            if matched_key:
                return source[matched_key]
        return None

    @classmethod
    def _find_marketplace_record(cls, listing_content: dict[str, Any], marketplace: str) -> dict[str, Any]:
        aliases = MARKETPLACE_LISTING_ALIASES.get(marketplace, (marketplace_output_id(marketplace), marketplace))
        sources = [
            listing_content,
            cls._as_record(listing_content.get("marketplaces")),
            cls._as_record(listing_content.get("platforms")),
            cls._as_record(listing_content.get("channels")),
        ]

        for source in sources:
            value = cls._get_by_keys(source, aliases)
            if isinstance(value, dict):
                return dict(value)
        return {}

    @staticmethod
    def _common_listing_values(listing_content: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in listing_content.items()
            if key in COMMON_LISTING_KEYS
        }

    @classmethod
    def _category_candidates(
        cls,
        analysis: dict[str, Any],
        listing_content: dict[str, Any],
        marketplace_record: dict[str, Any],
    ) -> list[Any]:
        category_keys = ("category", "category_name", "product_category", "product_type", "type", "subjectName")
        context_keys = (
            "title",
            "name",
            "product_name",
            "product_title",
            "short_title",
            "subtitle",
            "bullet_points",
            "seo_keywords",
            "search_queries",
            "specifications",
            "key_features",
        )
        candidates: list[Any] = []
        for source in (marketplace_record, listing_content, analysis):
            candidates.extend(cls._get_by_keys(source, (key,)) for key in category_keys)
        for source in (marketplace_record, listing_content, analysis):
            candidates.extend(cls._get_by_keys(source, (key,)) for key in context_keys)
        return candidates

    @staticmethod
    def _string_values(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, (int, float, bool)):
            return [str(value)]
        if isinstance(value, dict):
            values: list[str] = []
            for nested_value in value.values():
                values.extend(CardGenerationService._string_values(nested_value))
            return values
        if isinstance(value, (list, tuple, set)):
            values: list[str] = []
            for nested_value in value:
                values.extend(CardGenerationService._string_values(nested_value))
            return values
        return []

    @staticmethod
    def _hashtag_candidates(value: Any) -> list[str]:
        candidates: list[str] = []
        for text in CardGenerationService._string_values(value):
            for part in re.split(r"[#,\n;]+", text):
                words = re.findall(r"[a-zа-яё0-9]+", part.replace("ё", "е").casefold())
                if not words:
                    continue
                meaningful_words = [
                    word
                    for word in words
                    if len(word) >= 3 and not word.isdigit() and word not in HASHTAG_STOP_WORDS
                ]
                if not meaningful_words:
                    continue
                if len(meaningful_words) <= 3:
                    candidates.append("".join(meaningful_words))
                else:
                    candidates.append("".join(meaningful_words[:3]))
                candidates.extend(word for word in meaningful_words if len(word) >= 4)
        return candidates

    @staticmethod
    def _normalize_hashtags(*values: Any) -> list[str]:
        hashtags: list[str] = []
        seen: set[str] = set()

        for value in values:
            for candidate in CardGenerationService._hashtag_candidates(value):
                tag = re.sub(r"[^a-zа-я0-9]+", "", candidate.replace("ё", "е").casefold())
                if len(tag) < 2 or tag.isdigit() or tag in seen:
                    continue
                seen.add(tag)
                hashtags.append(tag[:40])
                if len(hashtags) >= MAX_HASHTAGS:
                    return hashtags

        return hashtags

    @classmethod
    def _ensure_marketplace_hashtags(
        cls,
        marketplace: str,
        analysis: dict[str, Any],
        listing_content: dict[str, Any],
        marketplace_record: dict[str, Any],
    ) -> dict[str, Any]:
        if marketplace not in HASHTAG_MARKETPLACES:
            return marketplace_record

        existing_values = [cls._get_by_keys(marketplace_record, (key,)) for key in HASHTAG_KEYS]
        hashtags = cls._normalize_hashtags(*existing_values)

        if not hashtags:
            source_values: list[Any] = []
            for source in (marketplace_record, listing_content, analysis):
                source_values.extend(cls._get_by_keys(source, (key,)) for key in HASHTAG_SOURCE_KEYS)
            hashtags = cls._normalize_hashtags(*source_values)

        if hashtags:
            marketplace_record["hashtags"] = hashtags
            marketplace_record["hash_tags"] = hashtags

        return marketplace_record

    @classmethod
    def _normalize_marketplace_listing(
        cls,
        analysis: dict[str, Any],
        listing_content: dict[str, Any],
        marketplace: str,
    ) -> dict[str, Any]:
        existing_record = cls._find_marketplace_record(listing_content, marketplace)
        normalized_record = {
            **cls._common_listing_values(listing_content),
            **existing_record,
        }
        category = coerce_marketplace_category(
            marketplace,
            cls._category_candidates(analysis, listing_content, normalized_record),
        )

        if category:
            normalized_record["category"] = category
            normalized_record["category_name"] = category
            normalized_record["product_category"] = category
        normalized_record = coerce_marketplace_fixed_values(marketplace, normalized_record)
        normalized_record = cls._ensure_marketplace_hashtags(
            marketplace,
            analysis,
            listing_content,
            normalized_record,
        )
        normalized_record["target_marketplace"] = marketplace_output_id(marketplace)
        return normalized_record

    @classmethod
    def normalize_listing_content(
        cls,
        listing_content: dict[str, Any],
        analysis: dict[str, Any],
        marketplace: str,
    ) -> dict[str, Any]:
        normalized_marketplace = normalize_card_marketplace(marketplace)
        source_listing = dict(cls._as_record(listing_content))
        source_analysis = cls._as_record(analysis)

        if normalized_marketplace == ALL_MARKETPLACE:
            marketplaces = {
                marketplace_output_id(target): cls._normalize_marketplace_listing(
                    source_analysis,
                    source_listing,
                    target,
                )
                for target in ALL_MARKETPLACE_TARGETS
            }
            categories = {
                marketplace_id: record["category"]
                for marketplace_id, record in marketplaces.items()
                if record.get("category")
            }
            result = dict(source_listing)
            result["marketplaces"] = marketplaces
            result["marketplace_categories"] = categories
            if categories:
                result["category"] = categories.get("wildberries") or next(iter(categories.values()))
                result["category_name"] = result["category"]
                result["product_category"] = result["category"]
            return result

        normalized_record = cls._normalize_marketplace_listing(
            source_analysis,
            source_listing,
            normalized_marketplace,
        )
        result = {
            **source_listing,
            **normalized_record,
        }
        if normalized_record.get("category"):
            result["marketplace_categories"] = {
                marketplace_output_id(normalized_marketplace): normalized_record["category"]
            }
        return result

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
        listing_content = self.normalize_listing_content(listing_content, analysis, normalized_marketplace)

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
