"""HTTP access with an on-disk cache and per-host politeness delays.

All public APIs used here are rate-limited; the cache makes reruns cheap and the
per-host minimum interval keeps the tool within each API's published limits.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import progress

USER_AGENT = "researchpipe/0.1 (research tooling discovery)"

# Minimum seconds between requests per host (published or conservative limits).
HOST_INTERVAL = {
    "export.arxiv.org": 3.1,
    "api.semanticscholar.org": 1.1,
    "api.openalex.org": 0.15,
    "api.github.com": 0.8,
    "www.usenix.org": 1.0,
}


class Net:
    def __init__(self, cache_dir: Path, ttl_hours: float = 168, offline: bool = False, verbose: bool = False):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_hours * 3600
        self.offline = offline
        self.verbose = verbose
        self._last: dict[str, float] = {}
        self.client = httpx.Client(timeout=40, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        self.github_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or _gh_cli_token()
        self.s2_key = os.environ.get("S2_API_KEY")
        self.openalex_key = os.environ.get("OPENALEX_API_KEY")
        self.github_search_remaining: int | None = None

    # -- cache ----------------------------------------------------------------
    def _cache_path(self, key: str) -> Path:
        h = hashlib.sha256(key.encode()).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.json"

    def cache_get(self, key: str):
        p = self._cache_path(key)
        if p.exists() and (self.offline or time.time() - p.stat().st_mtime < self.ttl):
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return None
        return None

    def cache_put(self, key: str, value) -> None:
        p = self._cache_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value))

    # -- requests -------------------------------------------------------------
    def _throttle(self, host: str) -> None:
        gap = HOST_INTERVAL.get(host, 0.5)
        wait = self._last.get(host, 0) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.time()

    def get(self, url: str, params: dict | None = None, kind: str = "json", headers: dict | None = None):
        """GET with caching. kind='json' returns parsed JSON, 'text' returns str. None on failure."""
        req = self.client.build_request("GET", url, params=params)
        key = f"{kind}:{req.url}"
        cached = self.cache_get(key)
        if cached is not None:
            return cached["body"]
        if self.offline:
            return None

        host = urlparse(str(req.url)).hostname or ""
        hdrs = dict(headers or {})
        if host == "api.github.com" and self.github_token:
            hdrs["Authorization"] = f"Bearer {self.github_token}"
            hdrs["Accept"] = "application/vnd.github+json"
        if host == "api.semanticscholar.org" and self.s2_key:
            hdrs["x-api-key"] = self.s2_key

        attempts = 6 if host == "api.semanticscholar.org" else 4  # shared unauthenticated pool often 429s
        for attempt in range(attempts):
            self._throttle(host)
            try:
                r = self.client.get(str(req.url), headers=hdrs)
            except httpx.HTTPError as e:
                progress.get().wait(f"{host}: {e.__class__.__name__}, retrying")
                time.sleep(2 * (attempt + 1))
                continue
            if host == "api.github.com" and "x-ratelimit-remaining" in r.headers:
                if "/search/" in str(req.url):
                    self.github_search_remaining = int(r.headers["x-ratelimit-remaining"])
                    if self.github_search_remaining <= 1:
                        reset = int(r.headers.get("x-ratelimit-reset", time.time() + 60))
                        pause = max(1, reset - time.time() + 1)
                        progress.get().wait(f"GitHub search rate limit reached, pausing {pause:.0f}s")
                        time.sleep(min(pause, 90))
            if r.status_code == 200:
                body = r.json() if kind == "json" else r.text
                self.cache_put(key, {"body": body})
                return body
            if r.status_code in (403, 429, 500, 502, 503, 504):
                retry_after = r.headers.get("retry-after")
                pause = float(retry_after) if retry_after and retry_after.isdigit() else 3 * (attempt + 1) ** 2
                progress.get().wait(f"{host} HTTP {r.status_code} (rate limited), retrying in {pause:.0f}s")
                time.sleep(min(pause, 90))
                continue
            if r.status_code == 404:
                self.cache_put(key, {"body": None})  # negative-cache missing resources
            else:
                progress.get().warn(f"{host} HTTP {r.status_code} for {req.url}")
            return None
        progress.get().warn(f"{host}: gave up after {attempts} attempts")
        return None

def _gh_cli_token() -> str | None:
    """Reuse the GitHub CLI's login if present, so no token setup is needed."""
    if not shutil.which("gh"):
        return None
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        tok = out.stdout.strip()
        return tok or None
    except (OSError, subprocess.SubprocessError):
        return None
