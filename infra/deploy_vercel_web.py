"""Deploy the BlindSpot website (apps/web, Next.js) to Vercel production.

Run from services/api so python-dotenv is available:
    cd services/api && uv run python ../../infra/deploy_vercel_web.py [--api-url URL] [--site-url URL]

The site reads the API from the browser (NEXT_PUBLIC_API_URL) and from the server (API_INTERNAL_URL);
both point at the deployed API. Its server functions run in Singapore (sin1), beside the API and Neon.
The token is read from .env (VERCEL_TOKEN) and never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "apps" / "web"
PROJECT = "blindspot-web"
API_URL = "https://blindspot-api-azure.vercel.app"
FUNCTION_REGION = "sin1"


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
        raise RuntimeError(f"vercel {' '.join(args[:2])} failed ({proc.returncode}):\n{out[-2500:]}")
    return out


def link_once(folder: Path, project: str, token: str) -> None:
    """Link `folder` to `project` the first time only. `vercel link --yes` also connects the GitHub
    repository, after which every push deployed the repo root to production (it broke the relay on
    2026-10-31), so a fresh link is followed by a disconnect. The repo's root vercel.json disables
    Git deployments as well. We deploy from these scripts only."""
    if (folder / ".vercel" / "project.json").exists():
        return
    print(run_vercel(["link", "--yes", "--project", project], folder, token).strip().splitlines()[-1])
    run_vercel(["git", "disconnect", "--yes"], folder, token, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-url", default=API_URL, help="the deployed API's address")
    parser.add_argument("--site-url", default="", help="the site's own address (canonical and share-card URLs)")
    parser.add_argument("--skip-env", action="store_true", help="do not rewrite environment variables")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    token = (os.getenv("VERCEL_TOKEN") or "").strip()
    if not token:
        print("VERCEL_TOKEN must be set in .env", file=sys.stderr)
        return 2

    vercel_json = WEB_DIR / "vercel.json"
    vercel_json.write_text(json.dumps({"regions": [FUNCTION_REGION]}, indent=2) + "\n", encoding="utf-8")

    link_once(WEB_DIR, PROJECT, token)

    if not args.skip_env:
        api = args.api_url.rstrip("/")
        values = {"NEXT_PUBLIC_API_URL": api, "API_INTERNAL_URL": api}
        if args.site_url:
            values["NEXT_PUBLIC_SITE_URL"] = args.site_url.rstrip("/")
        for name, value in values.items():
            run_vercel(["env", "rm", name, "production", "--yes"], WEB_DIR, token, check=False)
            run_vercel(["env", "add", name, "production"], WEB_DIR, token, stdin=value)
        print(f"env set: {sorted(values)}")

    out = run_vercel(["deploy", "--prod", "--yes"], WEB_DIR, token)
    urls = re.findall(r"https://[a-z0-9.-]+\.vercel\.app", out)
    print("deployed:", sorted(set(urls)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
