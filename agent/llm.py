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


class LLMTooSlow(LLMError):
    """A call ran past its time limit; the caller keeps its non-AI result."""


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


def _collect_stream(stream, deadline: Optional[float] = None) -> tuple[str, str, Any, Optional[str]]:
    """Assemble a streamed chat completion: (content, reasoning, finish_reason, usage).
    Stops (raising LLMTooSlow) once the wall-clock deadline has passed."""
    content, reasoning, finish, usage = [], [], None, None
    for chunk in stream:
        if deadline is not None and time.time() > deadline:
            try:
                stream.close()
            except Exception:
                pass
            raise LLMTooSlow("time limit reached while the model was still answering")
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        for choice in chunk.choices or []:
            delta = choice.delta
            if delta is not None:
                content.append(getattr(delta, "content", None) or "")
                reasoning.append(getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None) or "")
            finish = choice.finish_reason or finish
    return "".join(content), "".join(reasoning), finish, usage


class LLM:
    def __init__(self, cfg: Optional[Settings] = None):
        self.cfg = cfg or default_settings
        self.calls: list[LLMCall] = []
        self.lifetime = {"calls": 0, "tokens": 0, "cost_usd": 0.0}  # survives drain(); shown in the UI
        self._lock = threading.Lock()
        self._client = None
        self._fatal = ""  # set when the model does not exist or the endpoint stops answering
        self._fatal_until: Optional[float] = None  # None = until restart; else a cool-down end time
        self.cache_dir: Path = self.cfg.cache_dir / "llm"
        if self.cfg.cache_enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def reset_pause(self) -> None:
        """Forget a timeout pause from an earlier run (a wrong model id stays blocked)."""
        if self._fatal and self._fatal_until is not None:
            self._fatal, self._fatal_until = "", None

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
             max_tokens: int = 8000, temperature: float = 0.2, max_seconds: Optional[float] = None) -> str:
        """One chat completion. Returns the assistant text.

        max_seconds caps the whole call (default LLM_MAX_CALL_SECONDS); past it LLMTooSlow is raised
        so slow endpoints can never stall the agent."""
        if self.is_mock:
            raise LLMUnavailable("LLM disabled (no API key) - using deterministic fallback")
        if self._fatal:
            if self._fatal_until is None or time.time() < self._fatal_until:
                raise LLMError(self._fatal)
            self._fatal, self._fatal_until = "", None  # cool-down over: try the endpoint again
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

        if max_seconds is not None and max_seconds <= 0:
            raise LLMTooSlow(f"{stage}: no time left in the AI time budget")
        # an explicit budget from the caller wins; otherwise the default per-call cap applies
        limit = max_seconds if max_seconds is not None else (self.cfg.max_call_seconds or None)
        deadline = time.time() + limit if limit else None

        # Streamed: LLM_TIMEOUT then limits the wait for each chunk, not for the whole answer,
        # so slow-but-working endpoints (free tiers writing long components) never time out.
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if self.cfg.stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
        extra: dict[str, Any] = {}
        if self.cfg.thinking in ("enabled", "disabled"):
            if self.cfg.is_deepseek:
                extra["thinking"] = {"type": self.cfg.thinking}
            elif self.cfg.is_nvidia:
                # NVIDIA NIM exposes the DeepSeek reasoning switch through the chat template
                # DeepSeek's chat template reads "thinking"; GLM / Qwen templates read "enable_thinking".
                on = self.cfg.thinking == "enabled"
                extra["chat_template_kwargs"] = {"thinking": on, "enable_thinking": on}
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
            content, reasoning, usage, finish = self._with_retries(kwargs, stage, model, deadline)
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

    def _with_retries(self, kwargs: dict, stage: str, model: str,
                      deadline: Optional[float] = None) -> tuple[str, str, dict, str]:
        import openai

        delay, last_err = 2.0, None
        stripped_extras = False
        for attempt in range(4):
            t0 = time.time()
            client = self._get_client()
            if deadline is not None:
                left = deadline - t0
                if left <= 1:
                    raise LLMTooSlow(f"{stage}: the AI did not finish within its time limit")
                client = client.with_options(timeout=min(self.cfg.request_timeout, left))
            try:
                resp = client.chat.completions.create(**kwargs)
                if kwargs.get("stream"):
                    content, reasoning, finish_reason, u = _collect_stream(resp, deadline)
                else:
                    choice = resp.choices[0]
                    content = choice.message.content or ""
                    reasoning = (getattr(choice.message, "reasoning_content", None)
                                 or getattr(choice.message, "reasoning", None) or "")
                    finish_reason, u = choice.finish_reason, resp.usage
                prompt = getattr(u, "prompt_tokens", 0) or 0
                completion = getattr(u, "completion_tokens", 0) or 0
                cached = getattr(u, "prompt_cache_hit_tokens", None)
                if cached is None:
                    details = getattr(u, "prompt_tokens_details", None)
                    cached = getattr(details, "cached_tokens", 0) if details else 0
                cached = cached or 0
                self._record(LLMCall(stage=stage, model=model, prompt_tokens=prompt, cached_tokens=cached,
                                     completion_tokens=completion, cost_usd=self._cost(prompt, cached, completion),
                                     latency_s=round(time.time() - t0, 2), finish_reason=finish_reason or ""))
                return (content, str(reasoning), {"prompt_tokens": prompt, "completion_tokens": completion},
                        finish_reason or "")
            except LLMTooSlow as e:
                self._record(LLMCall(stage=stage, model=model, ok=False, error="time limit",
                                     latency_s=round(time.time() - t0, 2)))
                raise LLMTooSlow(f"{stage}: the AI did not finish within its time limit "
                                 f"({round(time.time() - t0)}s)") from e
            except openai.AuthenticationError as e:
                self._record(LLMCall(stage=stage, model=model, ok=False, error="auth"))
                raise LLMError("The LLM API key was rejected. Check LLM_API_KEY in .env.") from e
            except openai.BadRequestError as e:
                # Some OpenAI-compatible providers reject optional params: retry once without them.
                if not stripped_extras and ("extra_body" in kwargs or "response_format" in kwargs
                                            or "stream_options" in kwargs):
                    kwargs.pop("extra_body", None)
                    kwargs.pop("response_format", None)
                    kwargs.pop("stream_options", None)
                    stripped_extras = True
                    continue
                self._record(LLMCall(stage=stage, model=model, ok=False, error=str(e)[:200]))
                raise LLMError(f"LLM rejected the request: {str(e)[:300]}") from e
            except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
                    openai.InternalServerError) as e:
                last_err = e
                self._record(LLMCall(stage=stage, model=model, ok=False, error=type(e).__name__,
                                     latency_s=round(time.time() - t0, 2)))
                # A timeout already cost LLM_TIMEOUT seconds: retry it only once.
                if isinstance(e, openai.APITimeoutError) and attempt >= 1:
                    break
                if deadline is not None and time.time() + delay >= deadline:
                    raise LLMTooSlow(f"{stage}: the AI did not finish within its time limit") from e
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
        if isinstance(last_err, (openai.APITimeoutError, openai.APIConnectionError)):
            # The endpoint is unreachable or too slow: every further call of this run would wait just
            # as long, so fail fast from now on and let each stage use its deterministic fallback.
            self._fatal = (f"The LLM endpoint {self.cfg.base_url} did not answer model '{model}' "
                           f"({type(last_err).__name__} after {self.cfg.request_timeout:g}s). The endpoint may "
                           f"be overloaded or the model id wrong - run `python cli.py ping`, raise LLM_TIMEOUT "
                           f"or pick a faster model. AI calls are paused for 5 minutes (sections use the "
                           f"fallback renderer); press 'Reload .env settings' to retry sooner.")
            self._fatal_until = time.time() + 300
            raise LLMError(self._fatal)
        raise LLMError(f"LLM unavailable after retries: {type(last_err).__name__}: {str(last_err)[:200]}")

    def chat_json(self, messages: list[dict], *, stage: str, fast: bool = True, max_tokens: int = 8000,
                  max_seconds: Optional[float] = None) -> Any:
        """Chat in JSON mode; on malformed output, ask the model once to fix it."""
        t0 = time.time()
        text = self.chat(messages, stage=stage, fast=fast, json_mode=True, max_tokens=max_tokens,
                         max_seconds=max_seconds)
        try:
            return parse_json(text)
        except json.JSONDecodeError as e:
            fix = messages + [
                {"role": "assistant", "content": text[:20000]},
                {"role": "user", "content": f"That was not valid JSON ({e.msg}). Reply with ONLY the corrected JSON object."},
            ]
            left = None if max_seconds is None else max_seconds - (time.time() - t0)
            return parse_json(self.chat(fix, stage=stage + ":json-fix", fast=True, json_mode=True,
                                        max_tokens=max_tokens, max_seconds=left))

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
