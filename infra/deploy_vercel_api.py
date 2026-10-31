"""Deploy the BlindSpot API to Vercel as a Python serverless function (free Hobby plan).

Run from services/api so python-dotenv is available:
    cd services/api && uv run python ../../infra/deploy_vercel_api.py [--foundation-only] [--cors URL,...]

Only the web-serving code goes to Vercel. Heavy job libraries (geopandas, lightgbm, h5py, scipy, ...)
are deliberately left out: scheduled jobs that need them run on GitHub Actions and write to Neon,
and the API only reads Neon. The two imaging libraries do ship (pillow checks and encodes every
picture; rasterio draws one when Planetary Computer's pixels fail the plausibility gate); the API
imports both only on use, so they cost nothing at cold start. Keys are stored as encrypted Vercel environment variables, sent on
stdin so they never appear in a command line or in this script's output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
API_DIR = REPO_ROOT / "services" / "api"
PROJECT = "blindspot-api"

# Top-level packages the web process may import. Versions are pinned from uv.lock below.
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
# The project's production address: the absolute picture URLs the API hands out (PUBLIC_BASE_URL).
PUBLIC_URL = "https://blindspot-api-azure.vercel.app"
# Where the function runs: Singapore, beside Neon (aws ap-southeast-1) and near the users in India.
# Vercel's default (iad1, Washington) puts ~220 ms on every database round trip. Hobby allows one region.
FUNCTION_REGION = "sin1"
# The website's production address and its own deployments (blindspot-<hash>-<team>,
# blindspot-web-git-<branch>-<team>). Not every *.vercel.app name that starts with "blindspot":
# anyone can register one of those.
WEB_ORIGIN_REGEX = r"^https://blindspot(-web-five|(-web)?-[a-z0-9-]+-shubaces-projects)\.vercel\.app$"
SECRET_NAMES = (
    "GEMINI_API_KEY", "FIRMS_MAP_KEY", "DATA_GOV_IN_API_KEY", "OPENAQ_API_KEY",
    "LSASAF_USERNAME", "LSASAF_PASSWORD",
)
FOUNDATION_FILES = [
    "__init__.py", "config.py", "db.py", "http.py", "provenance.py", "geo.py", "api/__init__.py",
    "api/main.py", "api/responses.py", "api/routers/__init__.py", "jobs/__init__.py",
    "models/__init__.py", "models/core.py", "sources/__init__.py", "analytics/__init__.py",
    "agent/__init__.py",
]

# Vercel's zero-config FastAPI support looks for `app` in a root index.py and routes every path to
# it with the original request path (a rewrite to /api/index would hand the app that path instead).
INDEX_PY = '''"""Vercel entry point: the BlindSpot FastAPI app (ASGI), served at every path."""
from blindspot.api.main import app  # noqa: F401
'''


def locked_versions() -> dict[str, str]:
    lock = tomllib.loads((API_DIR / "uv.lock").read_text(encoding="utf-8"))
    return {pkg["name"]: pkg["version"] for pkg in lock.get("package", []) if "version" in pkg}


def direct_neon_url(url: str) -> str:
    """Neon's '-pooler' host is PgBouncer (transaction mode); our advisory locks need a direct session."""
    return re.sub(r"-pooler(\.)", r"\1", url, count=1)


