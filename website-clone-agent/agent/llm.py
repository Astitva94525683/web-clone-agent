"""LLM client: any OpenAI-compatible chat API (DeepSeek V4 by default).

Cost-awareness features live here:
  * on-disk response cache (identical prompts are never paid for twice),
  * per-call token + USD accounting (incl. DeepSeek's prompt-cache hits),
  * model routing (``fast`` model for cheap steps, main model for codegen),
  * retries with backoff for rate limits / transient errors,
  * JSON-mode helper that repairs or re-asks on malformed JSON.

When no API key is configured the client runs in *mock* mode: every call
raises ``LLMUnavailable`` and each stage falls back to its deterministic path,
so the whole agent still works offline.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from .config import Settings, settings as default_settings


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """No API key / mock mode - callers must use their deterministic fallback."""


@dataclass
class LLMCall:
    stage: str
    model: str
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    cache_hit: bool = False
    ok: bool = True
    error: str = ""
    finish_reason: str = ""
    ts: float = 0.0


def _strip_fences(text: str) -> str:
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return m.group(1) if m else text


def parse_json(text: str) -> Any:
    """Parse JSON from a model reply, tolerating fences, prose and trailing commas."""
    raw = _strip_fences(text or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        chunk = raw[start:end + 1]
        chunk = re.sub(r",\s*([}\]])", r"\1", chunk)
        return json.loads(chunk)
    raise json.JSONDecodeError("no JSON object found", raw, 0)


class LLM:
    def __init__(self, cfg: Optional[Settings] = None):
        self.cfg = cfg or default_settings
        self.calls: list[LLMCall] = []
        self.lifetime = {"calls": 0, "tokens": 0, "cost_usd": 0.0}  # survives drain(); shown in the UI
        self._lock = threading.Lock()
        self._client = None
        self._fatal = ""  # set when the endpoint says the model does not exist
        self.cache_dir: Path = self.cfg.cache_dir / "llm"
        if self.cfg.cache_enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    @property
    def is_mock(self) -> bool:
        return self.cfg.use_mock

    @property
    def label(self) -> str:
        if self.is_mock:
            return "offline mock (no API key) - deterministic fallbacks"
        return f"{self.cfg.model} via {self.cfg.base_url}"

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.cfg.api_key, base_url=self.cfg.base_url,
                                  timeout=self.cfg.request_timeout, max_retries=0)
        return self._client

    # ------------------------------------------------------------------
    def _cost(self, prompt: int, cached: int, completion: int) -> float:
        c = self.cfg
        return ((prompt - cached) * c.price_input + cached * c.price_input_cached + completion * c.price_output) / 1e6

    def _record(self, call: LLMCall) -> None:
        call.ts = time.time()
        with self._lock:
            self.calls.append(call)
            if not call.cache_hit:
                self.lifetime["calls"] += 1
                self.lifetime["tokens"] += call.prompt_tokens + call.completion_tokens
                self.lifetime["cost_usd"] += call.cost_usd

    def _cache_key(self, payload: dict) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    # ------------------------------------------------------------------
    def chat(self, messages: list[dict], *, stage: str, fast: bool = False, json_mode: bool = False,
             max_tokens: int = 8000, temperature: float = 0.2) -> str:
        """One chat completion. Returns the assistant text."""
        if self.is_mock:
            raise LLMUnavailable("LLM disabled (no API key) - using deterministic fallback")
        if self._fatal:
            raise LLMError(self._fatal)
        model = self.cfg.model_fast if fast else self.cfg.model
        payload = {"model": model, "messages": messages, "json": json_mode, "max_tokens": max_tokens,
                   "temperature": temperature, "thinking": self.cfg.thinking}
        key = self._cache_key(payload)
        cache_file = self.cache_dir / f"{key}.json"
        if self.cfg.cache_enabled and cache_file.exists():
            try:
                hit = json.loads(cache_file.read_text(encoding="utf-8"))
                self._record(LLMCall(stage=stage, model=model, cache_hit=True,
                                     prompt_tokens=hit.get("prompt_tokens", 0),
                                     completion_tokens=hit.get("completion_tokens", 0)))
                return hit["content"]
            except (json.JSONDecodeError, KeyError):
                pass

        kwargs: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        extra: dict[str, Any] = {}
        if self.cfg.thinking in ("enabled", "disabled"):
            if self.cfg.is_deepseek:
                extra["thinking"] = {"type": self.cfg.thinking}
            elif self.cfg.is_nvidia:
                # NVIDIA NIM exposes the DeepSeek reasoning switch through the chat template
                extra["chat_template_kwargs"] = {"thinking": self.cfg.thinking == "enabled"}
        extra.update(self.cfg.extra_body)
        if not (self.cfg.is_deepseek and self.cfg.thinking == "enabled"):
            kwargs["temperature"] = temperature  # thinking mode rejects temperature
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if extra:
            kwargs["extra_body"] = extra

        # Reasoning models can spend the whole budget "thinking" and return no answer:
        # retry with a bigger budget, and as a last resort use the reasoning text itself.
        for _ in range(3):
            content, reasoning, usage, finish = self._with_retries(kwargs, stage, model)
            if content.strip() or finish != "length" or kwargs["max_tokens"] >= 32768:
                break
            kwargs["max_tokens"] = min(32768, kwargs["max_tokens"] * 2)
        if not content.strip() and reasoning.strip():
            content = reasoning
        if not content.strip():
            raise LLMError(f"The model returned an empty answer (finish_reason={finish or 'unknown'}). "
                           "If it is a reasoning model, set LLM_THINKING=disabled or use a non-reasoning model.")
        if self.cfg.cache_enabled and finish != "length":
            cache_file.write_text(json.dumps({"content": content, **usage}), encoding="utf-8")
        return content

    def _with_retries(self, kwargs: dict, stage: str, model: str) -> tuple[str, str, dict, str]:
        import openai

        delay, last_err = 2.0, None
        stripped_extras = False
        for attempt in range(4):
            t0 = time.time()
            try:
                resp = self._get_client().chat.completions.create(**kwargs)
                choice = resp.choices[0]
                content = choice.message.content or ""
                reasoning = (getattr(choice.message, "reasoning_content", None)
                             or getattr(choice.message, "reasoning", None) or "")
                u = resp.usage
                prompt = getattr(u, "prompt_tokens", 0) or 0
                completion = getattr(u, "completion_tokens", 0) or 0
                cached = getattr(u, "prompt_cache_hit_tokens", None)
                if cached is None:
                    details = getattr(u, "prompt_tokens_details", None)
                    cached = getattr(details, "cached_tokens", 0) if details else 0
                cached = cached or 0
                self._record(LLMCall(stage=stage, model=model, prompt_tokens=prompt, cached_tokens=cached,
                                     completion_tokens=completion, cost_usd=self._cost(prompt, cached, completion),
                                     latency_s=round(time.time() - t0, 2), finish_reason=choice.finish_reason or ""))
                return (content, str(reasoning), {"prompt_tokens": prompt, "completion_tokens": completion},
                        choice.finish_reason or "")
            except openai.AuthenticationError as e:
                self._record(LLMCall(stage=stage, model=model, ok=False, error="auth"))
                raise LLMError("The LLM API key was rejected. Check LLM_API_KEY in .env.") from e
            except openai.BadRequestError as e:
                # Some OpenAI-compatible providers reject optional params: retry once without them.
                if not stripped_extras and ("extra_body" in kwargs or "response_format" in kwargs):
                    kwargs.pop("extra_body", None)
                    kwargs.pop("response_format", None)
                    stripped_extras = True
                    continue
                self._record(LLMCall(stage=stage, model=model, ok=False, error=str(e)[:200]))
                raise LLMError(f"LLM rejected the request: {str(e)[:300]}") from e
            except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
                    openai.InternalServerError) as e:
                last_err = e
                self._record(LLMCall(stage=stage, model=model, ok=False, error=type(e).__name__,
                                     latency_s=round(time.time() - t0, 2)))
                time.sleep(delay)
                delay *= 2
            except openai.APIStatusError as e:
                self._record(LLMCall(stage=stage, model=model, ok=False, error=str(e)[:200]))
                if e.status_code in (404, 410):
                    # Wrong / retired model id: every further call would fail the same way.
                    self._fatal = (f"Model '{model}' is not available at {self.cfg.base_url} (HTTP {e.status_code}). "
                                   f"Run `python cli.py models` to list valid ids, set LLM_MODEL in .env, "
                                   f"and restart the app.")
                    raise LLMError(self._fatal) from e
                raise LLMError(f"LLM API error: {str(e)[:300]}") from e
        raise LLMError(f"LLM unavailable after retries: {type(last_err).__name__}: {str(last_err)[:200]}")

    def chat_json(self, messages: list[dict], *, stage: str, fast: bool = True, max_tokens: int = 8000) -> Any:
        """Chat in JSON mode; on malformed output, ask the model once to fix it."""
        text = self.chat(messages, stage=stage, fast=fast, json_mode=True, max_tokens=max_tokens)
        try:
            return parse_json(text)
        except json.JSONDecodeError as e:
            fix = messages + [
                {"role": "assistant", "content": text[:20000]},
                {"role": "user", "content": f"That was not valid JSON ({e.msg}). Reply with ONLY the corrected JSON object."},
            ]
            return parse_json(self.chat(fix, stage=stage + ":json-fix", fast=True, json_mode=True, max_tokens=max_tokens))

    def list_models(self, contains: str = "") -> list[str]:
        """Model ids offered by the configured endpoint (helps pick LLM_MODEL)."""
        ids = sorted(m.id for m in self._get_client().models.list())
        return [i for i in ids if contains.lower() in i.lower()]

    # ------------------------------------------------------------------
    def summary(self) -> dict:
        with self._lock:
            calls = list(self.calls)
        return {
            "calls": len(calls),
            "cache_hits": sum(1 for c in calls if c.cache_hit),
            "failed": sum(1 for c in calls if not c.ok),
            "prompt_tokens": sum(c.prompt_tokens for c in calls if not c.cache_hit),
            "cached_prompt_tokens": sum(c.cached_tokens for c in calls),
            "completion_tokens": sum(c.completion_tokens for c in calls if not c.cache_hit),
            "cost_usd": round(sum(c.cost_usd for c in calls), 5),
        }

    def drain(self) -> list[dict]:
        """Return and clear the recorded calls (the pipeline persists them per project)."""
        with self._lock:
            out = [asdict(c) for c in self.calls]
            self.calls.clear()
        return out
