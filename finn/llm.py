"""One small LLM client: tiered routing, fall-through on quota, disk cache, cost ledger.

Design choices that matter for cost:

* **Tiers, not models.** Callers ask for "fast" or "smart"; `config.routes()` maps each tier to
  an ordered list of models. A daily free-tier cap on one model degrades to the next.
* **Cache by content.** The cache key is the task, prompt and inputs — not the model — so a
  filing read once is never paid for again, even if routing changes.
* **JSON in, JSON out.** Every call returns parsed JSON; numbers are extracted by the model
  but all arithmetic happens in Python.
* **Ledger.** Every call (including cache hits) is recorded, so a run can report exactly
  what it used and what it would have cost at paid list prices.

Providers: Gemini's native REST API (supports PDF input and search grounding) and any
OpenAI-compatible endpoint (Groq, OpenRouter, Ollama, OpenAI) for text-only calls.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from . import config

OPENAI_COMPATIBLE = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "openai": (os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), "OPENAI_API_KEY"),
    "ollama": (os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"), None),
}


class LLMUnavailable(RuntimeError):
    """No configured model could serve the request (no key, or every candidate exhausted)."""


class _Quota(RuntimeError):
    def __init__(self, message: str, retry_after: float | None, daily: bool):
        super().__init__(message)
        self.retry_after, self.daily = retry_after, daily


@dataclass
class Attachment:
    data: bytes
    mime_type: str = "application/pdf"

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class Call:
    task: str
    tier: str
    model: str
    cached: bool
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0
    ts: float = field(default_factory=time.time)

    @property
    def list_cost(self) -> float:
        price_in, price_out = config.LIST_PRICES.get(self.model, (0.0, 0.0))
        return (self.tokens_in * price_in + self.tokens_out * price_out) / 1_000_000


def extract_json(text: str):
    """Parse model output as JSON, tolerating code fences and stray prose around it."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON found in model output")
    start = min(starts)
    closer = "}" if text[start] == "{" else "]"
    end = text.rfind(closer)
    if end <= start:
        raise ValueError("unterminated JSON in model output")
    return json.loads(text[start : end + 1])


