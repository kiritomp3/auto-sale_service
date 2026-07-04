from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.clients.json_utils import parse_llm_json_object


_SIZE_TO_ASPECT: dict[str, str] = {
    "1024x1024": "1:1",
    "1024x1536": "2:3",
    "1536x1024": "3:2",
    "auto": "auto",
}


class KieAIImageClient:
    """
    Image-to-image через kie.ai GPT Image 2.

    Поток: upload image → createTask → poll recordInfo → download result.
    """

    _UPLOAD_URL = "https://kieai.redpandaai.co/api/file-stream-upload"
    _CREATE_URL = "https://api.kie.ai/api/v1/jobs/createTask"
    _STATUS_URL = "https://api.kie.ai/api/v1/jobs/recordInfo"
    _MODEL = "gpt-image-2-image-to-image"

    def __init__(self, api_key: str, poll_interval: int = 5, timeout: int = 300):
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._poll_interval = poll_interval
        self._timeout = timeout

    def _upload(self, image_path: Path) -> str:
        suffix = image_path.suffix.lower().lstrip(".")
        mime = f"image/{'jpeg' if suffix in ('jpg', 'jpeg') else suffix}"
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                self._UPLOAD_URL,
                headers=self._headers,
                files={"file": (image_path.name, image_path.read_bytes(), mime)},
                data={"uploadPath": "wbcards"},
            )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success"):
            raise RuntimeError(f"kie.ai upload failed: {body.get('msg')}")
        return body["data"]["downloadUrl"]

    def _create_task(self, prompt: str, image_urls: list[str], aspect_ratio: str = "1:1") -> str:
        payload = {
            "model": self._MODEL,
            "input": {"prompt": prompt, "input_urls": image_urls},
            "aspect_ratio": aspect_ratio,
            "resolution": "1K",
        }
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                self._CREATE_URL,
                headers={**self._headers, "Content-Type": "application/json"},
                json=payload,
            )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 200:
            raise RuntimeError(f"kie.ai createTask failed: {body.get('msg')}")
        return body["data"]["taskId"]

    def _poll(self, task_id: str) -> str:
        deadline = time.monotonic() + self._timeout
        with httpx.Client(timeout=15) as client:
            while time.monotonic() < deadline:
                resp = client.get(
                    self._STATUS_URL,
                    headers=self._headers,
                    params={"taskId": task_id},
                )
                resp.raise_for_status()
                data = resp.json().get("data", {})
                state = data.get("state")
                if state == "success":
                    result = json.loads(data["resultJson"])
                    return result["resultUrls"][0]
                if state == "fail":
                    raise RuntimeError(f"kie.ai task failed: {data.get('failMsg')}")
                time.sleep(self._poll_interval)
        raise TimeoutError(f"kie.ai task {task_id} не завершился за {self._timeout}с")

    def generate_card_image_b64(self, prompt: str, image_path: Path, image_size: str | None = None) -> str:
        aspect_ratio = _SIZE_TO_ASPECT.get(image_size or "", "1:1")
        image_url = self._upload(image_path)
        task_id = self._create_task(prompt, [image_url], aspect_ratio)
        result_url = self._poll(task_id)
        return self._download_b64(result_url)

    def generate_from_images_b64(
        self,
        prompt: str,
        image_paths: list[Path],
        image_size: str | None = None,
    ) -> str:
        """Image-to-image с несколькими входными изображениями.

        Используется для try-on: [фото модели, фото одежды] → одетая модель.
        Порядок image_paths важен и интерпретируется промптом.
        """
        if not image_paths:
            raise ValueError("Нужно хотя бы одно изображение")
        aspect_ratio = _SIZE_TO_ASPECT.get(image_size or "", "2:3")
        image_urls = [self._upload(path) for path in image_paths]
        task_id = self._create_task(prompt, image_urls, aspect_ratio)
        result_url = self._poll(task_id)
        return self._download_b64(result_url)

    def _download_b64(self, result_url: str) -> str:
        with httpx.Client(timeout=60) as client:
            resp = client.get(result_url)
        resp.raise_for_status()
        return base64.b64encode(resp.content).decode("utf-8")


