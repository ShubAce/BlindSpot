"""Copy the keys GitHub Actions needs from .env into the repo's encrypted Actions secrets.

Run it yourself (it writes to GitHub's secret store):
    cd services/api && uv run python ../../infra/set_github_secrets.py

Needs the GitHub CLI logged in (gh auth login). Values go to gh on stdin and are never printed.
DATABASE_URL is not set here: the jobs write to the AWS server's database, whose password lives on
that server, so `infra/aws/deploy.py --only none --github-secret` sets it (with set_secret below).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from dotenv import dotenv_values

REPO = "ShubAce/BlindSpot"
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
NAMES = ("GEMINI_API_KEY", "FIRMS_MAP_KEY", "DATA_GOV_IN_API_KEY", "OPENAQ_API_KEY", "LSASAF_USERNAME", "LSASAF_PASSWORD")


def set_secret(name: str, value: str) -> str:
    """Store one Actions secret of the repo; returns "set" or why it failed (never the value)."""
    gh = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
    proc = subprocess.run([gh, "secret", "set", name, "--repo", REPO], input=value, capture_output=True, text=True)
    return "set" if proc.returncode == 0 else "FAILED " + proc.stderr.strip()[:120]


def main() -> int:
    env = dotenv_values(ENV_FILE)
    for name in NAMES:
        value = (env.get(name) or "").strip()
        print(f"{name}: {set_secret(name, value) if value else 'skipped (empty in .env)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
