"""
test_remote.py - check your DEPLOYED connector from your laptop.

    python test_remote.py https://your-app.onrender.com YOUR_CONNECTOR_TOKEN
"""
import asyncio

try:  # trust the OS certificate store (needed behind corporate SSL inspection)
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass
import json
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(base: str, token: str) -> None:
    base = base.rstrip("/")
    print("health:", httpx.get(f"{base}/health", timeout=90).json())
    print("without token ->", httpx.post(f"{base}/mcp", json={}, timeout=30).status_code, "(should be 401)")
    async with streamablehttp_client(f"{base}/mcp", headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("tools:", [t.name for t in (await s.list_tools()).tools])
            for name, args in [("check_connection", {}),
                               ("list_tickets", {"limit": 5}),
                               ("search_tickets", {"status": "Open"})]:
                res = json.loads((await s.call_tool(name, args)).content[0].text)
                print(f"\n--- {name} ---\n{json.dumps(res, indent=2)[:1200]}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
