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

    _UPLOAD_URL = "https://api.kie.ai/api/file-stream-upload"
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

    def _create_task(self, prompt: str, image_url: str, aspect_ratio: str = "1:1") -> str:
        payload = {
            "model": self._MODEL,
            "input": {"prompt": prompt, "input_urls": [image_url]},
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
        task_id = self._create_task(prompt, image_url, aspect_ratio)
        result_url = self._poll(task_id)
        with httpx.Client(timeout=60) as client:
            resp = client.get(result_url)
        resp.raise_for_status()
        return base64.b64encode(resp.content).decode("utf-8")
