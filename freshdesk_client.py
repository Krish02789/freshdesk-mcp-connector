"""
freshdesk_client.py
-------------------
A small, read-only client for the Freshdesk REST API (v2).

What it handles for you:
  * Authentication  - Freshdesk API key sent as HTTP Basic auth ("<key>:X")
  * Rate limits     - reads X-Ratelimit-* headers, waits on HTTP 429 using
                      Retry-After, and retries 5xx/network errors with backoff
  * Pagination      - follows page/per_page and the `Link: rel="next"` header
  * Clean errors    - turns HTTP failures into FreshdeskError with a clear message

Docs: https://developers.freshdesk.com/api/
"""
from __future__ import annotations

import os
import random
import ssl
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


def _ssl_verify():
    """Trust the operating system's certificate store (works behind corporate
    SSL-inspection proxies). Optional overrides:
      FRESHDESK_CA_BUNDLE=/path/to/ca.pem   use a specific CA file
    """
    bundle = os.getenv("FRESHDESK_CA_BUNDLE")
    if bundle:
        return bundle
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return True

# Freshdesk numeric codes -> human words (makes agent answers readable)
STATUS = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
PRIORITY = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}
SOURCE = {1: "Email", 2: "Portal", 3: "Phone", 7: "Chat", 9: "Feedback Widget", 10: "Outbound Email"}
STATUS_BY_NAME = {v.lower(): k for k, v in STATUS.items()}
PRIORITY_BY_NAME = {v.lower(): k for k, v in PRIORITY.items()}


class FreshdeskError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class RateLimitState:
    total: int | None = None
    remaining: int | None = None
    last_retry_after: float | None = None
    throttled_count: int = 0


