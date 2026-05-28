"""Unit tests for server._agent_label — the token->banner-label resolver.

Pure: builds a fake MCP Context, no HTTP server or BLE involved.
"""

import server


class _Headers(dict):
    # Real starlette Headers are case-insensitive; our tools only ever
    # read the lowercase key, so a plain dict is a faithful enough stub.
    pass


class _Req:
    def __init__(self, headers):
        self.headers = _Headers(headers)


class _ReqCtx:
    def __init__(self, request):
        self.request = request


class _Ctx:
    def __init__(self, request):
        self.request_context = _ReqCtx(request)


def test_agent_label_maps_token(monkeypatch):
    monkeypatch.setattr(server, "_TOKEN_MAP", {"tok": "managed-agent"})
    ctx = _Ctx(_Req({"authorization": "Bearer tok"}))
    assert server._agent_label(ctx) == "managed-agent"


def test_agent_label_no_ctx_is_local():
    assert server._agent_label(None) == "local"


def test_agent_label_no_request_is_local(monkeypatch):
    monkeypatch.setattr(server, "_TOKEN_MAP", {"tok": "managed-agent"})
    ctx = _Ctx(None)
    assert server._agent_label(ctx) == "local"


def test_agent_label_unknown_token_defaults(monkeypatch):
    # Shouldn't happen post-middleware, but be defensive rather than crash.
    monkeypatch.setattr(server, "_TOKEN_MAP", {"tok": "managed-agent"})
    ctx = _Ctx(_Req({"authorization": "Bearer other"}))
    assert server._agent_label(ctx) == "agent"
