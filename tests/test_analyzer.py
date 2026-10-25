import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.analyzer import Analysis, build_analyzer_router
from app.clients.kie_ai_client import KieAIGeminiChatClient, KieAIResponseError


class Vision:
    def __init__(self):
        self.path = None
        self.calls = 0
        self.broken = False
        self.seen_format = None

    def analyze_product(self, path, prompt):
        self.path = path
        self.calls += 1
        assert path.is_file()
        with Image.open(path) as image:
            self.seen_format = image.format
        assert "seo_score" in prompt
        result = {key: 67 for key in ["total_score", "image_score", "infographic_score", "readability_score", "offer_score", "competitiveness_score"]}
        result.update(seo_score=None, problems=["Проблема 1", "Проблема 2", "Проблема 3"], recommendations=["Рекомендация 1", "Рекомендация 2", "Рекомендация 3"], strengths=["Сильная сторона"], suggested_actions=[{"kind": "image", "label": "Создать главное фото", "prompt": "Улучшить фон"}])
        if self.broken:
            result["total_score"] = 101
        return result


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.vision = Vision()
        app = FastAPI()
        app.include_router(build_analyzer_router(self.vision))
        self.client = TestClient(app)
        self.key = "test-internal-secret-at-least-32-characters"
        self.env = patch.dict(os.environ, {"ANALYZER_INTERNAL_KEY": self.key})
        self.env.start()
        data = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(data, "PNG")
        self.image = data.getvalue()

    def tearDown(self):
        self.env.stop()
        self.client.close()

    def upload(self, data=None, mime="image/png", key=None):
        return self.client.post("/internal/analyze", headers={"X-Analyzer-Key": key or self.key}, files={"image": ("../../untrusted.exe", self.image if data is None else data, mime)})

    def test_private_validated_pipeline_and_cleanup(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["seo_score"])
        self.assertFalse(self.vision.path.exists())
        self.assertEqual(self.vision.path.name, "image.png")

    def test_gateway_key_required_before_ai(self):
        self.assertEqual(self.upload(key="wrong").status_code, 403)
        self.assertEqual(self.vision.calls, 0)

    def test_failure_logs_classify_without_exposing_provider_data(self):
        secret = "private-image-url-and-api-key"
        request = httpx.Request("POST", "https://example.com/" + secret,
                                headers={"Authorization": "Bearer " + secret})
        http_error = httpx.HTTPStatusError(secret, request=request,
            response=httpx.Response(503, request=request, json={"private": secret}))
        wrapped = RuntimeError(secret)
        wrapped.__cause__ = KieAIResponseError(secret, kind="application_error", provider_code=500)
        cases = [
            (KieAIResponseError(secret, kind="application_error", provider_code=401), "application_error", "401"),
            (KieAIResponseError(secret, kind="upload_failed", provider_code=500), "upload_failed", "500"),
            (KieAIResponseError(secret, kind="invalid_json"), "invalid_json", "None"),
            (http_error, "http_status", "503"),
            (httpx.ReadTimeout(secret, request=request), "timeout", "None"),
            (wrapped, "application_error", "500"),
            (RuntimeError(secret), "unknown", "None"),
            (KieAIResponseError(secret, kind=secret, provider_code=secret), "unknown", "None"),
        ]
        for error, reason, code in cases:
            with self.subTest(reason=reason, code=code):
                with patch.object(self.vision, "analyze_product", side_effect=error), self.assertLogs("app.analyzer", "WARNING") as logs:
                    response = self.upload()
                self.assertEqual(response.status_code, 502)
                output = "\n".join(logs.output)
                self.assertIn("reason=" + reason, output)
                self.assertIn("provider_code=" + code, output)
                self.assertNotIn(secret, output)
                self.assertNotIn(secret, response.text)

    def test_invalid_model_output_logs_schema_reason_without_values(self):
        self.vision.broken = True
        with self.assertLogs("app.analyzer", "WARNING") as logs:
            response = self.upload()
        self.assertEqual(response.status_code, 502)
        self.assertIn("reason=invalid_schema", "\n".join(logs.output))
        self.assertNotIn("total_score", "\n".join(logs.output))

    def test_upload_to_validated_result_recovers_from_provider_application_error(self):
        import json
        result = {key: 67 for key in ["total_score", "image_score", "infographic_score", "readability_score", "offer_score", "competitiveness_score"]}
        result.update(seo_score=None, problems=["Проблема 1", "Проблема 2", "Проблема 3"],
            recommendations=["Рекомендация 1", "Рекомендация 2", "Рекомендация 3"], strengths=["Сильная сторона"],
            suggested_actions=[{"kind":"image", "label":"Проверить фон", "prompt":"Улучшить фон"}])
        responses = iter([httpx.Response(200, json={"code":503,"msg":"Temporary provider failure"}),
            httpx.Response(200, json={"choices":[{"message":{"content":json.dumps(result)},"finish_reason":"stop"}]})])
        requests = []
        def handler(request):
            requests.append(request)
            return next(responses)
        vision = KieAIGeminiChatClient("test-key")
        app = FastAPI()
        app.include_router(build_analyzer_router(vision))
        original_client = httpx.Client
        with TestClient(app) as client, patch.object(vision._image_client, "_upload", return_value="https://example.com/fixture.png") as upload:
            with patch("app.clients.kie_ai_client.httpx.Client", side_effect=lambda **kwargs: original_client(transport=httpx.MockTransport(handler))), patch("app.clients.kie_ai_client.time.sleep"):
                response = client.post("/internal/analyze", headers={"X-Analyzer-Key":self.key}, files={"image":("fixture.png",self.image,"image/png")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), result)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].content, requests[1].content)
        upload.assert_called_once()

    def test_phone_mpo_jpeg_uses_plain_first_frame_for_vision(self):
        data = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(data, "MPO", save_all=True, append_images=[Image.new("RGB", (100, 100), "black")])
        self.assertEqual(self.upload(data.getvalue(), mime="image/jpeg").status_code, 200)
        self.assertEqual(self.vision.seen_format, "JPEG")

    def test_corrupt_mime_and_size_rejected_before_ai(self):
        self.assertEqual(self.upload(b"not an image").status_code, 415)
        self.assertEqual(self.upload(mime="image/jpeg").status_code, 415)
        self.assertEqual(self.upload(b"x" * (10 * 1024 * 1024 + 1)).status_code, 413)
        data = io.BytesIO()
        Image.new("RGB", (5100, 5100)).save(data, "PNG")
        self.assertEqual(self.upload(data.getvalue()).status_code, 415)
        self.assertEqual(self.vision.calls, 0)

    def test_invalid_model_response_not_shown(self):
        self.vision.broken = True
        self.assertEqual(self.upload().status_code, 502)
        self.assertFalse(self.vision.path.exists())

    def test_application_wires_active_vision_client(self):
        # Import the real application to detect a stale Container attribute after changes.
        with patch.dict(os.environ, {"KIE_AI_API_KEY": "local-fixture-no-network"}):
            import main
        with patch.object(main.container.text_client, "analyze_product", side_effect=self.vision.analyze_product):
            response = TestClient(main.app).post("/internal/analyze", headers={"X-Analyzer-Key": self.key}, files={"image": ("card.png", self.image, "image/png")})
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
