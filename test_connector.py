"""
test_connector.py - proves the connector works end to end.

    python test_connector.py          # offline, against the built-in mock Freshdesk
    python test_connector.py --live   # against YOUR real Freshdesk (reads .env)

Covers: API-key auth (good + bad), list (with pagination), get, conversations,
search (friendly filters + raw), contacts, 404 handling, 429 rate-limit retry,
MCP over stdio (tool discovery + tool call), and the hosted HTTP endpoint
(bearer-token protection + tool call).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

from dotenv import load_dotenv

from freshdesk_client import FreshdeskClient, FreshdeskError

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = "--live" in sys.argv
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn):
    try:
        detail = fn() or ""
        RESULTS.append((name, True, str(detail)))
        print(f"  PASS  {name}  {detail}")
    except Exception as e:  # noqa: BLE001
        RESULTS.append((name, False, repr(e)))
        print(f"  FAIL  {name}  -> {e!r}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ----------------------------------------------------------- environment
if LIVE:
    load_dotenv(os.path.join(HERE, ".env"))
    ENV = {"FRESHDESK_DOMAIN": os.environ["FRESHDESK_DOMAIN"],
           "FRESHDESK_API_KEY": os.environ["FRESHDESK_API_KEY"]}
    print(f"\nLIVE mode -> {ENV['FRESHDESK_DOMAIN']}\n")
else:
    import mock_freshdesk
    _srv, base = mock_freshdesk.start()
    ENV = {"FRESHDESK_DOMAIN": "mock", "FRESHDESK_API_KEY": mock_freshdesk.VALID_KEY,
           "FRESHDESK_BASE_URL": base}
    print(f"\nMOCK mode -> {base}\n")

os.environ.update(ENV)
c = FreshdeskClient.from_env()

# ------------------------------------------------------- client-level tests
print("1) Authentication")
check("valid API key accepted", lambda: c.whoami()["name"])


def bad_key():
    bad = FreshdeskClient(domain=ENV["FRESHDESK_DOMAIN"], api_key="definitely-wrong",
                          base_url=ENV.get("FRESHDESK_BASE_URL"), max_retries=0)
    try:
        bad.whoami()
    except FreshdeskError as e:
        assert e.status_code == 401, e
        return "401 -> clear error message"
    raise AssertionError("bad key was accepted")


check("invalid API key rejected", bad_key)

print("\n2) List / Get primitives")
tickets: list[dict] = []


def t_list():
    global tickets
    tickets = c.list_tickets(limit=150 if not LIVE else 20,
                             updated_since="2020-01-01T00:00:00Z")
    if not LIVE:
        assert len(tickets) == 150, f"pagination broke: got {len(tickets)}"
    return f"{len(tickets)} tickets" + (" (2 pages followed)" if not LIVE else "")


check("list_tickets (+pagination)", t_list)
if not tickets:
    tickets = [{"id": 1}]
check("get_ticket with conversations",
      lambda: (lambda t: f"#{t['id']} '{t['subject']}' status={t['status']} convs={len(t['conversations'])}")(
          c.get_ticket(tickets[0]["id"])))
check("list_ticket_conversations",
      lambda: f"{len(c.list_ticket_conversations(tickets[0]['id']))} messages")


def t_404():
    try:
        c.get_ticket(987654321)
    except FreshdeskError as e:
        assert e.status_code == 404
        return "404 -> clear error message"
    raise AssertionError("expected 404")


check("unknown ticket -> 404 handled", t_404)

print("\n3) Search primitives")
check("search_tickets status=Open priority=Urgent",
      lambda: f"total={c.search_tickets('status:2 AND priority:4')['total']}")
RAW_Q = "tag:'refund' AND created_at:>'2026-01-01'"
check("search_tickets raw query with tag + date",
      lambda: "total=" + str(c.search_tickets(RAW_Q)["total"]))
if tickets and tickets[0].get("requester_id"):
    check("get_contact (ticket requester)",
          lambda: c.get_contact(tickets[0]["requester_id"])["name"])
if not LIVE:
    check("search_contacts by email", lambda: c.search_contacts(email="ben@example.com")[0]["name"])
    check("search_contacts by name prefix", lambda: len(c.search_contacts(name_prefix="ch")))

print("\n4) Rate-limit handling")
if not LIVE:
    def t_429():
        mock_freshdesk.STATE["force_429_next"] = 2
        start = time.time()
        c.whoami()
        waited = time.time() - start
        assert c.rate_limit.throttled_count >= 2 and waited >= 1.9
        return f"got 429 twice, honored Retry-After, succeeded after {waited:.1f}s"
    check("429 + Retry-After -> automatic retry", t_429)
check("rate-limit headers tracked",
      lambda: f"remaining={c.rate_limit.remaining}/{c.rate_limit.total} per minute")


# ------------------------------------------------------------ MCP tests
async def mcp_stdio_test():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=[os.path.join(HERE, "server.py")],
                                   env={**os.environ, **ENV}, cwd=HERE)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            res = await s.call_tool("search_tickets", {"status": "Open", "priority": "Urgent"})
            payload = json.loads(res.content[0].text)
            assert payload["ok"], payload
            return f"tools={tools}; search total={payload['data']['total']}"


async def mcp_http_test():
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    port, token = free_port(), "test-token-123"
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")], cwd=HERE,
                            env={**os.environ, **ENV, "MCP_TRANSPORT": "http", "PORT": str(port),
                                 "HOST": "127.0.0.1", "CONNECTOR_TOKEN": token},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                if httpx.get(f"{url}/health").status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.2)
        no_auth = httpx.post(f"{url}/mcp", json={}).status_code
        assert no_auth == 401, f"expected 401 without token, got {no_auth}"
        async with streamablehttp_client(f"{url}/mcp", headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool("check_connection", {})
                payload = json.loads(res.content[0].text)
                assert payload["ok"], payload
        return f"no token -> 401; with token -> check_connection ok ({payload['data']['name']})"
    finally:
        proc.terminate()


print("\n5) MCP server")
check("MCP over stdio: discover tools + call search_tickets", lambda: asyncio.run(mcp_stdio_test()))
check("MCP over HTTP: bearer auth + call check_connection", lambda: asyncio.run(mcp_http_test()))

# ---------------------------------------------------------------- summary
passed = sum(ok for _, ok, _ in RESULTS)
print(f"\n{'=' * 60}\n{passed}/{len(RESULTS)} checks passed\n{'=' * 60}")
sys.exit(0 if passed == len(RESULTS) else 1)
