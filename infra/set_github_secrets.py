"""Copy the keys GitHub Actions needs from .env into the repo's encrypted Actions secrets.

Run it yourself (it writes to GitHub's secret store):
    cd services/api && uv run python ../../infra/set_github_secrets.py

Needs the GitHub CLI logged in (gh auth login). Values go to gh on stdin and are never printed.
DATABASE_URL is the Neon *direct* endpoint (no '-pooler'): the jobs use session-level locks.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from dotenv import dotenv_values

REPO = "ShubAce/BlindSpot"
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
NAMES = ("GEMINI_API_KEY", "FIRMS_MAP_KEY", "DATA_GOV_IN_API_KEY", "OPENAQ_API_KEY", "LSASAF_USERNAME", "LSASAF_PASSWORD")


def main() -> int:
    env = dotenv_values(ENV_FILE)
    gh = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
    secrets = {name: (env.get(name) or "").strip() for name in NAMES}
    neon = (env.get("NEON_DB") or "").strip().strip("'\"")
    secrets["DATABASE_URL"] = re.sub(r"-pooler(\.)", r"\1", neon, count=1)
    for name, value in secrets.items():
        if not value:
            print(f"skip {name} (empty in .env)")
            continue
        proc = subprocess.run([gh, "secret", "set", name, "--repo", REPO], input=value, capture_output=True, text=True)
        print(f"{name}: {'set' if proc.returncode == 0 else 'FAILED ' + proc.stderr.strip()[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
