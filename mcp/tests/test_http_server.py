"""Integration tests for the streamable-HTTP transport wiring.

These exercise the real FastMCP app produced by server.build_http_app()
through a Starlette TestClient. No BLE device is touched: auth + the MCP
handshake + tools/list need no Bridge, and the tool-body tests below
monkeypatch server.bridge.send so the tool logic runs without a radio.
"""

import json

import pytest
from starlette.testclient import TestClient

import server
from ratelimit import MinIntervalLimiter

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
_H = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


@pytest.fixture(scope="module")
def client():
    # One app + one TestClient for the whole module: the FastMCP
    # streamable-HTTP session manager's lifespan can only run once per
    # `mcp` instance, and `mcp` is a module-level singleton. Two tokens
    # so we can assert token -> agent-label mapping on the device banner.
    app = server.build_http_app(
        {"tok": "claude-code", "cloud": "managed-agent"},
        tunnel_domain="abcd1234.tunnel.anthropic.com",
        extra_allowed_hosts=["testserver"],
    )
    with TestClient(app) as c:
        yield c


def _init_session(client, token):
    h = {**_H, "authorization": f"Bearer {token}"}
    init = client.post("/mcp", headers=h, json=_INIT)
    assert init.status_code == 200, init.text
    sid = init.headers["mcp-session-id"]
    h2 = {**h, "mcp-session-id": sid}
    client.post(
        "/mcp",
        headers=h2,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    return h2


def _sse_objects(text):
    out = []
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                out.append(json.loads(line[5:].strip()))
            except ValueError:
                pass
    return out


def test_missing_bearer_is_401(client):
    r = client.post("/mcp", headers=_H, json=_INIT)
    assert r.status_code == 401


def test_wrong_bearer_is_401(client):
    h = {**_H, "authorization": "Bearer WRONG"}
    r = client.post("/mcp", headers=h, json=_INIT)
    assert r.status_code == 401


def test_tunnel_host_header_is_not_421(client):
    # The Host a tunneled request actually arrives with. Must NOT be
    # rejected by DNS-rebinding protection (the bug the *.domain entry
    # silently caused). Bearer is valid, host is allowed -> 200.
    h = {
        **_H,
        "authorization": "Bearer cloud",
        "host": "cardputer.abcd1234.tunnel.anthropic.com",
    }
    r = client.post("/mcp", headers=h, json=_INIT)
    assert r.status_code != 421, "tunnel Host rejected by rebinding protection"
    assert r.status_code == 200, r.text


def test_initialize_and_tools_list_with_bearer(client):
    h = {**_H, "authorization": "Bearer tok"}
    init = client.post("/mcp", headers=h, json=_INIT)
    assert init.status_code == 200, init.text
    sid = init.headers.get("mcp-session-id")
    assert sid
    h2 = {**h, "mcp-session-id": sid}
    client.post(
        "/mcp",
        headers=h2,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    tl = client.post(
        "/mcp",
        headers=h2,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert tl.status_code == 200, tl.text
    names = set()
    for obj in _sse_objects(tl.text):
        for tool in obj.get("result", {}).get("tools", []):
            names.add(tool["name"])
    assert {"notify", "ask", "confirm", "show"} <= names, names


def test_notify_threads_agent_label_from_token(client, monkeypatch):
    # Capture what reaches the Bridge instead of touching BLE.
    captured = {}

    async def fake_send(cmd, payload, rpc_timeout_s=30.0, agent="mcp-client"):
        captured["cmd"] = cmd
        captured["agent"] = agent
        return {"ack": cmd, "ok": True}

    monkeypatch.setattr(server.bridge, "send", fake_send)

    h = _init_session(client, "cloud")  # token "cloud" -> label "managed-agent"
    r = client.post(
        "/mcp",
        headers=h,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "notify", "arguments": {"title": "hi"}},
        },
    )
    assert r.status_code == 200, r.text
    assert captured.get("cmd") == "notify"
    assert captured.get("agent") == "managed-agent", captured


def test_notify_returns_dnd_when_device_suppresses(client, monkeypatch):
    async def fake_send(cmd, payload, rpc_timeout_s=30.0, agent="mcp-client"):
        return {"ack": cmd, "ok": False, "dnd": True}

    monkeypatch.setattr(server.bridge, "send", fake_send)
    h = _init_session(client, "tok")
    r = client.post(
        "/mcp",
        headers=h,
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "notify", "arguments": {"title": "hi"}},
        },
    )
    assert r.status_code == 200, r.text
    texts = []
    for obj in _sse_objects(r.text):
        for c in obj.get("result", {}).get("content", []):
            if c.get("type") == "text":
                texts.append(c["text"])
    assert "dnd" in texts, texts


