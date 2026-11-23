from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root locally (backend/app/core/config.py → 3 levels up), `/` inside the container, where
# docker-compose mounts ./models at /models and ./data at /data.
_ROOT = Path(__file__).resolve().parents[3]

MODELS_DIR = _ROOT / "models"  # large, regenerable artifacts (embedding, ISRC bridge, popularity)
DATA_DIR = _ROOT / "data"  # durable user data (extraction cache, play history)


class Settings(BaseSettings):
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = "http://127.0.0.1:8000/auth/spotify/callback"
    # Where the Vue app is served: the post-OAuth redirect target and the only CORS origin.
    frontend_url: str = "http://localhost:5173"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