class KieAIChatClient:
    """
    Текстовый клиент через kie.ai Claude Haiku 4.5.

    Совместим с Anthropic Messages API.
    """

    _URL = "https://api.kie.ai/claude/v1/messages"
    _MODEL = "claude-haiku-4-5"
    _DEFAULT_MAX_TOKENS = 4096
    _LISTING_MAX_TOKENS = 8192
    _REPAIR_MAX_TOKENS = 8192
    _JSON_SYSTEM = "Return only a valid JSON object. Do not use markdown or explanatory text."
    _JSON_REPAIR_SYSTEM = (
        "You repair malformed JSON. Return only one valid JSON object. "
        "Preserve fields and values from the source as much as possible. Do not use markdown."
    )

    _MIME = {
        ".gif": "image/gif",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }

    def __init__(self, api_key: str):
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    @dataclass(frozen=True)
    class _TextResponse:
        text: str
        stop_reason: str | None

    def _post_response(
        self,
        messages: list,
        system: str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> _TextResponse:
        body: dict = {"model": self._MODEL, "max_tokens": max_tokens, "stream": False, "messages": messages}
        if system:
            body["system"] = system
        with httpx.Client(timeout=180) as client:
            resp = client.post(self._URL, headers=self._headers, json=body)
        resp.raise_for_status()
        payload = resp.json()
        content = payload.get("content", [])
        if isinstance(content, str):
            text = content
        else:
            text = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        if not text:
            raise RuntimeError("AI вернул пустой текстовый ответ.")
        return self._TextResponse(text=text, stop_reason=payload.get("stop_reason"))

    def _post(
        self,
        messages: list,
        system: str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> str:
        return self._post_response(messages, system=system, max_tokens=max_tokens).text

    @staticmethod
    def _parse_json(text: str) -> dict:
        return parse_llm_json_object(text)

    def _repair_json(self, raw_text: str, parse_error: Exception) -> dict:
        repair_prompt = (
            "The previous model response was supposed to be JSON, but parsing failed.\n"
            f"Parser error: {parse_error}\n\n"
            "Return only corrected valid JSON object based on this source:\n"
            f"{raw_text}"
        )
        response = self._post_response(
            [{"role": "user", "content": repair_prompt}],
            system=self._JSON_REPAIR_SYSTEM,
            max_tokens=self._REPAIR_MAX_TOKENS,
        )
        return self._parse_json(response.text)

    def _post_json(
        self,
        messages: list,
        *,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        retry_max_tokens: int | None = None,
    ) -> dict:
        response = self._post_response(messages, system=self._JSON_SYSTEM, max_tokens=max_tokens)
        try:
            return self._parse_json(response.text)
        except (json.JSONDecodeError, ValueError) as first_error:
            if response.stop_reason == "max_tokens" and retry_max_tokens and retry_max_tokens > max_tokens:
                response = self._post_response(messages, system=self._JSON_SYSTEM, max_tokens=retry_max_tokens)
                try:
                    return self._parse_json(response.text)
                except (json.JSONDecodeError, ValueError) as retry_error:
                    first_error = retry_error

            try:
                return self._repair_json(response.text, first_error)
            except (json.JSONDecodeError, ValueError) as repair_error:
                raise RuntimeError(
                    "AI вернул невалидный JSON с описанием карточки. "
                    "Повторите генерацию или выберите один маркетплейс вместо всех."
                ) from repair_error

    def analyze_product(self, image_path: Path, prompt: str) -> dict:
        suffix = image_path.suffix.lower()
        mime = self._MIME.get(suffix, "image/jpeg")
        b64 = base64.b64encode(image_path.read_bytes()).decode()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                ],
            }
        ]
        return self._post_json(messages, max_tokens=self._DEFAULT_MAX_TOKENS, retry_max_tokens=self._LISTING_MAX_TOKENS)

    def generate_listing(self, prompt: str) -> dict:
        messages = [{"role": "user", "content": prompt}]
        return self._post_json(messages, max_tokens=self._LISTING_MAX_TOKENS)

    def chat(self, system: str, user_prompt: str, max_tokens: int = 1200) -> str:
        messages = [{"role": "user", "content": user_prompt}]
        return self._post(messages, system=system, max_tokens=max_tokens)