def _call_texts(client, headers, name, arguments, call_id):
    """Call one tool and return the list of text-content strings it produced."""
    r = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert r.status_code == 200, r.text
    texts = []
    for obj in _sse_objects(r.text):
        for c in obj.get("result", {}).get("content", []):
            if c.get("type") == "text":
                texts.append(c["text"])
    return texts


def test_notify_rate_limited_for_same_agent(client, monkeypatch):
    # Fresh limiter so this test doesn't depend on (or pollute) the
    # module-level singleton. Two calls in one test run land well inside the
    # 60s window on the real monotonic clock.
    monkeypatch.setattr(server, "_notify_limiter", MinIntervalLimiter(60))

    reached = []

    async def fake_send(cmd, payload, rpc_timeout_s=30.0, agent="mcp-client"):
        reached.append((agent, payload.get("urgency")))
        return {"ack": cmd, "ok": True}

    monkeypatch.setattr(server.bridge, "send", fake_send)
    h = _init_session(client, "tok")  # token tok -> claude-code

    assert "shown" in _call_texts(client, h, "notify", {"title": "one"}, 10)
    assert "rate-limited" in _call_texts(client, h, "notify", {"title": "two"}, 11)
    # crit always bypasses the floor, even while the agent is throttled.
    crit = _call_texts(
        client, h, "notify", {"title": "fire", "urgency": "crit"}, 12
    )
    assert "shown" in crit, crit

    # Only the two allowed notifies reached the bridge; the rate-limited one
    # never hit the radio.
    assert reached == [("claude-code", "info"), ("claude-code", "crit")], reached


def test_notify_rate_limit_independent_per_agent(client, monkeypatch):
    monkeypatch.setattr(server, "_notify_limiter", MinIntervalLimiter(60))

    async def fake_send(cmd, payload, rpc_timeout_s=30.0, agent="mcp-client"):
        return {"ack": cmd, "ok": True}

    monkeypatch.setattr(server.bridge, "send", fake_send)

    h_local = _init_session(client, "tok")  # claude-code
    h_cloud = _init_session(client, "cloud")  # managed-agent

    assert "shown" in _call_texts(client, h_local, "notify", {"title": "a"}, 20)
    # Different agent -> own bucket -> still allowed.
    assert "shown" in _call_texts(client, h_cloud, "notify", {"title": "b"}, 21)
    # Same agent again -> throttled.
    assert "rate-limited" in _call_texts(client, h_local, "notify", {"title": "c"}, 22)


def test_show_sends_text_and_channel(client, monkeypatch):
    captured = {}

    async def fake_send(cmd, payload, rpc_timeout_s=30.0, agent="mcp-client"):
        captured["cmd"] = cmd
        captured["payload"] = payload
        captured["agent"] = agent
        return {"ack": cmd, "ok": True}

    monkeypatch.setattr(server.bridge, "send", fake_send)
    h = _init_session(client, "cloud")  # managed-agent
    texts = _call_texts(
        client, h, "show", {"text": "running pytest", "channel": "ci"}, 30
    )
    assert "shown" in texts, texts
    assert captured["cmd"] == "show"
    assert captured["payload"] == {"text": "running pytest", "channel": "ci"}
    assert captured["agent"] == "managed-agent"


