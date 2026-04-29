import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    output_dir: Path = Path("output")
    temp_dir: Path = Path("tmp")
    image_model: str = "gpt-image-1"
    text_model: str = "gpt-5"
    worker_count: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY не найден в переменных окружения")
        return cls(openai_api_key=api_key)

