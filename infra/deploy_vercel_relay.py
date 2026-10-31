"""Deploy the data.gov.in relay (infra/relay) to Vercel production, in Mumbai (bom1).

data.gov.in refuses connections from GitHub's runners (US), where the data jobs run, so the air job
reaches it through this one function (DATAGOV_RELAY_URL). Run from services/api so python-dotenv is
available:
    cd services/api && uv run python ../../infra/deploy_vercel_relay.py

The token is read from .env (VERCEL_TOKEN) and never printed. The relay needs no environment
variables: the caller sends its own data.gov.in key.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_vercel_web import run_vercel  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
RELAY_DIR = REPO_ROOT / "infra" / "relay"
PROJECT = "blindspot-relay"
FUNCTION_REGION = "bom1"  # Mumbai: an Indian address, which data.gov.in accepts


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    token = (os.getenv("VERCEL_TOKEN") or "").strip()
    if not token:
        print("VERCEL_TOKEN must be set in .env", file=sys.stderr)
        return 2

    vercel_json = RELAY_DIR / "vercel.json"
    vercel_json.write_text(json.dumps({"regions": [FUNCTION_REGION]}, indent=2) + "\n", encoding="utf-8")
    print(run_vercel(["link", "--yes", "--project", PROJECT], RELAY_DIR, token).strip().splitlines()[-1])
    out = run_vercel(["deploy", "--prod", "--yes"], RELAY_DIR, token)
    urls = re.findall(r"https://[a-z0-9.-]+\.vercel\.app", out)
    print("deployed:", sorted(set(urls)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