def run_vercel(args: list[str], cwd: Path, token: str, stdin: str | None = None, check: bool = True) -> str:
    npx = shutil.which("npx") or "npx"
    env = {**os.environ, "VERCEL_TOKEN": token}
    proc = subprocess.run(
        [npx, "--yes", "vercel@65.8.0", *args, "--token", token],
        cwd=cwd, env=env, input=stdin, capture_output=True, text=True, shell=False,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    out = out.replace(token, "***")
    if check and proc.returncode != 0:
        raise RuntimeError(f"vercel {' '.join(args[:2])} failed ({proc.returncode}):\n{out[-1500:]}")
    return out


def assemble(target: Path, foundation_only: bool) -> None:
    pkg_src = API_DIR / "blindspot"
    files = FOUNDATION_FILES if foundation_only else [
        str(p.relative_to(pkg_src)).replace("\\", "/") for p in pkg_src.rglob("*.py") if "__pycache__" not in p.parts
    ]
    for rel in files:
        dst = target / "blindspot" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pkg_src / rel, dst)
    if not foundation_only:  # the native libraries the runtime lacks (blindspot/_vendor/README.md)
        for src in (pkg_src / "_vendor").iterdir():
            if src.is_file() and src.suffix not in (".py", ".pyc"):
                shutil.copy2(src, target / "blindspot" / "_vendor" / src.name)
    (target / "index.py").write_text(INDEX_PY, encoding="utf-8")
    versions = locked_versions()
    reqs = [f"{name}=={versions[name]}" for name in WEB_PACKAGES if name in versions]
    (target / "requirements.txt").write_text("\n".join(reqs) + "\n", encoding="utf-8")
    vercel_json = {"regions": [FUNCTION_REGION], "functions": {"index.py": {"maxDuration": 60}}}
    (target / "vercel.json").write_text(json.dumps(vercel_json, indent=2), encoding="utf-8")
    (target / ".vercelignore").write_text("**/__pycache__\n*.pyc\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--foundation-only", action="store_true")
    parser.add_argument("--cors", default="", help="extra allowed origins, comma-separated")
    parser.add_argument("--skip-env", action="store_true", help="do not rewrite environment variables")
    parser.add_argument("--public-url", default=PUBLIC_URL, help="the API's production address")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    token = (os.getenv("VERCEL_TOKEN") or "").strip()
    neon = (os.getenv("NEON_DB") or "").strip().strip("'\"")
    if not token or not neon:
        print("VERCEL_TOKEN and NEON_DB must be set in .env", file=sys.stderr)
        return 2

    # A fixed work folder (not a TemporaryDirectory): on Windows the Vercel CLI can keep a handle
    # open briefly, which makes temp-dir cleanup fail. Rebuilt from scratch on every run.
    work = Path(os.getenv("BLINDSPOT_DEPLOY_DIR") or Path(tempfile.gettempdir()) / "blindspot-vercel-api")
    if work.exists():
        shutil.rmtree(work / "blindspot", ignore_errors=True)
        shutil.rmtree(work / "api", ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    assemble(work, args.foundation_only)
    print(f"bundle: {sum(1 for _ in work.rglob('*.py'))} python files; requirements:",
          (work / "requirements.txt").read_text().replace("\n", " "))

    print(run_vercel(["link", "--yes", "--project", PROJECT], work, token).strip().splitlines()[-1])

    if not args.skip_env:
        origins = ["http://localhost:3000"] + [o.strip() for o in args.cors.split(",") if o.strip()]
        values = {name: (os.getenv(name) or "").strip() for name in SECRET_NAMES}
        values.update({
            "DATABASE_URL": direct_neon_url(neon),
            "ENABLE_SCHEDULER": "0",
            "INIT_DB_ON_STARTUP": "false",  # the jobs create the tables; a cold start must not
            "CACHE_DIR": "/tmp/blindspot-cache",
            "PUBLIC_BASE_URL": args.public_url.rstrip("/"),  # picture URLs; otherwise http://localhost:8000
            "CORS_ORIGINS": json.dumps(origins),
            "CORS_ORIGIN_REGEX": WEB_ORIGIN_REGEX,
        })
        for name, value in values.items():
            if not value:
                continue
            run_vercel(["env", "rm", name, "production", "--yes"], work, token, check=False)
            run_vercel(["env", "add", name, "production"], work, token, stdin=value)
        print(f"env set: {sorted(n for n, v in values.items() if v)}")

    out = run_vercel(["deploy", "--prod", "--yes"], work, token)
    urls = re.findall(r"https://[a-z0-9.-]+\.vercel\.app", out)
    print("deployed:", sorted(set(urls)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