def test_show_defaults_channel_to_agent_label(client, monkeypatch):
    captured = {}

    async def fake_send(cmd, payload, rpc_timeout_s=30.0, agent="mcp-client"):
        captured["payload"] = payload
        return {"ack": cmd, "ok": True}

    monkeypatch.setattr(server.bridge, "send", fake_send)
    h = _init_session(client, "tok")  # claude-code
    _call_texts(client, h, "show", {"text": "idle ok"}, 31)
    # No channel given -> falls back to the token-derived agent label.
    assert captured["payload"]["channel"] == "claude-code"


def test_show_truncates_long_text(client, monkeypatch):
    captured = {}

    async def fake_send(cmd, payload, rpc_timeout_s=30.0, agent="mcp-client"):
        captured["payload"] = payload
        return {"ack": cmd, "ok": True}

    monkeypatch.setattr(server.bridge, "send", fake_send)
    h = _init_session(client, "tok")
    _call_texts(client, h, "show", {"text": "x" * 200}, 32)
    assert len(captured["payload"]["text"]) == 48


def test_show_unavailable_when_device_off(client, monkeypatch):
    async def fake_send(cmd, payload, rpc_timeout_s=30.0, agent="mcp-client"):
        return {"ack": cmd, "ok": False, "err": "unavailable: device not found"}

    monkeypatch.setattr(server.bridge, "send", fake_send)
    h = _init_session(client, "tok")
    texts = _call_texts(client, h, "show", {"text": "hi"}, 33)
    assert any(t.startswith("unavailable") for t in texts), texts


def test_confirm_includes_details_when_provided(client, monkeypatch):
    captured = {}

    async def fake_send(cmd, payload, rpc_timeout_s=30.0, agent="mcp-client"):
        captured["payload"] = payload
        return {"ack": cmd, "ok": True, "confirmed": True, "hold_ms": 3100}

    monkeypatch.setattr(server.bridge, "send", fake_send)
    h = _init_session(client, "cloud")  # managed-agent
    texts = _call_texts(
        client,
        h,
        "confirm",
        {"title": "DROP customers", "details": "DROP TABLE customers; -- prod"},
        40,
    )
    assert any(t.startswith("confirmed") for t in texts), texts
    assert captured["payload"]["details"] == "DROP TABLE customers; -- prod"
    assert captured["payload"]["title"] == "DROP customers"
    assert captured["payload"]["danger"] is True


def test_confirm_omits_details_when_empty(client, monkeypatch):
    # Back-compat: with no details, the field must not appear in the payload
    # (old firmware sees exactly today's confirm line).
    captured = {}

    async def fake_send(cmd, payload, rpc_timeout_s=30.0, agent="mcp-client"):
        captured["payload"] = payload
        return {"ack": cmd, "ok": True, "confirmed": True, "hold_ms": 3000}

    monkeypatch.setattr(server.bridge, "send", fake_send)
    h = _init_session(client, "tok")
    _call_texts(client, h, "confirm", {"title": "deploy prod"}, 41)
    assert "details" not in captured["payload"]


def test_confirm_truncates_long_details(client, monkeypatch):
    captured = {}

    async def fake_send(cmd, payload, rpc_timeout_s=30.0, agent="mcp-client"):
        captured["payload"] = payload
        return {"ack": cmd, "ok": True, "confirmed": True, "hold_ms": 3000}

    monkeypatch.setattr(server.bridge, "send", fake_send)
    h = _init_session(client, "tok")
    _call_texts(
        client, h, "confirm", {"title": "big", "details": "x" * 5000}, 42
    )
    assert len(captured["payload"]["details"]) == server._CONFIRM_DETAILS_MAX
