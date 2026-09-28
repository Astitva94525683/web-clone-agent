"""Tests the real LLM client against a local fake OpenAI-compatible server:
request shape (DeepSeek thinking switch, JSON mode, images), usage/cost parsing
incl. prompt-cache hits, retries, auth errors, param stripping and the disk cache.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import agent.llm as llm_mod
from agent.config import Settings
from agent.llm import LLM, LLMError


class FakeAPI:
    def __init__(self):
        self.requests: list[dict] = []
        self.script: list[tuple[int, str]] = []  # (status, content) consumed per request
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # /v1/models
                data = json.dumps({"object": "list", "data": [
                    {"id": "deepseek-ai/deepseek-v4-pro", "object": "model", "created": 0, "owned_by": "x"},
                    {"id": "meta/llama-x", "object": "model", "created": 0, "owned_by": "x"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                api.requests.append(body)
                item = api.script.pop(0) if api.script else (200, "ok")
                status, content = item[0], item[1]
                finish = item[2] if len(item) > 2 else "stop"
                reasoning = item[3] if len(item) > 3 else None
                if status != 200:
                    payload = {"error": {"message": f"status {status}", "type": "x"}}
                else:
                    payload = {
                        "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                        "choices": [{"index": 0, "finish_reason": finish,
                                     "message": {"role": "assistant", "content": content,
                                                 **({"reasoning_content": reasoning} if reasoning else {})}}],
                        "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
                                  "prompt_cache_hit_tokens": 600, "prompt_cache_miss_tokens": 400},
                    }
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"


class DeepSeekLikeSettings(Settings):
    @property
    def is_deepseek(self) -> bool:  # the fake server stands in for api.deepseek.com
        return True


@pytest.fixture()
def api(monkeypatch):
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    fake = FakeAPI()
    yield fake
    fake.server.shutdown()


def make_llm(api, tmp_path, **kw) -> LLM:
    cfg = DeepSeekLikeSettings(api_key="sk-test", base_url=api.url, model="deepseek-flash", provider="openai",
                               thinking="disabled", price_input=1.0, price_input_cached=0.1, price_output=2.0,
                               workspace=tmp_path, **kw)
    return LLM(cfg)


def test_request_shape_and_cost(api, tmp_path):
    llm = make_llm(api, tmp_path)
    assert not llm.is_mock
    out = llm.chat([{"role": "user", "content": "hi"}], stage="t", json_mode=True)
    assert out == "ok"
    req = api.requests[0]
    assert req["model"] == "deepseek-flash"
    assert req["thinking"] == {"type": "disabled"}          # sent via extra_body
    assert req["response_format"] == {"type": "json_object"}
    assert "temperature" in req
    call = llm.calls[0]
    assert (call.prompt_tokens, call.cached_tokens, call.completion_tokens) == (1000, 600, 500)
    # (400 * 1.0 + 600 * 0.1 + 500 * 2.0) / 1e6
    assert abs(call.cost_usd - 0.00146) < 1e-9


def test_disk_cache_avoids_second_request(api, tmp_path):
    llm = make_llm(api, tmp_path)
    msgs = [{"role": "user", "content": "same prompt"}]
    llm.chat(msgs, stage="a")
    llm.chat(msgs, stage="b")
    assert len(api.requests) == 1
    assert llm.calls[1].cache_hit and llm.calls[1].cost_usd == 0


def test_retries_rate_limit_and_server_errors(api, tmp_path):
    api.script = [(429, ""), (500, ""), (200, "recovered")]
    llm = make_llm(api, tmp_path, cache_enabled=False)
    assert llm.chat([{"role": "user", "content": "x"}], stage="t") == "recovered"
    assert len(api.requests) == 3
    assert [c.ok for c in llm.calls] == [False, False, True]


def test_auth_error_is_clear(api, tmp_path):
    api.script = [(401, "")]
    llm = make_llm(api, tmp_path, cache_enabled=False)
    with pytest.raises(LLMError, match="API key"):
        llm.chat([{"role": "user", "content": "x"}], stage="t")


def test_bad_request_strips_optional_params(api, tmp_path):
    api.script = [(400, ""), (200, "fine")]
    llm = make_llm(api, tmp_path, cache_enabled=False)
    assert llm.chat([{"role": "user", "content": "x"}], stage="t", json_mode=True) == "fine"
    assert "response_format" in api.requests[0] and "response_format" not in api.requests[1]
    assert "thinking" not in api.requests[1]


def test_json_mode_reasks_on_invalid_json(api, tmp_path):
    api.script = [(200, "not json at all"), (200, '{"fixed": true}')]
    llm = make_llm(api, tmp_path, cache_enabled=False)
    assert llm.chat_json([{"role": "user", "content": "x"}], stage="t") == {"fixed": True}
    assert "not valid JSON" in api.requests[1]["messages"][-1]["content"]


def test_image_content_is_forwarded(api, tmp_path):
    llm = make_llm(api, tmp_path, cache_enabled=False)
    content = [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}}]
    llm.chat([{"role": "user", "content": content}], stage="t")
    assert api.requests[0]["messages"][0]["content"][1]["type"] == "image_url"


def test_retired_model_fails_fast_with_clear_message(api, tmp_path):
    api.script = [(410, "")]
    llm = make_llm(api, tmp_path, cache_enabled=False)
    with pytest.raises(LLMError, match="not available"):
        llm.chat([{"role": "user", "content": "x"}], stage="a")
    with pytest.raises(LLMError, match="cli.py models"):
        llm.chat([{"role": "user", "content": "y"}], stage="b")
    assert len(api.requests) == 1  # second call never hit the network


def test_list_models(api, tmp_path):
    assert make_llm(api, tmp_path).list_models("deepseek") == ["deepseek-ai/deepseek-v4-pro"]


def test_reasoning_model_out_of_budget_is_retried_with_more_tokens(api, tmp_path):
    api.script = [(200, "", "length", "thinking..."), (200, '{"ok": 1}', "stop")]
    llm = make_llm(api, tmp_path, cache_enabled=False)
    assert llm.chat_json([{"role": "user", "content": "x"}], stage="t", max_tokens=1000) == {"ok": 1}
    assert api.requests[1]["max_tokens"] == 2000


def test_empty_content_falls_back_to_reasoning_text(api, tmp_path):
    api.script = [(200, "", "stop", 'Plan: {"operations": []}')]
    llm = make_llm(api, tmp_path, cache_enabled=False)
    assert llm.chat_json([{"role": "user", "content": "x"}], stage="t") == {"operations": []}


class NvidiaLikeSettings(Settings):
    @property
    def is_nvidia(self) -> bool:
        return True


def test_nvidia_thinking_switch(api, tmp_path):
    cfg = NvidiaLikeSettings(api_key="k", base_url=api.url, model="m", provider="openai", thinking="disabled",
                             workspace=tmp_path, cache_enabled=False)
    LLM(cfg).chat([{"role": "user", "content": "x"}], stage="t")
    assert api.requests[0]["chat_template_kwargs"] == {"thinking": False}
