"""An in-memory OpenSearch double that **executes** the real permission filter.

The engine half of the ADR-0019 §2 parity proof. Rather than asserting on the
*shape* of ``SearchAllowFilter.to_engine_filter()`` (which can drift into
agreeing with itself), this drives the **real**
:class:`~app.search.store.OpenSearchStore` over ``httpx.MockTransport``:

* the real bulk writer serialises the real :class:`~app.search.store.IndexedChunk`
  projection, so the documents evaluated here are byte-for-byte what production
  would index;
* the real ``hybrid_search`` builds the real query body, and this module
  interprets the filter clauses it carries.

Two properties keep the double honest:

* **unknown clause ⇒ raise.** :class:`UnsupportedClause` fires on any DSL
  construct the evaluator does not model, so a future widening of the filter can
  never silently pass the parity test by being ignored.
* **match-all scoring.** The BM25 ``must`` leg is deliberately treated as
  match-all: this double answers "who is *permitted* to see this chunk", never
  "how relevant is it". Relevance can therefore never hide a leak.

It also asserts the ADR-0010 §4 requirement that the identical filter appears in
**both** hybrid legs — a candidate must be unreachable per leg, not just after
the merge.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx


class UnsupportedClause(AssertionError):
    """The filter emitted a DSL construct this evaluator does not model.

    Deliberately fatal: the parity proof is only meaningful while the double
    understands every clause the production filter can emit.
    """


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else [value]


def _as_datetime(value: object) -> datetime | None:
    """Parse an indexed/queried date, normalising to UTC.

    A ``date``-mapped OpenSearch field stores an instant, so a value written
    without an offset is read back as UTC. The offline corpus goes through
    SQLite, which drops tzinfo, so normalising here keeps the double faithful to
    the engine rather than to the test database.
    """
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def matches(doc: dict[str, Any], clause: dict[str, Any]) -> bool:
    """Evaluate one filter clause against one indexed document.

    Models exactly the vocabulary ``SearchAllowFilter.to_engine_filter()`` and
    the store's write-side queries emit: ``term``, ``terms``, ``range`` (``gte``
    on a date field), and ``bool`` with ``filter`` / ``should`` +
    ``minimum_should_match`` / ``must``. Anything else raises.
    """
    if "term" in clause:
        ((field, expected),) = clause["term"].items()
        return bool(doc.get(field) == expected)
    if "terms" in clause:
        ((field, wanted),) = clause["terms"].items()
        # A keyword field may hold a scalar or an array; `terms` matches if ANY
        # indexed value is in the wanted list. An empty wanted list matches
        # nothing — the fail-closed shape an empty principal set produces.
        return bool(set(map(str, _as_list(doc.get(field)))) & set(map(str, wanted)))
    if "range" in clause:
        ((field, bounds),) = clause["range"].items()
        unsupported = set(bounds) - {"gte"}
        if unsupported:
            raise UnsupportedClause(f"range bounds {sorted(unsupported)} in {clause!r}")
        value = _as_datetime(doc.get(field))
        floor = _as_datetime(bounds["gte"])
        # A missing/null date NEVER satisfies a range — stale ⇒ deny.
        return value is not None and floor is not None and value >= floor
    if "bool" in clause:
        body = clause["bool"]
        unsupported = set(body) - {"filter", "should", "must", "minimum_should_match"}
        if unsupported:
            raise UnsupportedClause(f"bool keys {sorted(unsupported)} in {clause!r}")
        if not all(matches(doc, sub) for sub in body.get("filter", [])):
            return False
        if not all(matches(doc, sub) for sub in body.get("must", [])):
            return False
        should = body.get("should")
        if should is not None:
            minimum = int(body.get("minimum_should_match", 1))
            if sum(1 for sub in should if matches(doc, sub)) < minimum:
                return False
        return True
    if "match" in clause:
        # Scoring, not permissions: treated as match-all on purpose (see module
        # docstring) so relevance can never mask a permission leak.
        return True
    raise UnsupportedClause(f"unsupported filter clause: {clause!r}")


class FakeEngine:
    """A MockTransport handler standing in for the OpenSearch cluster."""

    def __init__(self, index: str = "lumen-chunks-kit") -> None:
        self.index = index
        self.docs: dict[str, dict[str, Any]] = {}
        self.searches: list[list[dict[str, Any]]] = []

    # --- transport ------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "HEAD" and path == f"/{self.index}":
            return httpx.Response(200)
        if request.method == "PUT":
            return httpx.Response(200, json={"acknowledged": True})
        if request.method == "GET" and path == "/_cluster/health":
            return httpx.Response(200, json={"status": "green"})
        if request.method == "POST":
            # The bulk endpoint is index-less (the action header names it); the
            # rest are `/{index}/_op`. Route on the operation either way.
            operation = path.rsplit("/", 1)[-1]
            if operation == "_bulk":
                return self._bulk(request.content)
            if operation == "_search":
                return self._search(json.loads(request.content))
            if operation == "_delete_by_query":
                return self._delete_by_query(json.loads(request.content))
            if operation == "_update_by_query":
                return self._update_by_query(json.loads(request.content))
        raise UnsupportedClause(f"unmodelled engine call: {request.method} {path}")

    # --- operations -----------------------------------------------------------

    def _bulk(self, content: bytes) -> httpx.Response:
        lines = [line for line in content.decode("utf-8").splitlines() if line]
        for header_line, doc_line in zip(lines[::2], lines[1::2], strict=True):
            header = json.loads(header_line)
            doc_id = header["index"]["_id"]
            self.docs[doc_id] = json.loads(doc_line)
        return httpx.Response(200, json={"errors": False, "items": []})

    def _selected(self, body: dict[str, Any]) -> list[str]:
        query = body["query"]
        return [doc_id for doc_id, doc in self.docs.items() if matches(doc, query)]

    def _delete_by_query(self, body: dict[str, Any]) -> httpx.Response:
        for doc_id in self._selected(body):
            self.docs.pop(doc_id, None)
        return httpx.Response(200, json={"deleted": 0})

    def _update_by_query(self, body: dict[str, Any]) -> httpx.Response:
        script = body.get("script", {}).get("source", "")
        selected = self._selected(body)
        for doc_id in selected:
            if script == "ctx._source.acl_synced_at = null":
                self.docs[doc_id]["acl_synced_at"] = None
            elif script == "ctx._source.acl_synced_at = params.stamp":
                self.docs[doc_id]["acl_synced_at"] = body["script"]["params"]["stamp"]
            else:  # pragma: no cover — a new script must be modelled explicitly
                raise UnsupportedClause(f"unmodelled update script: {script!r}")
        return httpx.Response(200, json={"updated": len(selected)})

    def _search(self, body: dict[str, Any]) -> httpx.Response:
        legs = body["query"]["hybrid"]["queries"]
        bm25_filter = legs[0]["bool"]["filter"]
        knn_filter = legs[1]["knn"]["embedding"]["filter"]["bool"]["filter"]
        # ADR-0010 §4: the permission filter must be in BOTH legs — a candidate
        # has to be unreachable per leg, not merely after the merge.
        assert bm25_filter == knn_filter, "hybrid legs carry different permission filters"
        self.searches.append(bm25_filter)
        wrapper = {"bool": {"filter": bm25_filter}}
        hits = [
            {"_score": 1.0, "_source": doc} for doc in self.docs.values() if matches(doc, wrapper)
        ]
        return httpx.Response(200, json={"hits": {"hits": hits[: int(body.get("size", 10))]}})


__all__ = ["FakeEngine", "UnsupportedClause", "matches"]
