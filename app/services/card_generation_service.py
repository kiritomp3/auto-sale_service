from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from app.clients.kie_ai_client import KieAIImageClient
from app.clients.openai_client import OpenAIClient
from app.prompts import DEFAULT_STYLE, PRODUCT_LOCK_RULES


class CardGenerationService:
    def __init__(self, text_client: OpenAIClient, image_client: KieAIImageClient, output_dir: Path):
        self._text_client = text_client
        self._image_client = image_client
        self._output_dir = output_dir
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
    def _build_listing_prompt(analysis: dict[str, Any], refinement_prompt: str) -> str:
        return f"""
Сгенерируй контент карточки товара для WB/Ozon на русском языке.
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
    def _build_card_prompt(analysis: dict[str, Any], card_index: int, refinement_prompt: str) -> str:
        product_name = analysis.get("product_name") or analysis.get("product_type") or "товар"
        short_title = analysis.get("short_title") or f"{product_name} для ежедневного использования"
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
        ]
        selected = variants[card_index % len(variants)]
        points_text = ", ".join(selected["points"])

        return f"""
Используй товар с прикреплённого фото как визуальный референс для создания рекламной карточки маркетплейса.

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
    ) -> tuple[int, str]:
        prompt = self._build_card_prompt(analysis, index, refinement_prompt)
        generated_b64 = self._image_client.generate_card_image_b64(prompt, image_path)
        output_path = self._output_dir / f"{job_id}_card_{index + 1}.png"
        output_path.write_bytes(__import__("base64").b64decode(generated_b64))
        return index, str(output_path)

    def _generate_cards(
        self,
        analysis: dict[str, Any],
        image_path: Path,
        refinement_prompt: str,
        job_id: str,
        n_cards: int = 3,
    ) -> list[str]:
        result_paths: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=n_cards) as executor:
            futures = {
                executor.submit(self._generate_single_card, analysis, image_path, refinement_prompt, job_id, i): i
                for i in range(n_cards)
            }
            for future in as_completed(futures):
                index, path = future.result()
                result_paths[index] = path
        return [result_paths[i] for i in range(n_cards)]

    def build_result(self, image_path: Path, refinement_prompt: str, job_id: str) -> dict[str, Any]:
        analysis_prompt = self._build_analyze_prompt(refinement_prompt)
        analysis = self._text_client.analyze_product(image_path=image_path, prompt=analysis_prompt)
        cards = self._generate_cards(analysis, image_path, refinement_prompt, job_id)
        listing_prompt = self._build_listing_prompt(analysis, refinement_prompt)
        listing_content = self._text_client.generate_listing(listing_prompt)

        return {
            "job_id": job_id,
            "status": "done",
            "input": {
                "image_path": str(image_path),
                "refinement_prompt": refinement_prompt,
            },
            "analysis": analysis,
            "cards": cards,
            "listing_content": listing_content,
        }
