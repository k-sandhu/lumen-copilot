"""The ``web_search`` internet-search tool, behind the governed registry (ADR-0014, #219).

The first agent tool that reaches **outside** the tenant's own corpus — the open
internet — so a different trust boundary (``risk:security``, egress) than the
read-only retrieval tools. Auto-discovered like every other impl (a new file with
a module-level ``TOOLS``; no registry edit — ADR-0008 §3 / issue #207 §1).

Governance (ADR-0014 §5), fail-closed:

* **off by default** — ``default_offered=False`` keeps it out of the ad-hoc chat
  default allow-list (``registry.default_allowlist``); it is offered to the model
  only when an assistant's allow-list includes it (which the assistant sets only
  when its ``knowledge_scope`` includes ``web``);
* **tenant/deploy enable** — even when advertised, the handler refuses unless
  ``settings.web_search_enabled`` is true, returning an ``ok=False`` *tool result*
  (the model reads it; the run continues) — a disabled deploy never crashes.

It is **T0 / read-only** (spec 0004 §2.5) — a search reads public info, it writes
nothing — so it bypasses the approval gate; the runner still enforces the
allow-list, bounds the call, audits ``tool.invoked``/``tool.result``, and records
a ``tool_invocations`` row (INV-6) for every invocation, including a governance
refusal.

Egress + rate limit are the adapter's concern (``app.search_web``): every result
-page fetch goes through the ``connectors/web/fetch.py`` SSRF chokepoint and every
search is admission-checked against the per-tenant Redis window. This handler is a
thin adapter: gate → call the service → render web results (URL + snippet +
fetched passage) as the tool reply, with the structured payload the runtime turns
into **web** citations (distinct from corpus ``document`` citations — INV-3).
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.domain.tools import ERROR_TOOL_ERROR, RiskTier, ToolHandlerResult
from app.search_web import (
    WebSearchRateLimited,
    WebSearchUnavailable,
    build_web_search_service,
)
from app.services.tools.types import ToolContext, ToolDefinition

# Stable ``ok=False`` codes distinct from the generic runner codes, so the model
# (and the trace) can tell *why* a web search did not run.
#: Web mode is not enabled for this tenant/deploy (ADR-0014 §5 — fail closed).
ERROR_WEB_DISABLED = "web_search_disabled"
#: The per-tenant web-search window is exhausted (ADR-0014 §3 — throttled).
ERROR_WEB_RATE_LIMITED = "web_search_rate_limited"
#: The search provider was unreachable / returned unusable data.
ERROR_WEB_UNAVAILABLE = "web_search_unavailable"

# How much of each result's cite-worthy text to surface back to the model (the
# fetched passage when a page was fetched, else the provider snippet). Keeps the
# tool reply compact; the full text still travels in the payload for the citation.
_REPLY_BUDGET = 700


def _clamp_k(value: object) -> int | None:
    """Coerce the model-supplied ``k`` to a positive int, or ``None`` (use default).

    A missing/garbage ``k`` becomes ``None`` so the service applies the configured
    default; the service also clamps to the configured max, so a hostile large
    value cannot widen the fan-out.
    """
    if not isinstance(value, int | float | str):
        return None
    try:
        k = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, k)


def _render(results: tuple[Any, ...]) -> str:
    """Render web results as the tool reply the model reads and may cite.

    Each result is labelled with its URL so the model can attribute a claim to the
    exact page, and carries the cite-worthy text: the extracted passage when the
    page was fetched through the SSRF chokepoint, else the provider snippet. Trimmed
    to a budget so the context stays bounded (INV-3 — the model may cite only text
    actually returned/fetched).
    """
    blocks: list[str] = []
    for i, r in enumerate(results, start=1):
        body = (r.fetched_passage or r.snippet or "").strip()
        if len(body) > _REPLY_BUDGET:
            body = body[:_REPLY_BUDGET].rstrip() + "…"
        title = r.title or r.url
        blocks.append(f"[{i}] {title}\n{r.url}\n{body}")
    return "\n\n".join(blocks)


def _payload(results: tuple[Any, ...]) -> dict[str, Any]:
    """The structured web-citation payload (distinct from corpus citations).

    A web citation carries the **URL** + the fetched snippet/passage — never a
    ``document_id`` (that would imply the content is in the corpus). ``fetched`` is
    true only when the page was retrieved through the chokepoint, so the runtime
    can honour INV-3 (a URL listed but never fetched cannot be cited as if its body
    were read).
    """
    return {
        "sourceType": "web",
        "resultCount": len(results),
        "results": [
            {
                "title": r.title,
                "url": r.url,
                "snippet": r.snippet,
                "publishedAt": r.published_at.isoformat() if r.published_at else None,
                "fetchedPassage": r.fetched_passage,
                "fetched": r.fetched_passage is not None,
            }
            for r in results
        ],
    }


async def _web_search(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    settings = get_settings()
    # Governance gate (ADR-0014 §5), fail-closed: a disabled tenant/deploy gets an
    # ``ok=False`` tool result, NOT a crash (issue #219 AC-3). The advertise-time
    # gate (default_offered=False + assistant knowledge_scope `web`) keeps it out of
    # the offered set; this is the belt-and-braces runtime gate.
    if not settings.web_search_enabled:
        return ToolHandlerResult(
            content=(
                "Web search is not enabled for this workspace. "
                "Answer from the connected documents instead."
            ),
            ok=False,
            error=ERROR_WEB_DISABLED,
            summary="web search disabled",
        )

    query = str(args.get("query") or "").strip()
    if not query:
        return ToolHandlerResult(content="No query provided.", summary="no query")

    service = build_web_search_service(settings, tenant_id=ctx.principal.tenant_id)
    try:
        results = await service.search(query, k=_clamp_k(args.get("k")))
    except WebSearchRateLimited:
        return ToolHandlerResult(
            content=(
                "Web search is temporarily rate-limited for this workspace. "
                "Try again shortly or answer from the connected documents."
            ),
            ok=False,
            error=ERROR_WEB_RATE_LIMITED,
            summary="rate limited",
        )
    except WebSearchUnavailable:
        return ToolHandlerResult(
            content="Web search is currently unavailable. Answer from the connected documents.",
            ok=False,
            error=ERROR_WEB_UNAVAILABLE,
            summary="provider unavailable",
        )
    except Exception:  # noqa: BLE001 — never leak a vendor error; the runner logs the type
        return ToolHandlerResult(
            content="Web search failed to complete. You may try a different approach.",
            ok=False,
            error=ERROR_TOOL_ERROR,
            summary="web search error",
        )

    if not results:
        return ToolHandlerResult(
            content="No web results were found for that query.",
            summary="0 results",
        )
    return ToolHandlerResult(
        content=_render(results),
        summary=f"{len(results)} web result(s)",
        hit_count=len(results),
        payload=_payload(results),
    )


TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="web_search",
        description=(
            "Search the public internet for current information and return ranked "
            "results (title, URL, and a snippet or an extracted passage from the "
            "page). Use this only when the answer needs up-to-date public web "
            "information the user's own documents do not contain. Always cite the "
            "URL of any web result you rely on."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The natural-language web search query.",
                },
                "k": {
                    "type": "integer",
                    "description": "How many results to return (bounded by config).",
                    "minimum": 1,
                },
            },
            "required": ["query"],
        },
        handler=_web_search,
        risk_tier=RiskTier.T0,
        read_only=True,
        # Off by default: governed/egress tool, NOT part of the ad-hoc default
        # allow-list (ADR-0014 §5). Offered only via an assistant allow-list whose
        # knowledge_scope includes `web`, and enabled per-tenant/deploy.
        default_offered=False,
    ),
)


__all__ = [
    "ERROR_WEB_DISABLED",
    "ERROR_WEB_RATE_LIMITED",
    "ERROR_WEB_UNAVAILABLE",
    "TOOLS",
]