class LLM:
    def __init__(self, cache_dir: Path | None = None, routes: dict[str, list[str]] | None = None):
        self.cache_dir = Path(cache_dir or config.CACHE_DIR / "llm")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.routes = routes or config.routes()
        self.calls: list[Call] = []
        self._exhausted: set[str] = set()
        self._last_start: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.session = requests.Session()
        self.read_timeout = float(os.environ.get("FINN_LLM_TIMEOUT", 150))

    # -- availability ------------------------------------------------------------
    def _usable(self, spec: str) -> bool:
        provider = spec.split(":", 1)[0]
        if spec in self._exhausted:
            return False
        if provider == "gemini":
            return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        if provider in OPENAI_COMPATIBLE:
            env = OPENAI_COMPATIBLE[provider][1]
            return env is None or bool(os.environ.get(env))
        return False

    def available(self, tier: str = "fast") -> bool:
        return any(self._usable(spec) for spec in self.routes.get(tier, []))

    # -- cache -------------------------------------------------------------------
    def _cache_path(self, task: str, system: str, user: str, attachments: list[Attachment]) -> Path:
        h = hashlib.sha256()
        for part in (task, system, user, *[a.digest for a in attachments]):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        return self.cache_dir / f"{task.split('/')[0]}-{h.hexdigest()[:24]}.json"

    # -- public API --------------------------------------------------------------
    def json(
        self,
        task: str,
        *,
        tier: str,
        system: str,
        user: str,
        attachments: list[Attachment] | None = None,
        max_tokens: int = 4096,
        search: bool = False,
        use_cache: bool = True,
    ):
        """Run a prompt and return parsed JSON. Raises LLMUnavailable if nothing can serve it."""
        attachments = attachments or []
        path = self._cache_path(task, system, user, attachments)
        if use_cache and path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            self.calls.append(Call(task, tier, record.get("model", "?"), True))
            return record["data"]

        errors: list[str] = []
        for spec in self.routes.get(tier, []):
            if not self._usable(spec):
                continue
            provider, model = spec.split(":", 1)
            if attachments and provider != "gemini":
                continue  # only the native Gemini path takes PDFs
            for attempt in range(3):
                try:
                    started = time.time()
                    self._pace(model)
                    text, t_in, t_out, extra = self._dispatch(provider, model, system, user, attachments, max_tokens, search)
                    data = extract_json(text)
                    if extra and isinstance(data, dict):
                        data.setdefault("_sources", extra)
                    self.calls.append(Call(task, tier, model, False, t_in, t_out, time.time() - started))
                    path.write_text(json.dumps({"model": model, "task": task, "data": data}, ensure_ascii=False), encoding="utf-8")
                    return data
                except _Quota as exc:
                    if exc.daily or attempt == 2:
                        self._exhausted.add(spec)
                        errors.append(f"{model}: quota exhausted")
                        break
                    time.sleep(min(exc.retry_after or 20, 65))
                except ValueError as exc:  # unparseable JSON: one more try, then move on
                    errors.append(f"{model}: {exc}")
                    if attempt >= 1:
                        break
                except requests.Timeout:
                    # A model that hangs once tends to keep hanging: stop asking it this run.
                    self._exhausted.add(spec)
                    errors.append(f"{model}: timed out")
                    break
                except requests.RequestException as exc:
                    errors.append(f"{model}: {type(exc).__name__}")
                    time.sleep(2 * (attempt + 1))
                except RuntimeError as exc:
                    errors.append(f"{model}: {exc}")
                    break
        raise LLMUnavailable("; ".join(errors) or f"no model configured for tier '{tier}' (is GEMINI_API_KEY set?)")

    # -- pacing ------------------------------------------------------------------
    def _pace(self, model: str) -> None:
        interval = 60.0 / config.RPM.get(model, config.DEFAULT_RPM)
        with self._guard:
            lock = self._locks.setdefault(model, threading.Lock())
        with lock:
            wait = interval - (time.time() - self._last_start.get(model, 0.0))
            if wait > 0:
                time.sleep(wait)
            self._last_start[model] = time.time()

    # -- providers ---------------------------------------------------------------
    def _dispatch(self, provider, model, system, user, attachments, max_tokens, search):
        if provider == "gemini":
            return self._gemini(model, system, user, attachments, max_tokens, search)
        return self._openai_compatible(provider, model, system, user, max_tokens)

    def _gemini(self, model, system, user, attachments, max_tokens, search):
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        base = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
        parts: list[dict] = [
            {"inlineData": {"mimeType": a.mime_type, "data": base64.b64encode(a.data).decode("ascii")}} for a in attachments
        ]
        parts.append({"text": user})
        body: dict = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_tokens},
        }
        if search:
            body["tools"] = [{"google_search": {}}]  # grounding and JSON mode are mutually exclusive
        else:
            body["generationConfig"]["responseMimeType"] = "application/json"
        resp = self.session.post(
            f"{base}/models/{model}:generateContent", headers={"x-goog-api-key": key}, json=body, timeout=(10, self.read_timeout)
        )
        if resp.status_code == 429:
            raise self._gemini_quota(resp)
        if resp.status_code in (500, 502, 503, 504):
            raise requests.ConnectionError(f"gemini {resp.status_code}")
        if resp.status_code != 200:
            raise RuntimeError(f"gemini {resp.status_code}: {_error_message(resp)}")
        payload = resp.json()
        candidates = payload.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"gemini returned no candidates ({payload.get('promptFeedback')})")
        text = "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []) if not p.get("thought"))
        usage = payload.get("usageMetadata", {})
        t_out = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
        sources = [
            {"title": c["web"].get("title", ""), "url": c["web"].get("uri", "")}
            for c in candidates[0].get("groundingMetadata", {}).get("groundingChunks", [])
            if c.get("web")
        ]
        return text, usage.get("promptTokenCount", 0), t_out, sources

    @staticmethod
    def _gemini_quota(resp) -> _Quota:
        retry_after, daily = None, False
        try:
            for detail in resp.json().get("error", {}).get("details", []):
                if detail.get("@type", "").endswith("RetryInfo"):
                    retry_after = float(str(detail.get("retryDelay", "0s")).rstrip("s") or 0)
                for violation in detail.get("violations", []):
                    if "PerDay" in violation.get("quotaId", "") or "per_day" in violation.get("quotaMetric", ""):
                        daily = True
        except (ValueError, AttributeError):
            pass
        return _Quota("gemini 429", retry_after, daily)

    def _openai_compatible(self, provider, model, system, user, max_tokens):
        base, key_env = OPENAI_COMPATIBLE[provider]
        headers = {"Authorization": f"Bearer {os.environ[key_env]}"} if key_env else {}
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        resp = self.session.post(f"{base}/chat/completions", headers=headers, json=body, timeout=(10, self.read_timeout))
        if resp.status_code == 429:
            raise _Quota(f"{provider} 429", float(resp.headers.get("retry-after", 20) or 20), False)
        if resp.status_code in (500, 502, 503, 504):
            raise requests.ConnectionError(f"{provider} {resp.status_code}")
        if resp.status_code != 200:
            raise RuntimeError(f"{provider} {resp.status_code}: {_error_message(resp)}")
        payload = resp.json()
        usage = payload.get("usage", {})
        return (
            payload["choices"][0]["message"]["content"],
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            None,
        )

    # -- reporting ---------------------------------------------------------------
    def usage(self) -> dict:
        """Totals for this process: calls, cache hits, tokens and list-price cost, by task and model."""
        live = [c for c in self.calls if not c.cached]
        by_task: dict[str, dict] = {}
        by_model: dict[str, dict] = {}
        for call in self.calls:
            task = by_task.setdefault(call.task.split("/")[0], {"calls": 0, "cached": 0, "tokens_in": 0, "tokens_out": 0, "list_cost_usd": 0.0})
            task["cached" if call.cached else "calls"] += 1
            if not call.cached:
                task["tokens_in"] += call.tokens_in
                task["tokens_out"] += call.tokens_out
                task["list_cost_usd"] = round(task["list_cost_usd"] + call.list_cost, 5)
                model = by_model.setdefault(call.model, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "list_cost_usd": 0.0})
                model["calls"] += 1
                model["tokens_in"] += call.tokens_in
                model["tokens_out"] += call.tokens_out
                model["list_cost_usd"] = round(model["list_cost_usd"] + call.list_cost, 5)
        return {
            "calls": len(live),
            "cache_hits": len(self.calls) - len(live),
            "tokens_in": sum(c.tokens_in for c in live),
            "tokens_out": sum(c.tokens_out for c in live),
            "list_cost_usd": round(sum(c.list_cost for c in live), 4),
            "by_task": by_task,
            "by_model": by_model,
        }


def _error_message(resp) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", ""))[:200]
    except ValueError:
        return resp.text[:200]
