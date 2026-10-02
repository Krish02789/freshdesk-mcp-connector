# Freshdesk Private Connector (MCP)

A **read-only** MCP connector that lets an Agent Studio agent (or any MCP client) read **tickets, ticket conversations and customers** from Freshdesk. It uses Freshdesk **API-key auth**, provides **list, get and search** primitives, handles **rate limits** (429 + `Retry-After`, backoff), and is protected by a **bearer token** when hosted.

**No credentials are in this repo.** Secrets go in a local `.env` file (git-ignored) or in the hosting dashboard. The test data in `mock_freshdesk.py` is fictional.

## Files
| File | What it is |
|---|---|
| `freshdesk_client.py` | Freshdesk API client: auth, pagination, rate-limit handling, errors |
| `server.py` | MCP server exposing 7 tools (stdio for desktop, HTTP for hosted) |
| `mcp_tools.json` | MCP tool specification (names, descriptions, JSON input schemas) |
| `test_connector.py` | Test script, 15 checks (`--live` runs against your real Freshdesk) |
| `test_remote.py` | Checks the deployed URL end to end |
| `mock_freshdesk.py` | Fake Freshdesk used for offline tests (fictional data) |
| `render.yaml` | One-click hosting config for Render |
| `.env.example` | Template for your secrets (copy it to `.env`) |

---

## Setup and run (step by step)

### Step 0: Things you need
- A Mac or PC with **Python 3.10+**. Type `python3 --version` in Terminal. If it's missing, install it from python.org.
- A free **GitHub** account (to share the code) and a free **Render** account (to host it).

### Step 1: Get a free Freshdesk and your API key
1. Go to **freshworks.com/freshdesk** and click **Free trial**. Sign up and pick a helpdesk name, for example `aayush-demo`. Your helpdesk is now `https://aayush-demo.freshdesk.com`.
2. Make some tickets. Click **New → Ticket** and create 5–6 fake ones with different priorities, statuses and a tag like `refund`. Use made-up names and emails only.
3. Get the API key. Click your **profile picture (top-right) → Profile settings → View API key**, then copy it.

### Step 2: Install the project
```bash
cd freshdesk-connector
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then open .env and fill in the 3 values
```
In `.env`, `FRESHDESK_DOMAIN` is just the subdomain (`aayush-demo`). `CONNECTOR_TOKEN` is any long random password you make up.

### Step 3: Run the tests
```bash
python test_connector.py           # offline, uses the fake Freshdesk -> expect "15/15 checks passed"
python test_connector.py --live    # uses YOUR Freshdesk from .env
```

### Step 4 (optional): Try it with a desktop agent (Claude Desktop)
Add this to `~/Library/Application Support/Claude/claude_desktop_config.json` (use your real paths), then restart the app:
```json
{ "mcpServers": { "freshdesk": {
    "command": "/FULL/PATH/freshdesk-connector/.venv/bin/python",
    "args": ["/FULL/PATH/freshdesk-connector/server.py"] } } }
```
Ask: *"Show me all open urgent tickets."*

### Step 5: Put the code on GitHub
1. On github.com click **New repository**, name it `freshdesk-mcp-connector`, and choose **Private** or **Public**.
2. In the project folder:
```bash
git init && git add . && git commit -m "Freshdesk MCP connector"
git branch -M main
git remote add origin https://github.com/<you>/freshdesk-mcp-connector.git
git push -u origin main
```
`.env` is in `.gitignore`, so your key is **not** uploaded. Check this on GitHub after pushing.

### Step 6: Host it online (Render, free)
1. On **render.com**, sign in with GitHub, then click **New → Blueprint** and pick your repo. Render reads `render.yaml`.
2. When asked, fill in **FRESHDESK_DOMAIN** and **FRESHDESK_API_KEY**. `CONNECTOR_TOKEN` is generated for you.
3. Click **Apply** and wait for it to show **Live**. Copy the URL, for example `https://freshdesk-mcp-connector.onrender.com`.
4. Open **Environment** on the service and copy the value of `CONNECTOR_TOKEN`.
5. Check it from your laptop:
```bash
python test_remote.py https://freshdesk-mcp-connector.onrender.com <CONNECTOR_TOKEN>
```
On the free plan the server sleeps after about 15 minutes idle, so the first call can take about 50 seconds.

