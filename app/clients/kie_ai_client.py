from __future__ import annotations

import base64
from pathlib import Path

from openai import OpenAI


class KieAIImageClient:
    """Image-to-image через kie.ai (GPT Image 2 / gpt-image-1)."""

    BASE_URL = "https://api.kie.ai/v1"
    MODEL = "gpt-image-1"
    SIZE = "1024x1024"

    def __init__(self, api_key: str):
        self._client = OpenAI(api_key=api_key, base_url=self.BASE_URL)

    def generate_card_image_b64(self, prompt: str, image_path: Path) -> str:
        with image_path.open("rb") as fh:
            response = self._client.images.edit(
                model=self.MODEL,
                image=fh,
                prompt=prompt,
                size=self.SIZE,
                n=1,
                response_format="b64_json",
            )
        b64 = response.data[0].b64_json
        if not b64:
            raise RuntimeError("kie.ai не вернул изображение в ответе")
        return b64
