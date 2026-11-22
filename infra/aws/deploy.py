"""Deploy BlindSpot (API, website, relay, Caddy) to its AWS server from this working tree.

Run from services/api (python-dotenv; the server must exist: infra/aws/provision.py):
    cd services/api && uv run python ../../infra/aws/deploy.py --setup   # first time (setup is safe to repeat)
    uv run python ../../infra/aws/deploy.py                  # code changes: API, relay, website
    uv run python ../../infra/aws/deploy.py --only api       # or web / relay / caddy
    uv run python ../../infra/aws/deploy.py --only none --github-secret   # jobs' DATABASE_URL -> here
    uv run python ../../infra/aws/deploy.py --only none --indexnow        # tell search engines

The server serves https://www.blindspotin.tech (Next.js), https://api.blindspotin.tech (FastAPI) and
https://api.blindspotin.tech/relay (infra/relay, for the jobs on GitHub's US runners) through Caddy.
Every step runs on the server as one systemd job whose log streams back here, so a dropped SSH
connection neither stops it nor loses its result. Website builds go beside the live copy and switch
over when done; an unchanged website is not rebuilt. NEXT_PUBLIC_GA_ID and the search-engine
verification codes, when set in .env, are baked into the website (WEB_PUBLIC_FROM_ENV).
Keys go from .env to /etc/blindspot/api.env over SSH on stdin; nothing secret is printed. The API
gets the web-only package set (infra/api_common.py) plus uvicorn; the job libraries stay on GitHub
Actions. (The one-off copy of the Neon database, 2026-11-22, is in this file's git history.)
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "infra"))
sys.path.insert(0, str(REPO_ROOT / "infra" / "aws"))
import api_common  # noqa: E402  (WEB_PACKAGES, locked_versions, SECRET_NAMES, WEB_ORIGIN_REGEX)
import provision  # noqa: E402
from set_github_secrets import set_secret  # noqa: E402

API_DIR = REPO_ROOT / "services" / "api"
WEB_DIR = REPO_ROOT / "apps" / "web"
RELAY_DIR = REPO_ROOT / "infra" / "relay"
OPS_DIR = REPO_ROOT / "infra" / "aws"

SITE_URL = "https://www.blindspotin.tech"
API_URL = "https://api.blindspotin.tech"
SERVER_PACKAGES = ["uvicorn", "uvloop", "httptools"]  # what Vercel's runtime provided
# Baked into the website at build time when set in the repo-root .env (all public values: they end up
# in every page's HTML). GA: a Google Analytics 4 measurement ID; the other two: the content of the
# verification meta tags Google Search Console and Bing Webmaster Tools offer.
WEB_PUBLIC_FROM_ENV = ("NEXT_PUBLIC_GA_ID", "NEXT_PUBLIC_GOOGLE_SITE_VERIFICATION", "NEXT_PUBLIC_BING_SITE_VERIFICATION")
INDEXNOW_KEY_FILE = "451e281a7d80a4734d312294ecbc0856.txt"  # apps/web/public/<key>.txt holds the key

WEB_SKIP = frozenset({"node_modules", ".next", ".vercel", "tsconfig.tsbuildinfo"})
TEXT_SUFFIXES = {".sh", ".service", ".timer", ".js", ""}  # LF on the server, whatever this checkout has (Caddyfile: "")

UNIT = "bs-deploy"  # the systemd job every deploy runs as, with its log and exit code beside it
LOG, RC = f"/var/log/{UNIT}.log", f"/var/log/{UNIT}.rc"
END = b"\n@@rc="  # what follow-mode prints after the log once the job has ended, then its exit code


def allow_ssh(group_id: str) -> None:
    """Let this machine's current /24 use SSH. VPNs such as Cloudflare WARP move the address between
    neighbours during a session; logging in still needs the private key."""
    provision.allow(group_id, 22, provision.my_ip().rsplit(".", 1)[0] + ".0/24", "ssh from the deployer")


class Server:
    """SSH to the server as `ubuntu`; every script runs as root."""

    def __init__(self, ip: str, group_id: str) -> None:
        self.group_id = group_id
        self.base = [shutil.which("ssh") or "ssh", "-i", str(provision.KEY_PATH), "-o", "StrictHostKeyChecking=accept-new",
                     "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=8",
                     "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", f"ubuntu@{ip}"]

    def _remote(self, script: str) -> list[str]:
        return self.base + ["sudo bash -c " + shlex.quote(script)]

    def _reconnect(self, what: str) -> None:
        print(f"ssh {what}; allowing this IP's /24 and retrying", file=sys.stderr)
        time.sleep(5)
        allow_ssh(self.group_id)

    def run(self, script: str, stdin: bytes = b"", quiet: bool = False) -> str:
        """Run a short bash script; stream its output unless quiet (then return it)."""
        for _attempt in range(6):  # 255: ssh could not connect or lost the link; every step is idempotent
            proc = subprocess.run(self._remote("set -euo pipefail\n" + script), input=stdin, capture_output=quiet)
            if proc.returncode != 255:
                break
            self._reconnect("failed")
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode(errors="replace")[-1500:] if quiet else ""
            raise RuntimeError(f"remote step failed ({proc.returncode}) {detail}")
        return (proc.stdout or b"").decode(errors="replace") if quiet else ""

    def run_detached(self, script: str) -> None:
        """Run a long script as a systemd job and stream its log over one SSH connection, picking up
        from the last byte received if the connection drops (the job itself keeps running)."""
        self.run(f"cat > /root/{UNIT}.sh; rm -f {RC} {LOG}; systemctl reset-failed {UNIT} 2>/dev/null || true\n"
                 f"systemd-run --quiet --unit={UNIT} --collect --property=Type=exec bash -c "
                 f"'bash /root/{UNIT}.sh > {LOG} 2>&1; echo $? > {RC}'",
                 stdin=("set -euo pipefail\n" + script).encode())
        offset = 0
        for _attempt in range(40):
            # On the server: print the log from `offset` as it grows; once the exit code exists (the
            # log is then complete), print the rest, END and the code, and stop.
            follow = (f"off={offset}; while :; do done=0; [ -f {RC} ] && done=1; "
                      f"size=$(stat -c %s {LOG} 2>/dev/null || echo 0); "
                      f"if [ \"$size\" -gt \"$off\" ]; then tail -c +$((off + 1)) {LOG} | head -c $((size - off)); off=$size; fi; "
                      f"if [ $done = 1 ]; then printf '\\n@@rc=%s\\n' \"$(cat {RC})\"; exit 0; fi; sleep 3; done")
            proc = subprocess.Popen(self._remote(follow), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
            pending = b""
            for chunk in iter(lambda: proc.stdout.read1(65536), b""):
                pending += chunk
                if END in pending:
                    break
                # print all but a tail that could be the start of END
                safe = len(pending) - (len(END) - 1)
                if safe > 0:
                    offset += self._emit(pending[:safe])
                    pending = pending[safe:]
            rest = proc.stdout.read()
            proc.wait()
            pending += rest
            if END in pending:
                log, _, code = pending.partition(END)
                self._emit(log)
                code = code.decode(errors="replace").strip()
                if code != "0":
                    raise RuntimeError(f"the deploy job failed with exit code {code} (server log: {LOG})")
                return
            offset += self._emit(pending)  # the link dropped mid-stream: all of it was log
            self._reconnect("lost the log stream")
        raise RuntimeError(f"lost the server for too long; the job may still be running (server log: {LOG})")

    @staticmethod
    def _emit(data: bytes) -> int:
        sys.stdout.flush()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return len(data)


def add_tree(tar: tarfile.TarFile, src: Path, arc: str, skip: frozenset[str] = frozenset()) -> None:
    """Add src's files under arc/, never walking into skipped folders (node_modules, .next...) or
    __pycache__, and leaving out .pyc files and .env* files (local secrets: apps/web/.env.local
    holds a Vercel CLI token)."""
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in skip and d != "__pycache__")
        for name in sorted(files):
            if name in skip or name.endswith(".pyc") or name.startswith(".env"):
                continue
            path = Path(root) / name
            add_bytes(tar, f"{arc}/{path.relative_to(src).as_posix()}", path.read_bytes())


def add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    if Path(name).suffix in TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), mode, int(time.time())
    tar.addfile(info, io.BytesIO(data))


def requirements() -> str:
    versions = api_common.locked_versions()
    return "".join(f"{n}=={versions[n]}\n" for n in api_common.WEB_PACKAGES + SERVER_PACKAGES if n in versions)


def bundle(parts: set[str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        add_tree(tar, OPS_DIR, "ops")
        if "api" in parts:
            add_tree(tar, API_DIR / "blindspot", "api/blindspot", skip=frozenset({"libexpat.so.1"}))  # x86 copy, unused on arm64
            add_bytes(tar, "api/requirements.txt", requirements().encode())
        if "relay" in parts:
            add_tree(tar, RELAY_DIR / "api", "relay/api")
            add_bytes(tar, "relay/relay_server.js", (OPS_DIR / "relay_server.js").read_bytes())
        if "web" in parts:
            add_tree(tar, WEB_DIR, "web", skip=WEB_SKIP)
            web_env = (f"NEXT_PUBLIC_API_URL={API_URL}\nAPI_INTERNAL_URL=http://127.0.0.1:8000\n"
                       f"NEXT_PUBLIC_SITE_URL={SITE_URL}\n")
            for name in WEB_PUBLIC_FROM_ENV:  # analytics and search-engine verification, when set in .env
                value = (os.getenv(name) or "").strip()
                if value:
                    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
                        sys.exit(f"{name} in .env does not look like an ID or a verification code")
                    web_env += f"{name}={value}\n"
            add_bytes(tar, "ops/web.env", web_env.encode())
    return buf.getvalue()


def api_env() -> bytes:
    """/etc/blindspot/api.env: the API's settings and keys (DATABASE_URL is in db.env, made on the server)."""
    def q(value: str) -> str:
        if "'" in value or "\n" in value:
            raise ValueError("a value with a quote or newline cannot go in a systemd environment file")
        return f"'{value}'"

    values = {name: (os.getenv(name) or "").strip() for name in api_common.SECRET_NAMES}
    values.update({
        "ENABLE_SCHEDULER": "0",
        "INIT_DB_ON_STARTUP": "false",  # the jobs create the tables
        "BACKGROUND_REBUILDS": "false",  # the jobs do job work (featured frames); not the 1 GB server's API
        "GZIP_RESPONSES": "false",  # Caddy compresses
        "CACHE_DIR": "/srv/blindspot/cache",
        "PUBLIC_BASE_URL": API_URL,
        "CORS_ORIGINS": json.dumps(["http://localhost:3000"]),
        "CORS_ORIGIN_REGEX": api_common.WEB_ORIGIN_REGEX,
        "LOG_LEVEL": "INFO",
    })
    return "".join(f"{k}={q(v)}\n" for k, v in values.items() if v).encode()


