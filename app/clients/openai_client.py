import base64
import json
from pathlib import Path
from typing import Any

from openai import OpenAI


class OpenAIClient:
    def __init__(self, api_key: str, text_model: str, image_model: str):
        self._client = OpenAI(api_key=api_key)
        self._text_model = text_model
        self._image_model = image_model

    @staticmethod
    def _parse_json_text(text: str) -> dict[str, Any]:
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text.replace("json", "", 1).strip()
        return json.loads(text)

    @staticmethod
    def _encode_image_to_data_url(path: Path) -> str:
        ext = path.suffix.lower().replace(".", "") or "jpeg"
        b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
        return f"data:image/{ext};base64,{b64}"

    def analyze_product(self, image_path: Path, prompt: str) -> dict[str, Any]:
        image_data_url = self._encode_image_to_data_url(image_path)
        response = self._client.responses.create(
            model=self._text_model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_data_url},
                    ],
                }
            ],
        )
        return self._parse_json_text(response.output_text)

    def generate_card_image_b64(self, prompt: str, size: str) -> str:
        response = self._client.images.generate(
            model=self._image_model,
            prompt=prompt,
            size=size,
            quality="high",
        )
        return response.data[0].b64_json

    def generate_listing(self, prompt: str) -> dict[str, Any]:
        response = self._client.responses.create(model=self._text_model, input=prompt)
        return self._parse_json_text(response.output_text)

