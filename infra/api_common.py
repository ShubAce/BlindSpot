"""What every deployment of the BlindSpot API shares: the packages the web process imports (pinned
from services/api/uv.lock), the upstream keys it needs, and the website origins its CORS allows.

Used by infra/aws/deploy.py (the live server, since 2026-11-22) and infra/deploy_vercel_api.py (the
earlier Vercel deployment), so either can go without breaking the other.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1] / "services" / "api"

# Top-level packages the web process may import. Versions are pinned from uv.lock (locked_versions).
# Heavy job libraries (geopandas, lightgbm, h5py, scipy, ...) stay out: the jobs run on GitHub Actions.
WEB_PACKAGES = [
    "fastapi", "starlette", "pydantic", "pydantic-settings", "sqlalchemy", "psycopg", "psycopg-binary",
    "geoalchemy2", "httpx", "orjson", "apscheduler", "tzdata", "cachetools", "tenacity",
    "sse-starlette", "python-dotenv", "numpy", "shapely", "skyfield", "sgp4", "jplephem",
    "google-genai", "pystac-client", "planetary-computer",
    # Imaging, imported on use only (sources/pcdata.py). Unpacked linux cp312 sizes measured on
    # 2026-10-31: the packages above 141 MB, pillow 13.0.0 +20 MB, rasterio 1.5.2 (GDAL inside) and
    # affine +121 MB: about 282 MB of Vercel's 500 MB.
    "pillow", "rasterio", "affine",
]

# The website's own domain (blindspotin.tech redirects to www.blindspotin.tech), its earlier Vercel
# production alias and deployments (blindspot-<hash>-<team>, blindspot-web-git-<branch>-<team>).
# Not every *.vercel.app name that starts with "blindspot": anyone can register one of those.
WEB_ORIGIN_REGEX = (
    r"^https://((www\.)?blindspotin\.tech"
    r"|blindspot(-web-five|(-web)?-[a-z0-9-]+-shubaces-projects)\.vercel\.app)$"
)

# The upstream keys the API itself sends (the jobs' copies are GitHub secrets: set_github_secrets.py).
SECRET_NAMES = (
    "GEMINI_API_KEY", "FIRMS_MAP_KEY", "DATA_GOV_IN_API_KEY", "OPENAQ_API_KEY",
    "LSASAF_USERNAME", "LSASAF_PASSWORD",
)


def locked_versions() -> dict[str, str]:
    """Every package's version in services/api/uv.lock."""
    lock = tomllib.loads((API_DIR / "uv.lock").read_text(encoding="utf-8"))
    return {pkg["name"]: pkg["version"] for pkg in lock.get("package", []) if "version" in pkg}
