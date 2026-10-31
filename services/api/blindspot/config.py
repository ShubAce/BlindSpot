"""Settings for the whole backend, read once from environment variables and the repo-root .env file.

API keys are SecretStr, so they never show up in a repr, a log line or a traceback. Call
.get_secret_value() only at the exact point where a key is sent upstream, and pass any text that
might contain a key through Settings.redact() before logging or storing it.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, quote_plus

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent  # services/api/blindspot
API_DIR = PACKAGE_DIR.parent  # services/api
REPO_ROOT = API_DIR.parent.parent  # repository root, which holds .env
ENV_FILE = REPO_ROOT / ".env"

BBox = tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat), WGS84 degrees

SECRET_FIELDS = ("gemini_api_key", "firms_map_key", "data_gov_in_api_key", "openaq_api_key")
LOCAL_DATABASE_URL = "postgresql+psycopg://blindspot:blindspot@localhost:5433/blindspot"
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class Settings(BaseSettings):
    """Each field can be set by an environment variable with the same name in capitals.

    Real environment variables win over .env. Empty values (KEY=) count as not set.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore"
    )

    # Upstream API keys. All optional: a module whose key is missing must say so, not fake data.
    gemini_api_key: SecretStr | None = None
    firms_map_key: SecretStr | None = None
    data_gov_in_api_key: SecretStr | None = None
    openaq_api_key: SecretStr | None = None
    # data.gov.in refuses connections from GitHub's US runners, so the jobs there reach it through
    # the relay in Mumbai (infra/relay): DATAGOV_RELAY_URL=https://blindspot-relay.vercel.app
    datagov_relay_url: str | None = None

    database_url: str = LOCAL_DATABASE_URL  # the Docker PostGIS from infra/docker-compose.yml
    enable_scheduler: bool = False  # run the job scheduler inside the API process
    # Create missing tables when the API starts. Serverless deployments (Vercel) set this to false:
    # every cold start would otherwise run create_all, and the tables already exist there.
    init_db_on_startup: bool = True
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    # Origins allowed by pattern as well (full match), for preview deployments whose host changes:
    # CORS_ORIGIN_REGEX=^https://blindspot[a-z0-9-]*\.vercel\.app$
    cors_origin_regex: str | None = None
    cache_dir: Path = REPO_ROOT / "data" / "cache"  # downloads and rendered imagery (gitignored)
    aoi_bbox: Annotated[BBox, NoDecode] = (73.8, 27.6, 77.7, 32.6)  # Punjab + Haryana + Delhi
    met_bbox: Annotated[BBox, NoDecode] = (70.0, 26.0, 80.0, 34.0)  # wider box for wind fields
    public_base_url: str = "http://localhost:8000"  # how browsers reach this API
    log_level: str = "INFO"

    @field_validator(*SECRET_FIELDS, mode="before")
    @classmethod
    def _strip_key(cls, value: Any) -> Any:
        # A key pasted with a stray space or newline fails upstream in a confusing way.
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("database_url")
    @classmethod
    def _use_psycopg3(cls, value: str) -> str:
        # Accept the URL as people paste it into a secret store: Neon's console offers it wrapped in
        # `psql '...'`, and .env files often quote it.
        value = value.strip()
        if value.startswith("psql "):
            value = value[len("psql ") :].strip()
        value = value.strip("'\"").strip()
        # Jobs hold session-level advisory locks, which a transaction pooler does not keep, so always
        # use Neon's direct endpoint (the same host without '-pooler').
        value = re.sub(r"-pooler(\.)", r"\1", value, count=1)
        # Hosted Postgres providers hand out postgres:// URLs; SQLAlchemy must be told the driver.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        # CORS_ORIGINS may be a JSON list or comma-separated. Browsers send origins without a
        # trailing slash, so one in the setting would never match.
        if isinstance(value, str):
            text = value.strip()
            value = json.loads(text) if text.startswith("[") else text.split(",")
        if isinstance(value, list):
            return [str(origin).strip().rstrip("/") for origin in value if str(origin).strip()]
        return value

    @field_validator("aoi_bbox", "met_bbox", mode="before")
    @classmethod
    def _parse_bbox(cls, value: Any) -> Any:
        if isinstance(value, str):  # "73.8,27.6,77.7,32.6"
            return tuple(float(part) for part in value.strip(" []()").split(","))
        return value

    @field_validator("aoi_bbox", "met_bbox")
    @classmethod
    def _check_bbox(cls, value: BBox) -> BBox:
        min_lon, min_lat, max_lon, max_lat = value
        if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
            raise ValueError("bbox must be min_lon,min_lat,max_lon,max_lat with min < max")
        return value

    @field_validator("cache_dir")
    @classmethod
    def _anchor_cache_dir(cls, value: Path) -> Path:
        # A relative CACHE_DIR is relative to the repo root, not to wherever the process started.
        return value if value.is_absolute() else REPO_ROOT / value

    @field_validator("public_base_url")
    @classmethod
    def _no_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    def secret_values(self) -> list[str]:
        """The configured API keys (for masking). Values under 8 characters are skipped so that
        a short placeholder cannot turn ordinary words into ***."""
        values = []
        for name in SECRET_FIELDS:
            secret: SecretStr | None = getattr(self, name)
            if secret is not None and len(secret.get_secret_value()) >= 8:
                values.append(secret.get_secret_value())
        return values

    def redact(self, text: str) -> str:
        """Return `text` with every configured API key (raw or URL-encoded) replaced by ***."""
        for secret in self.secret_values():
            for form in {secret, quote(secret, safe=""), quote_plus(secret)}:
                text = text.replace(form, "***")
        return text

    def cache_path(self, *parts: str) -> Path:
        """A file path under cache_dir, with its parent folders created."""
        path = self.cache_dir.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    """The process-wide settings, read once. Tests call get_settings.cache_clear() to re-read."""
    return Settings()


class RedactingFormatter(logging.Formatter):
    """Formats a log record, then masks any API key that slipped into the message or traceback."""

    def format(self, record: logging.LogRecord) -> str:
        return get_settings().redact(super().format(record))


def setup_logging(level: str | None = None) -> None:
    """Log to stderr through the redacting formatter. Safe to call more than once."""
    root = logging.getLogger()
    if not any(isinstance(h.formatter, RedactingFormatter) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(RedactingFormatter(LOG_FORMAT))
        root.addHandler(handler)
    root.setLevel((level or get_settings().log_level).upper())
    # httpx logs every request URL at INFO, and some upstream URLs carry a key in the path.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
