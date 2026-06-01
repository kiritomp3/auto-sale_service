from __future__ import annotations

import base64
from pathlib import Path

from google import genai
from google.genai import types


class GeminiImageClient:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash-image"):
        self._client = genai.Client(api_key=api_key)
        self._model = model

    def generate_card_image_b64(self, prompt: str, image_path: Path) -> str:
        suffix = image_path.suffix.lower().lstrip(".")
        mime_type = f"image/{'jpeg' if suffix in ('jpg', 'jpeg') else suffix}"
        image_bytes = image_path.read_bytes()

        response = self._client.models.generate_content(
            model=self._model,
            contents=[
                prompt,
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            ],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
            ),
        )

        for part in response.candidates[0].content.parts:
            if part.inline_data is not None:
                return base64.b64encode(part.inline_data.data).decode("utf-8")

        raise RuntimeError("Gemini не вернул изображение в ответе")
