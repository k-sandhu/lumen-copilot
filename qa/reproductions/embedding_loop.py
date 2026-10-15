"""Live-only two-task embedding lifecycle probe for the QA777 finding.

Run in the configured backend environment with ``--live``. Calls the configured
embedding provider twice and prints types/flags only, never exception messages,
credentials, client-cache keys or vectors. Exit 1 means a call failed.
"""

from __future__ import annotations

import asyncio
import json
import sys


async def probe(number: int) -> dict[str, object]:
    from app.core.config import get_settings
    from app.llm import LLMGateway

    try:
        values = await LLMGateway(get_settings()).embed(
            [f"Synthetic QA777 task lifecycle probe {number}"]
        )
        return {"probe": number, "ok": True, "dimensions": len(values[0].vector)}
    except Exception as exc:
        chain = []
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen and len(chain) < 5:
            seen.add(id(current))
            message = str(current).lower()
            chain.append(
                {
                    "type": type(current).__name__,
                    "status": getattr(current, "status_code", None),
                    "closed_loop": "event loop is closed" in message,
                    "different_loop": "different loop" in message,
                    "rate_limit": "rate limit" in message or "too many requests" in message,
                }
            )
            current = current.__cause__ or current.__context__
        return {"probe": number, "ok": False, "safe_exception_chain": chain}


def main() -> int:
    if "--live" not in sys.argv:
        print("Opted out: pass --live in the configured backend environment (two provider calls).")
        return 2
    from app.tasks.runner import run_task

    runner = asyncio.run if "--plain" in sys.argv else run_task
    results = [runner(probe(1)), runner(probe(2))]
    print(json.dumps(results, indent=2))
    return 0 if all(item["ok"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