STEP_UNPACK = """
rm -rf /tmp/bs && mkdir -p /tmp/bs && tar xzf - -C /tmp/bs
"""
STEP_API = """
rsync -a --delete /tmp/bs/api/blindspot/ /srv/blindspot/api/blindspot/
install -m 644 /tmp/bs/api/requirements.txt /srv/blindspot/api/requirements.txt
chown -R blindspot:blindspot /srv/blindspot/api
cd /srv/blindspot
[ -x /srv/blindspot/venv/bin/python ] || sudo -u blindspot -H uv venv --quiet --python 3.12 /srv/blindspot/venv
sudo -u blindspot -H uv pip install --quiet --python /srv/blindspot/venv/bin/python -r /srv/blindspot/api/requirements.txt
install -m 644 /tmp/bs/ops/blindspot-api.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --quiet blindspot-api && systemctl restart blindspot-api
curl -fsS --retry 60 --retry-delay 2 --retry-all-errors --max-time 20 http://127.0.0.1:8000/api/health | head -c 400; echo
"""
STEP_RELAY = """
rsync -a --delete /tmp/bs/relay/ /srv/blindspot/relay/
chown -R blindspot:blindspot /srv/blindspot/relay
install -m 644 /tmp/bs/ops/blindspot-relay.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --quiet blindspot-relay && systemctl restart blindspot-relay
"""
STEP_WEB = """
# Blue/green: build beside the live copy (web-a / web-b, each at its final path), then point
# /srv/blindspot/web at it and restart. The site keeps serving during the build, which takes a few
# minutes on the 1 GB server, and a failed build leaves the live copy untouched. A website whose
# files and build settings (web.env) match the live build's is not rebuilt.
install -m 644 /tmp/bs/ops/web.env /etc/blindspot/web.env
install -m 644 /tmp/bs/ops/blindspot-web.service /etc/systemd/system/
systemctl daemon-reload
cd /srv/blindspot
LIVE=$(readlink -f web 2>/dev/null || true)
HASH=$( (cd /tmp/bs/web && find . -type f -print0 | sort -z | xargs -0 sha256sum; cat /etc/blindspot/web.env) | sha256sum | cut -c1-64)
if [ -f "$LIVE/.build-hash" ] && [ "$(cat "$LIVE/.build-hash")" = "$HASH" ]; then
  echo "website unchanged: not rebuilt ($(readlink web))"
else
  if [ "$LIVE" = /srv/blindspot/web-a ]; then NEW=web-b; else NEW=web-a; fi
  rm -rf "$NEW" && mkdir -p "$NEW"
  rsync -a /tmp/bs/web/ "$NEW"/
  if [ -d "$LIVE/.next/cache" ]; then mkdir -p "$NEW/.next" && cp -a "$LIVE/.next/cache" "$NEW/.next/"; fi
  chown -R blindspot:blindspot "$NEW"
  cd "/srv/blindspot/$NEW"
  sudo -u blindspot -H env COREPACK_ENABLE_DOWNLOAD_PROMPT=0 NEXT_TELEMETRY_DISABLED=1 CI=1 bash -c '
    set -euo pipefail; set -a; . /etc/blindspot/web.env; set +a
    pnpm install --frozen-lockfile --reporter=append-only
    NODE_OPTIONS=--max-old-space-size=1536 pnpm build'
  echo "$HASH" > .build-hash
  systemctl stop blindspot-web 2>/dev/null || true
  cd /srv/blindspot && rm -rf web && ln -s "$NEW" web && chown -h blindspot:blindspot web
  systemctl enable --quiet blindspot-web && systemctl start blindspot-web
  curl -fsS -o /dev/null --retry 60 --retry-delay 2 --retry-all-errors --max-time 30 http://127.0.0.1:3000/
  case "$LIVE" in /srv/blindspot/web-a|/srv/blindspot/web-b) rm -rf "$LIVE" ;; esac
  echo "website: $(readlink web)"
fi
"""
STEP_CADDY = """
install -m 644 /tmp/bs/ops/Caddyfile /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
systemctl reload caddy || systemctl restart caddy
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--setup", action="store_true", help="install the server's software first (safe to repeat)")
    parser.add_argument("--only", choices=["api", "web", "relay", "caddy", "none"], action="append",
                        help="deploy only these parts (none: just the other flags' actions)")
    parser.add_argument("--github-secret", action="store_true", help="set the repo's DATABASE_URL secret to this server")
    parser.add_argument("--indexnow", action="store_true",
                        help="after deploying, send the live sitemap's URLs to IndexNow (Bing, Yandex, Seznam, Naver)")
    args = parser.parse_args()
    load_dotenv(REPO_ROOT / ".env")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # build logs carry ▲ ✓; Windows' console is cp1252

    inst = provision.find_instance()
    if not inst or not inst.get("PublicIpAddress"):
        sys.exit("no running server: create or start it with infra/aws/provision.py")
    ip, group_id = inst["PublicIpAddress"], inst["SecurityGroups"][0]["GroupId"]
    allow_ssh(group_id)
    srv = Server(ip, group_id)
    parts = set(args.only or ["api", "relay", "web", "caddy"]) - {"none"}
    print(f"server {ip}: {', '.join(sorted(parts)) or 'no parts'}{' + setup' if args.setup else ''}")
    if parts or args.setup:
        deploy(srv, parts, setup=args.setup)
    if args.github_secret:
        set_github_secret(srv, ip)
    if args.indexnow:
        submit_indexnow()
    print(f"done. {SITE_URL}  {API_URL}/api/health")
    return 0


def deploy(srv: Server, parts: set[str], setup: bool) -> None:
    """Upload this tree's parts, then run every step as one job on the server (run_detached)."""
    srv.run(STEP_UNPACK, stdin=bundle(parts))
    if "api" in parts:  # keys: root-only until the job moves them into /etc/blindspot
        srv.run("umask 077; cat > /root/bs-api.env", stdin=api_env())
    steps = ["bash /tmp/bs/ops/setup_server.sh"] if setup else []
    if "api" in parts:
        steps += ["install -m 640 -o root -g blindspot /root/bs-api.env /etc/blindspot/api.env && rm -f /root/bs-api.env",
                  STEP_API]
    if "relay" in parts:
        steps.append(STEP_RELAY)
    if "web" in parts:
        steps.append(STEP_WEB)
    if "caddy" in parts:
        steps.append(STEP_CADDY)
    steps.append("rm -rf /tmp/bs; echo 'all steps done'")
    srv.run_detached("\n".join(steps))


