"""
mock_freshdesk.py - a tiny fake Freshdesk API used for offline testing.

It imitates the real API's: Basic-auth API key, pagination + Link header,
X-Ratelimit-* headers, HTTP 429 + Retry-After, /search/tickets, contacts.
All data is fictional.
"""
from __future__ import annotations

import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VALID_KEY = "test-api-key"
_lock = threading.Lock()
STATE = {"force_429_next": 0, "remaining": 50}

CONTACTS = [
    {"id": 1000 + i, "name": n, "email": f"{n.split()[0].lower()}@example.com",
     "phone": f"+1555000{i:04d}", "company_id": None, "created_at": "2026-08-01T10:00:00Z"}
    for i, n in enumerate(["Asha Rao", "Ben Ortiz", "Chen Wei", "Dana Kim", "Eli Novak"])
]

TICKETS = []
for i in range(1, 151):  # 150 tickets -> forces 2 pages at per_page=100
    TICKETS.append({
        "id": i,
        "subject": f"Order #{5000 + i} - {['Refund request', 'Late delivery', 'Wrong size', 'Payment failed'][i % 4]}",
        "status": [2, 3, 4, 5][i % 4],
        "priority": [1, 2, 3, 4][(i // 4) % 4],
        "source": 1,
        "type": "Question",
        "tags": ["refund"] if i % 4 == 0 else ["shipping"],
        "requester_id": CONTACTS[i % 5]["id"],
        "responder_id": None,
        "group_id": None,
        "created_at": f"2026-09-{(i % 28) + 1:02d}T09:00:00Z",
        "updated_at": f"2026-09-{(i % 28) + 1:02d}T12:00:00Z",
        "due_by": "2026-10-10T09:00:00Z",
        "description_text": f"Hi, I need help with order #{5000 + i}.",
    })
CONVERSATIONS = {
    i: [
        {"id": 1, "from_email": "ben@example.com", "incoming": True, "private": False,
         "body_text": "Any update?", "created_at": "2026-09-02T10:00:00Z"},
        {"id": 2, "from_email": "support@acme.test", "incoming": False, "private": False,
         "body_text": "Shipped today!", "created_at": "2026-09-02T11:00:00Z"},
    ] for i in range(1, 151)
}


def _match(t: dict, query: str) -> bool:
    for clause in query.strip('"').split(" AND "):
        k, v = clause.split(":", 1)
        k, v = k.strip(), v.strip()
        if k in ("status", "priority", "agent_id", "group_id"):
            if str(t.get(k)) != v:
                return False
        elif k == "tag":
            if v.strip("'") not in t["tags"]:
                return False
        elif k == "created_at":
            op, date = v[0], v[1:].strip("'")
            if op == ">" and not t["created_at"][:10] > date:
                return False
            if op == "<" and not t["created_at"][:10] < date:
                return False
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # keep test output clean
        pass

    def _send(self, code: int, body, extra: dict | None = None):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Ratelimit-Total", "50.0")
        self.send_header("X-Ratelimit-Remaining", f"{STATE['remaining']}.0")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"{VALID_KEY}:X".encode()).decode()
        if auth != expected:
            return self._send(401, {"code": "invalid_credentials", "message": "You have to be logged in to perform this action."})

        with _lock:
            if STATE["force_429_next"] > 0:
                STATE["force_429_next"] -= 1
                return self._send(429, {"message": "rate limited"}, {"Retry-After": "1"})
            STATE["remaining"] = max(10, STATE["remaining"] - 1)

        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path.removeprefix("/api/v2")

        if path == "/agents/me":
            return self._send(200, {"id": 42, "contact": {"name": "Test Agent", "email": "agent@acme.test"}})

        if path == "/tickets":
            items = TICKETS
            if "email" in q:
                ids = {c["id"] for c in CONTACTS if c["email"] == q["email"]}
                items = [t for t in items if t["requester_id"] in ids]
            items = sorted(items, key=lambda t: t.get(q.get("order_by", "updated_at")),
                           reverse=q.get("order_type", "desc") == "desc")
            page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
            chunk = items[(page - 1) * per: page * per]
            extra = {}
            if page * per < len(items):
                extra["Link"] = f'<{u.path}?page={page + 1}&per_page={per}>; rel="next"'
            return self._send(200, chunk, extra)

        m = re.fullmatch(r"/tickets/(\d+)", path)
        if m:
            t = next((t for t in TICKETS if t["id"] == int(m.group(1))), None)
            if not t:
                return self._send(404, {"code": "not_found"})
            full = dict(t)
            inc = q.get("include", "")
            if "conversations" in inc:
                full["conversations"] = CONVERSATIONS.get(t["id"], [])
            if "requester" in inc:
                full["requester"] = next(c for c in CONTACTS if c["id"] == t["requester_id"])
            return self._send(200, full)

        m = re.fullmatch(r"/tickets/(\d+)/conversations", path)
        if m:
            return self._send(200, CONVERSATIONS.get(int(m.group(1)), []))

        if path == "/search/tickets":
            query = q.get("query", "")
            if not (query.startswith('"') and query.endswith('"')):
                return self._send(400, {"description": "query must be wrapped in double quotes"})
            res = [t for t in TICKETS if _match(t, query)]
            page = int(q.get("page", 1))
            return self._send(200, {"results": res[(page - 1) * 30: page * 30], "total": len(res)})

        if path == "/contacts":
            res = CONTACTS
            if "email" in q:
                res = [c for c in res if c["email"] == q["email"]]
            if "phone" in q:
                res = [c for c in res if c["phone"] == q["phone"]]
            return self._send(200, res)

        if path == "/contacts/autocomplete":
            term = q.get("term", "").lower()
            return self._send(200, [c for c in CONTACTS if c["name"].lower().startswith(term)])

        m = re.fullmatch(r"/contacts/(\d+)", path)
        if m:
            c = next((c for c in CONTACTS if c["id"] == int(m.group(1))), None)
            return self._send(200, c) if c else self._send(404, {"code": "not_found"})

        return self._send(404, {"code": "not_found"})


def start(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/api/v2"


if __name__ == "__main__":
    srv, url = start(8765)
    print(f"Mock Freshdesk running at {url}  (API key: {VALID_KEY})  Ctrl+C to stop")
    srv.serve_forever()
