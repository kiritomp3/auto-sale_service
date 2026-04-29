import base64
import json
from pathlib import Path
from typing import Any

from app.clients.openai_client import OpenAIClient
from app.prompts import BASE_STYLE, PRODUCT_LOCK_RULES


class CardGenerationService:
    def __init__(self, client: OpenAIClient, output_dir: Path, image_sizes: list[str] | None = None):
        self._client = client
        self._output_dir = output_dir
        self._image_sizes = image_sizes or ["1024x1280", "1024x1536"]
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _save_b64_png(b64_data: str, file_path: Path) -> None:
        file_path.write_bytes(base64.b64decode(b64_data))

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
        variants = [
            {
                "name": "Card 1 HERO",
                "layout": "ГЛАВНОЕ ФОТО: 80% площади — товар крупным планом. Минимум текста: заголовок + 2 плашки + оффер.",
                "title": short_title,
                "points": [selling_points[0], selling_points[1]],
                "offer": offer,
            },
            {
                "name": "Card 2 INFO",
                "layout": "ИНФОГРАФИКА: товар 45-55% площади, вокруг 4 плашки преимуществ, визуальные указатели/линии к деталям товара.",
                "title": f"Преимущества {product_name}",
                "points": [selling_points[2], selling_points[3], selling_points[4], selling_points[5]],
                "offer": "4 ключевых плюса",
            },
            {
                "name": "Card 3 OFFER",
                "layout": "ОФФЕРНАЯ: товар 60-65% площади, крупный оффер-бейдж, блок 'для кого/где использовать', акцент на выгоде.",
                "title": f"{product_name}: максимум пользы",
                "points": [selling_points[0], selling_points[3], selling_points[5]],
                "offer": offer,
            },
        ]
        selected = variants[card_index % len(variants)]
        points_text = ", ".join(selected["points"])

        prompt = f"""
Создай вертикальное главное фото карточки товара для маркетплейса в ярком технологичном рекламном стиле.
В центре размести крупный реалистичный 3D-рендер товара: {product_name}, под динамичным углом, с чистым светом, мягкими тенями и премиальной подачей.
Добавь крупный продающий заголовок на русском: {selected['title']}.
Используй информационные плашки с ключевыми преимуществами: {points_text}.
Добавь круглый оранжевый бейдж с главным оффером: {selected['offer']}.

{BASE_STYLE}

{PRODUCT_LOCK_RULES}

Жестко соблюдай сценарий {selected['name']}: {selected['layout']}
Сделай композицию заметно отличной от других сценариев по количеству текста, расположению плашек и доле товара в кадре.
Без лишних деталей, без чужих брендов, без орфографических ошибок.
""".strip()

        if refinement_prompt:
            prompt += f"\nДополнительное уточнение пользователя: {refinement_prompt}"
        return prompt

    def _generate_cards(self, analysis: dict[str, Any], refinement_prompt: str, job_id: str, n_cards: int = 3) -> list[str]:
        result_paths: list[str] = []
        for index in range(n_cards):
            prompt = self._build_card_prompt(analysis, index, refinement_prompt)
            generated_b64 = None
            last_error = None
            for size in self._image_sizes:
                try:
                    generated_b64 = self._client.generate_card_image_b64(prompt, size)
                    break
                except Exception as error:
                    last_error = error
            if generated_b64 is None:
                raise RuntimeError(f"Не удалось сгенерировать карточку {index + 1}: {last_error}")

            output_path = self._output_dir / f"{job_id}_card_{index + 1}.png"
            self._save_b64_png(generated_b64, output_path)
            result_paths.append(str(output_path))
        return result_paths

    def build_result(self, image_path: Path, refinement_prompt: str, job_id: str) -> dict[str, Any]:
        analysis_prompt = self._build_analyze_prompt(refinement_prompt)
        analysis = self._client.analyze_product(image_path=image_path, prompt=analysis_prompt)
        cards = self._generate_cards(analysis, refinement_prompt, job_id)
        listing_prompt = self._build_listing_prompt(analysis, refinement_prompt)
        listing_content = self._client.generate_listing(listing_prompt)

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