def set_github_secret(srv: Server, ip: str) -> None:
    """Point the data jobs (GitHub Actions) at this server's database, over TLS."""
    match = re.search(r"blindspot:([0-9a-f]+)@", srv.run("cat /etc/blindspot/db.env", quiet=True))
    if not match:
        sys.exit("could not read the database password from the server")
    url = f"postgresql://blindspot:{match.group(1)}@{ip}:5432/blindspot?sslmode=require"
    print("GitHub secret DATABASE_URL:", set_secret("DATABASE_URL", url))


def submit_indexnow() -> None:
    """Tell IndexNow (Bing, and through it DuckDuckGo and Yahoo; Yandex, Seznam, Naver) about every
    URL in the live sitemap. The key file (apps/web/public/<key>.txt) proves the site is ours."""
    import urllib.error
    import urllib.request
    import xml.etree.ElementTree as ET
    from urllib.parse import urlparse

    with urllib.request.urlopen(f"{SITE_URL}/sitemap.xml", timeout=60) as resp:
        tree = ET.fromstring(resp.read())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = [e.text.strip() for e in tree.findall("s:url/s:loc", ns) if e.text]
    body = json.dumps({
        "host": urlparse(SITE_URL).hostname,
        "key": INDEXNOW_KEY_FILE.removesuffix(".txt"),
        "keyLocation": f"{SITE_URL}/{INDEXNOW_KEY_FILE}",
        "urlList": urls[:10_000],
    }).encode()
    req = urllib.request.Request("https://api.indexnow.org/indexnow", data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            print(f"IndexNow: HTTP {resp.status} for {len(urls)} URLs (200/202 = accepted)")
    except urllib.error.HTTPError as err:
        print(f"IndexNow refused: HTTP {err.code} {err.read()[:300]!r}")


if __name__ == "__main__":
    raise SystemExit(main())
