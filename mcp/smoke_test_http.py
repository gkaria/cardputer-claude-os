"""Smoke-test the cardputer-mcp HTTP daemon end-to-end, with auth.

This is the HTTP/tunnel-transport counterpart to smoke_test.py (which
drives the BLE Bridge directly). It speaks the streamable-http MCP
protocol the way a cloud agent does — bearer token, initialize handshake,
tools/call — so you can validate the daemon + auth + transport layer
locally before wiring up a real tunnel.

Prereqs:
  - The daemon running (CARDPUTER_HTTP=1), e.g. via
    mac/install_cardputer_bridge.sh, or manually:
      CARDPUTER_HTTP=1 CARDPUTER_TOKENS=smoke=claude-code \\
        .venv/bin/python server.py
  - For the notify step to show anything, the Cardputer powered on with
    the cardputer_mcp app running and in BLE range.

Run:
  cd mcp
  CARDPUTER_SMOKE_TOKEN=smoke .venv/bin/python smoke_test_http.py
  # optional: CARDPUTER_SMOKE_URL=http://127.0.0.1:9000/mcp
"""

import json
import os
import sys

import httpx

URL = os.environ.get("CARDPUTER_SMOKE_URL", "http://127.0.0.1:9000/mcp")
TOKEN = os.environ.get("CARDPUTER_SMOKE_TOKEN", "smoke")
_ACCEPT = "application/json, text/event-stream"


def _sse_result(text):
    """Pull the JSON-RPC result object out of an SSE response body."""
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                obj = json.loads(line[5:].strip())
            except ValueError:
                continue
            if "result" in obj or "error" in obj:
                return obj
    return None


def main() -> int:
    # 1) Auth must be enforced.
    print(f"→ {URL}")
    print("→ checking auth rejects a missing bearer...")
    r = httpx.post(
        URL,
        headers={"accept": _ACCEPT, "content-type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        timeout=10,
    )
    if r.status_code != 401:
        print(f"  FAIL: expected 401 without bearer, got {r.status_code}")
        return 1
    print("  OK — 401 without a token.\n")

    h = {
        "authorization": f"Bearer {TOKEN}",
        "accept": _ACCEPT,
        "content-type": "application/json",
    }

    # 2) Initialize a session.
    print("→ initialize...")
    init = httpx.post(
        URL,
        headers=h,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke-http", "version": "1"},
            },
        },
        timeout=10,
    )
    if init.status_code != 200:
        print(f"  FAIL: initialize {init.status_code}: {init.text[:200]}")
        print("  (Is the daemon running with CARDPUTER_HTTP=1 and your token?)")
        return 1
    sid = init.headers.get("mcp-session-id")
    h["mcp-session-id"] = sid
    httpx.post(
        URL,
        headers=h,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout=10,
    )
    print(f"  OK — session {sid}.\n")

    # 3) notify (drives the device if it's connected).
    print("→ notify: 'HTTP smoke test' (info)")
    r = httpx.post(
        URL,
        headers=h,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "notify",
                "arguments": {
                    "title": "HTTP smoke test",
                    "body": "from smoke_test_http.py",
                    "urgency": "info",
                },
            },
        },
        timeout=20,
    )
    obj = _sse_result(r.text)
    print(f"  ← {json.dumps(obj)[:300] if obj else r.text[:300]}")
    if not obj or "result" not in obj:
        print("  FAIL: no tool result")
        return 1
    texts = [c.get("text") for c in obj["result"].get("content", [])]
    print(f"  tool returned: {texts}")
    if "shown" in texts:
        print("  OK — banner shown on the device.")
    elif any(t and t.startswith("unavailable") for t in texts):
        print("  Device unavailable (expected if it's off) — transport works.")
    elif "dnd" in texts:
        print("  Device in DND — transport works; banner suppressed.")
    else:
        print("  Transport works; unexpected device result (see above).")
    print()
    print("Done. notify went cloud-shaped HTTP -> bearer-auth -> daemon -> BLE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
