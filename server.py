"""
server.py - Freshdesk MCP connector (read-only)

Run locally for desktop agents (stdio):
    python server.py

Run as a hosted HTTP MCP server (for Agent Studio / any remote MCP client):
    MCP_TRANSPORT=http CONNECTOR_TOKEN=some-long-secret python server.py
    -> endpoint: http://<host>:<PORT>/mcp   (send header: Authorization: Bearer <CONNECTOR_TOKEN>)
"""
from __future__ import annotations

import logging
import os
from typing import Literal, Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from freshdesk_client import FreshdeskClient, FreshdeskError, build_ticket_query

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = FastMCP(
    name="freshdesk",
    instructions=(
        "Read-only access to a Freshdesk helpdesk. Use search_tickets for filtered lookups "
        "(status, priority, tag, dates), list_tickets for recent activity, and get_ticket "
        "for full details including the conversation thread. You cannot create, update, "
        "reply to or delete anything."
    ),
    host=os.getenv("HOST", "0.0.0.0"),
    port=int(os.getenv("PORT", "8000")),
    stateless_http=True,
    log_level="WARNING",
    # We protect the HTTP endpoint with a bearer token below, so allow any Host header
    # (cloud hosts like Render put their own hostname in front of the app).
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_client: FreshdeskClient | None = None


def client() -> FreshdeskClient:
    global _client
    if _client is None:
        _client = FreshdeskClient.from_env()
    return _client


def _safe(fn, *args, **kwargs) -> dict:
    """Run a client call and turn failures into a readable error object for the agent."""
    try:
        return {"ok": True, "data": fn(*args, **kwargs)}
    except FreshdeskError as e:
        return {"ok": False, "error": str(e), "status_code": e.status_code}


# ------------------------------------------------------------------ tools
@mcp.tool()
def check_connection() -> dict:
    """Verify the Freshdesk API key works. Returns the agent who owns the key and current rate-limit budget."""
    res = _safe(client().whoami)
    rl = client().rate_limit
    res["rate_limit"] = {"per_minute_total": rl.total, "remaining": rl.remaining,
                         "times_throttled": rl.throttled_count}
    return res


@mcp.tool()
def list_tickets(
    limit: int = 30,
    updated_since: Optional[str] = None,
    order_by: Literal["created_at", "updated_at", "due_by", "status"] = "updated_at",
    order_type: Literal["asc", "desc"] = "desc",
    predefined_filter: Optional[Literal["new_and_my_open", "watching", "spam", "deleted"]] = None,
    requester_email: Optional[str] = None,
) -> dict:
    """List recent tickets (newest first by default).

    Args:
        limit: max tickets to return (1-300).
        updated_since: ISO date like '2026-09-01T00:00:00Z'. Without it Freshdesk only returns the last 30 days.
        order_by / order_type: sort order.
        predefined_filter: optional Freshdesk built-in view.
        requester_email: only tickets raised by this customer email.
    """
    limit = max(1, min(300, limit))
    return _safe(client().list_tickets, limit=limit, updated_since=updated_since,
                 order_by=order_by, order_type=order_type,
                 predefined_filter=predefined_filter, requester_email=requester_email)


@mcp.tool()
def get_ticket(ticket_id: int, include_conversations: bool = True) -> dict:
    """Get one ticket by ID: full description, requester, and (optionally) the reply/note thread."""
    return _safe(client().get_ticket, ticket_id, include_conversations)


@mcp.tool()
def list_ticket_conversations(ticket_id: int, limit: int = 50) -> dict:
    """List all replies and notes on a ticket (use when a thread is long)."""
    return _safe(client().list_ticket_conversations, ticket_id, max(1, min(500, limit)))


@mcp.tool()
def search_tickets(
    status: Optional[Literal["Open", "Pending", "Resolved", "Closed"]] = None,
    priority: Optional[Literal["Low", "Medium", "High", "Urgent"]] = None,
    tag: Optional[str] = None,
    created_after: Optional[str] = None,
    created_before: Optional[str] = None,
    agent_id: Optional[int] = None,
    group_id: Optional[int] = None,
    raw_query: Optional[str] = None,
    page: int = 1,
) -> dict:
    """Search tickets by filters. Combine any of: status, priority, tag, created_after/created_before
    (YYYY-MM-DD), agent_id, group_id. Advanced: pass raw_query in Freshdesk syntax, e.g.
    "priority:4 AND status:2". Returns 30 results per page, up to page 10.
    Note: Freshdesk search does NOT support free-text keyword search of subject/body.
    """
    try:
        query = raw_query or build_ticket_query(status, priority, tag, created_after,
                                                created_before, agent_id, group_id)
    except FreshdeskError as e:
        return {"ok": False, "error": str(e)}
    res = _safe(client().search_tickets, query, page)
    res["query_used"] = query
    return res


@mcp.tool()
def get_contact(contact_id: int) -> dict:
    """Get a customer (contact) by ID - e.g. the requester_id on a ticket."""
    return _safe(client().get_contact, contact_id)


@mcp.tool()
def search_contacts(email: Optional[str] = None, phone: Optional[str] = None,
                    name_prefix: Optional[str] = None) -> dict:
    """Find customers by exact email, exact phone, or the start of their name."""
    return _safe(client().search_contacts, email=email, phone=phone, name_prefix=name_prefix)


# ---------------------------------------------------------- HTTP extras
@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def build_http_app():
    """Streamable-HTTP app protected by a static bearer token."""
    app = mcp.streamable_http_app()
    token = os.getenv("CONNECTOR_TOKEN", "")
    if not token:
        raise SystemExit("Set CONNECTOR_TOKEN before running in HTTP mode (it protects your endpoint).")

    class BearerAuth:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope["path"] != "/health":
                headers = dict(scope.get("headers") or [])
                if headers.get(b"authorization", b"").decode() != f"Bearer {token}":
                    resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                    return await resp(scope, receive, send)
            return await self.inner(scope, receive, send)

    return BearerAuth(app)


if __name__ == "__main__":
    if os.getenv("MCP_TRANSPORT", "stdio").lower() == "http":
        import uvicorn
        uvicorn.run(build_http_app(), host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))
    else:
        mcp.run()  # stdio
