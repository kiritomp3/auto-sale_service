from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx


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

    def _post(self, messages: list, system: str | None = None, max_tokens: int = 4096) -> str:
        body: dict = {"model": self._MODEL, "max_tokens": max_tokens, "stream": False, "messages": messages}
        if system:
            body["system"] = system
        with httpx.Client(timeout=120) as client:
            resp = client.post(self._URL, headers=self._headers, json=body)
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]

    @staticmethod
    def _parse_json(text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        return json.loads(text)

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
        return self._parse_json(self._post(messages))

    def generate_listing(self, prompt: str) -> dict:
        messages = [{"role": "user", "content": prompt}]
        return self._parse_json(self._post(messages))

    def chat(self, system: str, user_prompt: str, max_tokens: int = 1200) -> str:
        messages = [{"role": "user", "content": user_prompt}]
        return self._post(messages, system=system, max_tokens=max_tokens)
