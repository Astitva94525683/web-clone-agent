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
                if status == 200 and body.get("stream"):
                    # Server-sent events, like the real API: content split in two chunks + a usage chunk
                    msg = payload["choices"][0]["message"]
                    half = len(content) // 2
                    deltas = [{"role": "assistant", "content": content[:half]}, {"content": content[half:]}]
                    if msg.get("reasoning_content"):
                        deltas.append({"reasoning_content": msg["reasoning_content"]})
                    events = [{"id": "x", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                               "choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
                    events[-1]["choices"][0]["finish_reason"] = finish
                    events.append({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                                   "choices": [], "usage": payload["usage"]})
                    data = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
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
    assert api.requests[0]["chat_template_kwargs"] == {"thinking": False, "enable_thinking": False}


def test_timeouts_fail_fast_for_the_rest_of_the_run(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    silent = socket.socket()  # accepts connections but never answers
    silent.bind(("127.0.0.1", 0))
    silent.listen(16)
    try:
        cfg = DeepSeekLikeSettings(api_key="sk-test", base_url=f"http://127.0.0.1:{silent.getsockname()[1]}/v1",
                                   model="m", provider="openai", request_timeout=0.3, workspace=tmp_path,
                                   cache_enabled=False)
        llm = LLM(cfg)
        with pytest.raises(LLMError, match="did not answer"):
            llm.chat([{"role": "user", "content": "x"}], stage="a")
        assert len(llm.calls) == 2  # one retry only, not four
        with pytest.raises(LLMError, match="did not answer"):
            llm.chat([{"role": "user", "content": "y"}], stage="b")
        assert len(llm.calls) == 2  # later calls fail immediately
    finally:
        silent.close()


def test_answers_are_streamed_and_non_streaming_still_works(api, tmp_path):
    llm = make_llm(api, tmp_path, cache_enabled=False)
    assert llm.chat([{"role": "user", "content": "x"}], stage="t") == "ok"
    assert api.requests[-1]["stream"] is True
    assert llm.calls[-1].prompt_tokens == 1000  # usage read from the final stream chunk
    plain = make_llm(api, tmp_path, cache_enabled=False, stream=False)
    assert plain.chat([{"role": "user", "content": "y"}], stage="t") == "ok"
    assert "stream" not in api.requests[-1]


def test_timeout_pause_expires(tmp_path, monkeypatch):
    llm = LLM(DeepSeekLikeSettings(api_key="k", base_url="http://127.0.0.1:9/v1", provider="openai",
                                   workspace=tmp_path))
    llm._fatal, llm._fatal_until = "paused", llm_mod.time.time() - 1
    monkeypatch.setattr(llm, "_with_retries", lambda *a, **k: ("back", "", {}, "stop"))
    assert llm.chat([{"role": "user", "content": "z"}], stage="t") == "back"


def test_slow_stream_is_cut_off_at_the_time_limit(tmp_path):
    """A model that keeps streaming slowly must not stall the agent: the call stops at max_seconds,
    and (unlike an unreachable endpoint) later calls are not paused."""
    import socket
    import time as _time

    from agent.llm import LLMTooSlow

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
            try:
                for _ in range(100):  # 0.2s per chunk, 20s in total
                    chunk = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
                             "choices": [{"index": 0, "delta": {"content": "a"}, "finish_reason": None}]}
                    conn.sendall(f"data: {json.dumps(chunk)}\n\n".encode())
                    _time.sleep(0.2)
            except OSError:
                pass
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        llm = LLM(DeepSeekLikeSettings(api_key="k", base_url=f"http://127.0.0.1:{srv.getsockname()[1]}/v1",
                                       model="m", provider="openai", workspace=tmp_path, cache_enabled=False))
        t0 = _time.time()
        with pytest.raises(LLMTooSlow):
            llm.chat([{"role": "user", "content": "x"}], stage="slow", max_seconds=1.5)
        assert _time.time() - t0 < 5
        assert not llm._fatal  # a slow answer does not pause the endpoint
        with pytest.raises(LLMTooSlow):  # an empty budget fails at once
            llm.chat([{"role": "user", "content": "y"}], stage="none", max_seconds=0)
    finally:
        stop.set()
        srv.close()
