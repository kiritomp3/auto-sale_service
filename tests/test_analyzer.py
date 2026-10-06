import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.analyzer import Analysis, build_analyzer_router


class Vision:
    def __init__(self):
        self.path = None
        self.calls = 0
        self.broken = False

    def analyze_product(self, path, prompt):
        self.path = path
        self.calls += 1
        assert path.is_file()
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