@dataclass
class FreshdeskClient:
    domain: str                      # e.g. "acme" or "acme.freshdesk.com"
    api_key: str
    base_url: str | None = None      # override (used by the offline mock server)
    max_retries: int = 4
    timeout: float = 20.0
    min_remaining_before_pause: int = 3   # be polite when the bucket is nearly empty
    rate_limit: RateLimitState = field(default_factory=RateLimitState)

    def __post_init__(self) -> None:
        if not self.api_key:
            raise FreshdeskError("FRESHDESK_API_KEY is missing.")
        if not self.base_url:
            if not self.domain:
                raise FreshdeskError("FRESHDESK_DOMAIN is missing.")
            d = self.domain.replace("https://", "").replace("http://", "").strip("/")
            if "." not in d:
                d = f"{d}.freshdesk.com"
            self.base_url = f"https://{d}/api/v2"
        self._http = httpx.Client(
            base_url=self.base_url,
            auth=(self.api_key, "X"),             # Freshdesk API-key auth
            timeout=self.timeout,
            verify=_ssl_verify(),
            headers={"Accept": "application/json", "User-Agent": "freshdesk-mcp-connector/1.0"},
        )

    # ------------------------------------------------------------------ setup
    @classmethod
    def from_env(cls) -> "FreshdeskClient":
        return cls(
            domain=os.getenv("FRESHDESK_DOMAIN", ""),
            api_key=os.getenv("FRESHDESK_API_KEY", ""),
            base_url=os.getenv("FRESHDESK_BASE_URL") or None,
        )

    # --------------------------------------------------------------- core I/O
    def _request(self, path: str, params: dict | None = None) -> httpx.Response:
        """GET with rate-limit awareness and retries."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            # Proactive pause if we are about to exhaust the per-minute bucket.
            if self.rate_limit.remaining is not None and self.rate_limit.remaining <= self.min_remaining_before_pause:
                time.sleep(2)

            try:
                resp = self._http.get(path, params=params)
            except httpx.TransportError as e:
                if attempt >= self.max_retries:
                    raise FreshdeskError(f"Network error talking to Freshdesk: {e}") from e
                self._backoff(attempt)
                attempt += 1
                continue

            self._record_rate_headers(resp)

            if resp.status_code == 429:
                try:
                    retry_after = float(resp.headers.get("Retry-After", "5"))
                except ValueError:
                    retry_after = 5.0
                self.rate_limit.last_retry_after = retry_after
                self.rate_limit.throttled_count += 1
                if attempt >= self.max_retries:
                    raise FreshdeskError(
                        f"Rate limited by Freshdesk; retry after {retry_after:.0f}s.", 429
                    )
                time.sleep(min(retry_after, 60))
                attempt += 1
                continue

            if resp.status_code >= 500 and attempt < self.max_retries:
                self._backoff(attempt)
                attempt += 1
                continue

            if resp.status_code >= 400:
                raise FreshdeskError(self._explain_error(resp), resp.status_code)
            return resp

    def _record_rate_headers(self, resp: httpx.Response) -> None:
        h = resp.headers

        def _num(v):
            try:
                return int(float(v)) if v is not None else None
            except ValueError:
                return None

        total = _num(h.get("X-Ratelimit-Total"))
        remaining = _num(h.get("X-Ratelimit-Remaining"))
        if total is not None:
            self.rate_limit.total = total
        if remaining is not None:
            self.rate_limit.remaining = remaining

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(2 ** attempt, 16) + random.uniform(0, 0.5))

    @staticmethod
    def _explain_error(resp: httpx.Response) -> str:
        friendly = {
            400: "Bad request - check the filters/query you sent.",
            401: "Authentication failed - the API key is wrong or missing.",
            403: "Forbidden - this API key's agent lacks permission (or API access is not on your plan).",
            404: "Not found - that ID does not exist (or the domain is wrong).",
        }
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:300]
        return f"Freshdesk HTTP {resp.status_code}: {friendly.get(resp.status_code, 'Request failed.')} Details: {body}"

    def _get_paged(self, path: str, params: dict, limit: int) -> list[dict]:
        """Collect up to `limit` items following Freshdesk pagination."""
        out: list[dict] = []
        page = 1
        per_page = min(100, max(1, limit))
        while len(out) < limit:
            resp = self._request(path, {**params, "page": page, "per_page": per_page})
            items = resp.json()
            out.extend(items)
            if 'rel="next"' not in resp.headers.get("Link", "") or not items:
                break
            page += 1
        return out[:limit]

    # ------------------------------------------------------------- primitives
    def list_tickets(
        self,
        limit: int = 30,
        updated_since: str | None = None,
        order_by: str = "updated_at",
        order_type: str = "desc",
        predefined_filter: str | None = None,
        requester_email: str | None = None,
    ) -> list[dict]:
        """List tickets. NB: without updated_since Freshdesk returns only the last 30 days."""
        params = {
            "updated_since": updated_since,
            "order_by": order_by,
            "order_type": order_type,
            "filter": predefined_filter,   # new_and_my_open | watching | spam | deleted
            "email": requester_email,
            "include": "description",
        }
        return [simplify_ticket(t) for t in self._get_paged("/tickets", params, limit)]

    def get_ticket(self, ticket_id: int, include_conversations: bool = True) -> dict:
        include = "conversations,requester,stats" if include_conversations else "requester,stats"
        t = self._request(f"/tickets/{int(ticket_id)}", {"include": include}).json()
        result = simplify_ticket(t)
        result["description"] = t.get("description_text") or t.get("description")
        if t.get("requester"):
            r = t["requester"]
            result["requester"] = {"id": r.get("id"), "name": r.get("name"), "email": r.get("email")}
        if include_conversations:
            result["conversations"] = [simplify_conversation(c) for c in t.get("conversations", [])]
        return result

    def list_ticket_conversations(self, ticket_id: int, limit: int = 50) -> list[dict]:
        items = self._get_paged(f"/tickets/{int(ticket_id)}/conversations", {}, limit)
        return [simplify_conversation(c) for c in items]

    def search_tickets(self, query: str, page: int = 1) -> dict:
        """
        Freshdesk filter-search. `query` uses Freshdesk syntax, e.g.
            status:2 AND priority:4
            tag:'refund' AND created_at:>'2026-09-01'
        Returns up to 30 results per page, max 10 pages (300 results).
        """
        q = query.strip()
        if not (q.startswith('"') and q.endswith('"')):
            q = f'"{q}"'
        if len(q) > 512:
            raise FreshdeskError("Search query too long (Freshdesk max is 512 characters).")
        data = self._request("/search/tickets", {"query": q, "page": max(1, min(10, page))}).json()
        return {
            "total": data.get("total", 0),
            "page": page,
            "results": [simplify_ticket(t) for t in data.get("results", [])],
        }

    def get_contact(self, contact_id: int) -> dict:
        return simplify_contact(self._request(f"/contacts/{int(contact_id)}").json())

    def search_contacts(self, email: str | None = None, phone: str | None = None,
                        name_prefix: str | None = None) -> list[dict]:
        if email:
            items = self._request("/contacts", {"email": email}).json()
        elif phone:
            items = self._request("/contacts", {"phone": phone}).json()
        elif name_prefix:
            items = self._request("/contacts/autocomplete", {"term": name_prefix}).json()
        else:
            raise FreshdeskError("Provide email, phone or name_prefix.")
        return [simplify_contact(c) for c in items]

    def whoami(self) -> dict:
        """Cheap auth check - returns the agent that owns the API key."""
        me = self._request("/agents/me").json()
        return {"id": me.get("id"), "name": me.get("contact", {}).get("name"),
                "email": me.get("contact", {}).get("email")}


# --------------------------------------------------------- response shaping
def simplify_ticket(t: dict) -> dict:
    return {
        "id": t.get("id"),
        "subject": t.get("subject"),
        "status": STATUS.get(t.get("status"), t.get("status")),
        "priority": PRIORITY.get(t.get("priority"), t.get("priority")),
        "source": SOURCE.get(t.get("source"), t.get("source")),
        "type": t.get("type"),
        "tags": t.get("tags", []),
        "requester_id": t.get("requester_id"),
        "responder_id": t.get("responder_id"),
        "group_id": t.get("group_id"),
        "created_at": t.get("created_at"),
        "updated_at": t.get("updated_at"),
        "due_by": t.get("due_by"),
        "description_preview": (t.get("description_text") or "")[:300] or None,
    }


def simplify_conversation(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "from_email": c.get("from_email"),
        "incoming": c.get("incoming"),
        "private_note": c.get("private"),
        "body": c.get("body_text") or c.get("body"),
        "created_at": c.get("created_at"),
    }


def simplify_contact(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "name": c.get("name"),
        "email": c.get("email"),
        "phone": c.get("phone"),
        "company_id": c.get("company_id"),
        "created_at": c.get("created_at"),
    }


def build_ticket_query(status: str | None = None, priority: str | None = None,
                       tag: str | None = None, created_after: str | None = None,
                       created_before: str | None = None, agent_id: int | None = None,
                       group_id: int | None = None) -> str:
    """Turn friendly filters into a Freshdesk search query string."""
    parts: list[str] = []
    if status:
        code = STATUS_BY_NAME.get(status.lower())
        if code is None:
            raise FreshdeskError(f"Unknown status '{status}'. Use one of: {', '.join(STATUS.values())}")
        parts.append(f"status:{code}")
    if priority:
        code = PRIORITY_BY_NAME.get(priority.lower())
        if code is None:
            raise FreshdeskError(f"Unknown priority '{priority}'. Use one of: {', '.join(PRIORITY.values())}")
        parts.append(f"priority:{code}")
    if tag:
        parts.append(f"tag:'{tag}'")
    if created_after:
        parts.append(f"created_at:>'{created_after}'")
    if created_before:
        parts.append(f"created_at:<'{created_before}'")
    if agent_id:
        parts.append(f"agent_id:{int(agent_id)}")
    if group_id:
        parts.append(f"group_id:{int(group_id)}")
    if not parts:
        raise FreshdeskError("Give at least one filter (status, priority, tag, dates, agent_id, group_id).")
    return " AND ".join(parts)
