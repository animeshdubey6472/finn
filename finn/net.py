"""Polite HTTP: an honest User-Agent, per-host pacing, retries, and an on-disk cache.

Every source FINN reads is a public page or file. We identify ourselves, never fetch a
host faster than once a second, and cache aggressively so re-runs cost the source nothing.
Filings are immutable, so PDFs are cached forever; listing pages get a short TTL.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import CACHE_DIR

# NSE resets connections whose User-Agent contains a URL, so the identification stays plain.
USER_AGENT = "FINN-prototype/0.1 (portfolio research demo)"
# BSE answers bursts with 406; ~1 request every 2.5s is reliably accepted.
MIN_INTERVAL = {"www.screener.in": 1.2, "www.bseindia.com": 2.5}
TRANSIENT = {406, 429, 500, 502, 503, 504}
SLOW_HOSTS = {"www.bseindia.com"}
DEFAULT_INTERVAL = 0.3


class FetchError(RuntimeError):
    def __init__(self, url: str, reason: str, status: int | None = None):
        super().__init__(f"{reason} [{status}] {url}" if status else f"{reason} {url}")
        self.url, self.reason, self.status = url, reason, status


@dataclass
class Fetched:
    url: str
    content: bytes
    content_type: str
    from_cache: bool
    fetched_at: float

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class Fetcher:
    def __init__(self, cache_dir: Path | None = None, offline: bool = False):
        self.cache_dir = Path(cache_dir or CACHE_DIR / "http")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        self._host_locks: dict[str, threading.Lock] = {}
        self._last_hit: dict[str, float] = {}
        self._guard = threading.Lock()
        self.stats = {"network": 0, "cache": 0, "bytes": 0}

    # -- cache -------------------------------------------------------------------
    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.bin", self.cache_dir / f"{key}.json"

    def cached(self, url: str, max_age: float | None) -> Fetched | None:
        body, meta = self._paths(url)
        if not (body.exists() and meta.exists()):
            return None
        info = json.loads(meta.read_text())
        if max_age is not None and not self.offline and time.time() - info["fetched_at"] > max_age:
            return None
        return Fetched(url, body.read_bytes(), info.get("content_type", ""), True, info["fetched_at"])

    def _store(self, url: str, content: bytes, content_type: str) -> None:
        body, meta = self._paths(url)
        body.write_bytes(content)
        meta.write_text(json.dumps({"url": url, "fetched_at": time.time(), "content_type": content_type}))

    # -- pacing ------------------------------------------------------------------
    def _wait_turn(self, host: str) -> threading.Lock:
        with self._guard:
            lock = self._host_locks.setdefault(host, threading.Lock())
        lock.acquire()
        gap = MIN_INTERVAL.get(host, DEFAULT_INTERVAL) - (time.time() - self._last_hit.get(host, 0.0))
        if gap > 0:
            time.sleep(gap)
        return lock

    # -- fetch -------------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        max_age: float | None = None,
        timeout: float = 30,
        max_bytes: int = 40_000_000,
        budget_s: float = 120,
        retries: int = 2,
    ) -> Fetched:
        """GET with cache. `max_age=None` means "cached forever" (right for filings).

        `max_bytes` and `budget_s` bound large downloads (annual reports can exceed 50 MB);
        exceeding either raises FetchError rather than stalling a run.
        """
        hit = self.cached(url, max_age)
        if hit:
            self.stats["cache"] += 1
            return hit
        if self.offline:
            raise FetchError(url, "offline and not cached")

        host = urlparse(url).netloc
        if host in SLOW_HOSTS:
            retries = max(retries, 4)
        last: FetchError | None = None
        for attempt in range(retries + 1):
            lock = self._wait_turn(host)
            try:
                started = time.time()
                with self.session.get(url, timeout=timeout, stream=True, allow_redirects=True) as resp:
                    self._last_hit[host] = time.time()
                    if resp.status_code in TRANSIENT:
                        last = FetchError(url, "transient error", resp.status_code)
                    elif resp.status_code != 200:
                        raise FetchError(url, "http error", resp.status_code)
                    else:
                        chunks, size = [], 0
                        for chunk in resp.iter_content(chunk_size=65536):
                            chunks.append(chunk)
                            size += len(chunk)
                            if size > max_bytes:
                                raise FetchError(url, f"larger than {max_bytes // 1_000_000} MB")
                            if time.time() - started > budget_s:
                                raise FetchError(url, f"slower than {budget_s:.0f}s budget")
                        content = b"".join(chunks)
                        ctype = resp.headers.get("content-type", "")
                        self._store(url, content, ctype)
                        self.stats["network"] += 1
                        self.stats["bytes"] += len(content)
                        return Fetched(url, content, ctype, False, time.time())
            except requests.RequestException as exc:
                last = FetchError(url, f"network error: {type(exc).__name__}")
            finally:
                self._last_hit[host] = time.time()
                lock.release()
            if attempt < retries:
                # BSE answers bursts with 406: back off harder there than elsewhere.
                time.sleep((8.0 if host in SLOW_HOSTS else 4.0) * (attempt + 1))
        raise last or FetchError(url, "failed")


_default: Fetcher | None = None


def fetcher() -> Fetcher:
    global _default
    if _default is None:
        _default = Fetcher()
    return _default