### Step 7: Connect it to Agent Studio
In Agent Studio, add a custom **MCP server / tool connection** with:
- **URL:** `https://<your-app>.onrender.com/mcp`, transport **Streamable HTTP**
- **Header:** `Authorization: Bearer <CONNECTOR_TOKEN>`

Agent Studio should then list the 7 tools. If your Agent Studio uses an OpenAPI or function-tool format instead of MCP, the same schemas are in `mcp_tools.json`.

Example prompts: *"List urgent open tickets"*, *"Summarise ticket 12"*, *"Find all tickets from asha@example.com"*.

---

## Assumptions
- Freshdesk is the chosen tool. The data read is **tickets, conversations and contacts**, because Freshdesk is a helpdesk and has no orders or inventory.
- API-key auth is used (Freshdesk's standard REST auth). OAuth would need a Freshworks Marketplace app (see long-term fix).
- One Freshdesk account per deployment. The agent sees what the API key's agent can see.
- Agent Studio can connect to a remote MCP server over Streamable HTTP with a custom header.
- Read-only by design, to keep a first connector safe.

## Tools (primitives)

| Tool | Type | What it does |
|---|---|---|
| `check_connection` | health | Confirms the API key works; shows rate-limit budget |
| `list_tickets` | list | Recent tickets, sortable, filter by requester email / built-in view / `updated_since`; auto-paginates up to 300 |
| `get_ticket` | get | One ticket with full description, requester and conversation thread |
| `list_ticket_conversations` | list | Every reply/note on a long ticket |
| `search_tickets` | search | Filter by status, priority, tag, created date range, agent, group, or a raw Freshdesk query |
| `get_contact` | get | A customer by ID |
| `search_contacts` | search | A customer by email, phone or name prefix |

Full JSON schemas: `mcp_tools.json`.

## What the agent CAN do
- Answer "What urgent tickets are open right now?" or "Show refund tickets created this week."
- Summarise a ticket's full back-and-forth before a human replies.
- Look up a customer by email and list all their tickets.
- Report status/priority in plain words (Freshdesk's numeric codes are translated).
- Keep working under load: it waits and retries on HTTP 429 using `Retry-After`, slows down when `X-Ratelimit-Remaining` runs low, and retries 5xx/network errors with exponential backoff.
- Return clear, structured errors (bad key, missing permission, unknown ID) instead of crashing.

## What the agent CANNOT do
- **No writes:** it can't create, update, reply to, assign, merge or delete tickets or contacts. This is deliberate for a first version.
- **No free-text search:** Freshdesk's search API filters on fields only. "Find tickets mentioning 'broken zipper'" isn't supported. The agent can list recent tickets and read them instead.
- **Search caps:** 30 results per page, 10 pages (300 results) per query, and a 512-character query limit.
- **30-day default window:** `list_tickets` returns only the last 30 days unless `updated_since` is given.
- **No attachments content:** attachment files aren't downloaded or read.
- **No orders or inventory:** Freshdesk is a helpdesk, so order data lives in the store (Shopify, WooCommerce and so on). It only shows up here if it's written in the ticket text or in custom fields.
- **Single tenant, single identity:** one API key per deployment. The agent sees exactly what that Freshdesk agent can see, with no per-end-user permissions.
- **Plan-dependent limits:** the rate limit per minute depends on the Freshdesk plan, so heavy agents on small plans will slow down.

## Known limitations of this build, and the long-term fix

| Limitation now | Long-term fix |
|---|---|
| API key in an environment variable (one shared identity) | Build a Freshworks Marketplace app with OAuth so each workspace or user connects their own account, and store tokens in a secrets manager with rotation |
| Static bearer token protects the hosted endpoint | Use MCP's standard OAuth 2.1 authorization (the spec's protected-resource flow) or Agent Studio's native auth, with per-agent scopes |
| Read-only | Add `reply_to_ticket`, `add_note` and `update_ticket_status`, gated behind human approval and audit logging |
| Rate limiting is per process (in memory) | Use a shared limiter (for example Redis) when running several replicas; add caching for hot tickets |
| Polling only | Subscribe to Freshdesk webhooks or automations to push ticket events to the agent |
| No full-text search | Index tickets into a vector or keyword store, synced by webhooks, and expose a `semantic_search_tickets` tool |
