"""Live benchmark runner — upload the corpus, ask the bank, score the answers (#430).

Drives the REAL stack over HTTP end-to-end: login → collection → multipart
uploads (including the deliberate disallowed-format rejections) → wait for
Celery ingestion (parse → chunk → embed → index) → then, per question, score

* **retrieval** via ``GET /search`` — was a gold document in the top-k
  (hit@5 / hit@10 / MRR@10, answerable questions only); and
* **grounded answering** via the chat API — fact recall (which
  ``answer_facts`` the streamed answer contains), citation-file correctness
  (INV-3 at file granularity), and honest-refusal behaviour on the
  unanswerable slice (refusal + zero citations = correct; a cited confident
  answer = hallucination).

Results land under ``corpus/_results/<run-id>/`` (git-ignored): a per-question
CSV, a summary markdown, and the aggregate JSON. The runner is idempotent per
stack: files already uploaded (matched by filename) are reused, so a re-run
after a partial failure continues rather than re-ingesting.

Usage (bench stack per compose.bench.yml; from ``backend/``):

    uv run --extra dev python -m tests.eval.benchmark.run_live \
        --api http://localhost:47281 --email bench@lumen.test \
        --password lumen-bench-local [--skip-chat] [--only bm-001,bm-002]

This is test tooling driving a server over HTTP — the sync httpx client and
polling loops are deliberate; nothing here runs on the app's event loop.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from tests.eval.benchmark.bank import BenchmarkQuestion, load_questions, normalize
from tests.eval.benchmark.client import ApiClient
from tests.eval.benchmark.manifest import CORPUS, corpus_dir
from tests.eval.metrics import is_refusal

_POLL_SECONDS = 3.0
_INGEST_TIMEOUT_SECONDS = 45 * 60  # free-tier embedding of ~19 MB of text is slow
_ANSWER_TIMEOUT_SECONDS = 180.0
_SEARCH_K = 10


@dataclass(slots=True)
class UploadOutcome:
    """What happened to one corpus file at the upload/ingestion stage."""

    file_id: str
    filename: str
    expected: str  # "ok" | "rejected_type"
    status: str  # "ready" | "failed" | "rejected:<code>" | "timeout" | "unexpected:<code>"
    document_id: str | None = None


@dataclass(slots=True)
class QuestionOutcome:
    """Scores for one question against the live stack."""

    qid: str
    category: str
    difficulty: bool | str
    answerable: bool
    retrieval_rank: int | None = None  # 1-based rank of the first gold doc in /search
    facts_found: int = 0
    facts_total: int = 0
    cited_gold_file: bool = False
    cited_anything: bool = False
    refused: bool = False
    answer_chars: int = 0
    error: str = ""
    answer_preview: str = ""


@dataclass(slots=True)
class RunState:
    """Eval-run state around the shared :class:`ApiClient` (#441 extraction)."""

    api: ApiClient
    collection_id: str = ""
    doc_ids: dict[str, str] = field(default_factory=dict)  # file_id -> document_id
    uploads: list[UploadOutcome] = field(default_factory=list)


def _upload_corpus(state: RunState) -> None:
    existing = state.api.existing_documents()
    for entry in CORPUS:
        prior = existing.get(entry.filename)
        if prior is not None and entry.expected_ingest == "ok":
            state.doc_ids[entry.file_id] = str(prior["id"])
            state.uploads.append(
                UploadOutcome(
                    entry.file_id,
                    entry.filename,
                    entry.expected_ingest,
                    str(prior["status"]),
                    str(prior["id"]),
                )
            )
            print(f"[reuse ] {entry.file_id}: already uploaded (status={prior['status']})")
            continue
        # Bytes (not a handle) so a 401-refresh replay can re-send the body.
        payload = (corpus_dir() / entry.filename).read_bytes()
        response = state.api.upload_document(
            collection_id=state.collection_id,
            filename=entry.filename,
            data=payload,
            mime_type=entry.mime_type,
        )
        if entry.expected_ingest == "rejected_type":
            outcome = (
                f"rejected:{response.status_code}"
                if response.status_code == 415
                else f"unexpected:{response.status_code}"
            )
            state.uploads.append(
                UploadOutcome(entry.file_id, entry.filename, entry.expected_ingest, outcome)
            )
            print(f"[negatv] {entry.file_id}: {outcome} (415 expected)")
            continue
        if response.status_code != 201:
            state.uploads.append(
                UploadOutcome(
                    entry.file_id,
                    entry.filename,
                    entry.expected_ingest,
                    f"unexpected:{response.status_code}",
                )
            )
            print(
                f"[FAILED] {entry.file_id}: upload -> {response.status_code} "
                f"{response.text[:120]}"
            )
            continue
        document_id = str(response.json()["id"])
        state.doc_ids[entry.file_id] = document_id
        state.uploads.append(
            UploadOutcome(
                entry.file_id, entry.filename, entry.expected_ingest, "pending", document_id
            )
        )
        print(f"[upload] {entry.file_id}: document {document_id}")


def _wait_for_ingestion(state: RunState) -> None:
    pending = {
        u.filename: u for u in state.uploads if u.expected == "ok" and u.document_id is not None
    }
    outcomes = state.api.wait_for_documents(set(pending), timeout_seconds=_INGEST_TIMEOUT_SECONDS)
    for filename, status in outcomes.items():
        pending[filename].status = status
        if status == "timeout":
            print(
                f"[timout] {pending[filename].file_id}: "
                f"not ready after {_INGEST_TIMEOUT_SECONDS}s"
            )


def _gold_document_ids(state: RunState, question: BenchmarkQuestion) -> set[str]:
    return {state.doc_ids[fid] for fid in question.source_files if fid in state.doc_ids}


def _score_retrieval(state: RunState, question: BenchmarkQuestion) -> int | None:
    response = state.api.request(
        "GET",
        "/api/v1/search",
        params={"q": question.question, "limit": str(_SEARCH_K)},
    )
    response.raise_for_status()
    gold = _gold_document_ids(state, question)
    for rank, result in enumerate(response.json().get("results", []), start=1):
        if str(result.get("document_id")) in gold:
            return rank
    return None


def _ask_chat(
    state: RunState, question: BenchmarkQuestion, model: str
) -> tuple[str, list[dict[str, object]]]:
    """One chat session per question; return (answer_text, citations)."""
    created = state.api.request(
        "POST",
        "/api/v1/chat/sessions",
        json={"title": f"bench {question.qid}", "model": model},
    )
    created.raise_for_status()
    session_id = created.json()["id"]
    sent = state.api.request(
        "POST",
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": question.question, "model": model},
    )
    sent.raise_for_status()

    deadline = time.monotonic() + _ANSWER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        listing = state.api.request("GET", f"/api/v1/chat/sessions/{session_id}/messages")
        listing.raise_for_status()
        assistant = [
            m
            for m in listing.json().get("items", [])
            if m["role"] == "assistant" and str(m.get("content", "")).strip()
        ]
        if assistant:
            final = assistant[-1]
            return str(final["content"]), list(final.get("citations", []))
        time.sleep(_POLL_SECONDS)
    return "", []


def _score_question(
    state: RunState, question: BenchmarkQuestion, *, model: str, skip_chat: bool
) -> QuestionOutcome:
    outcome = QuestionOutcome(
        qid=question.qid,
        category=question.category,
        difficulty=question.difficulty,
        answerable=question.answerable,
        facts_total=len(question.answer_facts),
    )
    try:
        if question.answerable:
            outcome.retrieval_rank = _score_retrieval(state, question)
    except httpx.HTTPError as exc:
        outcome.error = f"search: {exc}"
        return outcome
    if skip_chat:
        return outcome
    try:
        answer, citations = _ask_chat(state, question, model)
    except httpx.HTTPError as exc:
        outcome.error = f"chat: {exc}"
        return outcome
    outcome.answer_chars = len(answer)
    outcome.answer_preview = " ".join(answer.split())[:160]
    if not answer:
        outcome.error = "no answer before timeout"
        return outcome
    normalized_answer = normalize(answer)
    outcome.refused = is_refusal(answer)
    outcome.facts_found = sum(
        1 for fact in question.answer_facts if normalize(fact) in normalized_answer
    )
    outcome.cited_anything = bool(citations)
    gold = _gold_document_ids(state, question)
    outcome.cited_gold_file = any(str(c.get("document_id")) in gold for c in citations)
    return outcome


def _aggregate(outcomes: list[QuestionOutcome]) -> dict[str, object]:
    answerable = [o for o in outcomes if o.answerable and not o.error]
    unanswerable = [o for o in outcomes if not o.answerable and not o.error]

    def ratio(num: int, den: int) -> float:
        return round(num / den, 3) if den else 0.0

    with_rank = [o for o in answerable if o.retrieval_rank is not None]
    summary: dict[str, object] = {
        "questions": len(outcomes),
        "errors": sum(1 for o in outcomes if o.error),
        "retrieval_hit@5": ratio(
            sum(1 for o in with_rank if (o.retrieval_rank or 99) <= 5), len(answerable)
        ),
        "retrieval_hit@10": ratio(len(with_rank), len(answerable)),
        "retrieval_mrr@10": round(
            sum(1 / (o.retrieval_rank or 10**9) for o in with_rank) / len(answerable), 3
        )
        if answerable
        else 0.0,
        "answer_fact_recall": ratio(
            sum(o.facts_found for o in answerable), sum(o.facts_total for o in answerable)
        ),
        "answer_all_facts": ratio(
            sum(1 for o in answerable if o.facts_total and o.facts_found == o.facts_total),
            len(answerable),
        ),
        "citation_gold_file": ratio(
            sum(1 for o in answerable if o.cited_gold_file), len(answerable)
        ),
        "wrong_refusals": ratio(sum(1 for o in answerable if o.refused), len(answerable)),
        "refusal_accuracy": ratio(
            sum(1 for o in unanswerable if o.refused and not o.cited_anything),
            len(unanswerable),
        ),
        "hallucinated_unanswerable": ratio(
            sum(1 for o in unanswerable if not o.refused), len(unanswerable)
        ),
    }
    per_category: dict[str, dict[str, float]] = {}
    by_cat: dict[str, list[QuestionOutcome]] = defaultdict(list)
    for o in outcomes:
        if not o.error:
            by_cat[o.category].append(o)
    for category, items in sorted(by_cat.items()):
        cat_answerable = [o for o in items if o.answerable]
        if cat_answerable:
            per_category[category] = {
                "n": len(cat_answerable),
                "hit@10": ratio(
                    sum(1 for o in cat_answerable if o.retrieval_rank is not None),
                    len(cat_answerable),
                ),
                "fact_recall": ratio(
                    sum(o.facts_found for o in cat_answerable),
                    sum(o.facts_total for o in cat_answerable),
                ),
                "cited_gold": ratio(
                    sum(1 for o in cat_answerable if o.cited_gold_file), len(cat_answerable)
                ),
            }
        else:
            per_category[category] = {
                "n": len(items),
                "refusal_accuracy": ratio(
                    sum(1 for o in items if o.refused and not o.cited_anything), len(items)
                ),
            }
    summary["per_category"] = per_category
    return summary


def _write_reports(
    run_dir: Path,
    uploads: list[UploadOutcome],
    outcomes: list[QuestionOutcome],
    summary: dict[str, object],
    *,
    model: str,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "questions.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "qid",
                "category",
                "answerable",
                "retrieval_rank",
                "facts_found",
                "facts_total",
                "cited_gold_file",
                "cited_anything",
                "refused",
                "answer_chars",
                "error",
                "answer_preview",
            ]
        )
        for o in outcomes:
            writer.writerow(
                [
                    o.qid,
                    o.category,
                    o.answerable,
                    o.retrieval_rank,
                    o.facts_found,
                    o.facts_total,
                    o.cited_gold_file,
                    o.cited_anything,
                    o.refused,
                    o.answer_chars,
                    o.error,
                    o.answer_preview,
                ]
            )
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    lines = [
        "# Live benchmark run",
        "",
        f"- generation model: `{model}`",
        f"- questions: {summary['questions']} ({summary['errors']} errored)",
        "",
        "## Uploads",
        "",
        "| outcome | count |",
        "|---|---|",
    ]
    counts: dict[str, int] = defaultdict(int)
    for upload in uploads:
        counts[upload.status] += 1
    for status, count in sorted(counts.items()):
        lines.append(f"| {status} | {count} |")
    lines += [
        "",
        "## Aggregate",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    for key, value in summary.items():
        if key != "per_category":
            lines.append(f"| {key} | {value} |")
    lines += ["", "## Per category", ""]
    per_category = summary["per_category"]
    assert isinstance(per_category, dict)
    for category, stats in per_category.items():
        lines.append(f"- **{category}**: {json.dumps(stats)}")
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the live RAG benchmark over HTTP.")
    parser.add_argument("--api", default="http://localhost:47281")
    parser.add_argument("--email", default="bench@lumen.test")
    parser.add_argument("--password", default="lumen-bench-local")
    parser.add_argument("--model", default="openrouter/tencent/hy3:free")
    parser.add_argument("--collection", default="RAG benchmark corpus")
    parser.add_argument("--only", help="comma-separated qids (default: whole bank)")
    parser.add_argument("--skip-chat", action="store_true", help="retrieval metrics only")
    parser.add_argument("--skip-upload", action="store_true", help="assume corpus is ingested")
    args = parser.parse_args(argv)

    questions = load_questions()
    if args.only:
        wanted = {token.strip() for token in args.only.split(",") if token.strip()}
        questions = tuple(q for q in questions if q.qid in wanted)

    with httpx.Client(timeout=httpx.Timeout(60.0, read=300.0)) as client:
        api = ApiClient(client, args.api, args.email, args.password)
        api.login()
        state = RunState(api=api)
        state.collection_id = api.ensure_collection(args.collection)
        print(f"collection: {state.collection_id}")

        if args.skip_upload:
            existing = state.api.existing_documents()
            for entry in CORPUS:
                row = existing.get(entry.filename)
                if row is not None:
                    state.doc_ids[entry.file_id] = str(row["id"])
        else:
            _upload_corpus(state)
            _wait_for_ingestion(state)

        ready = sum(1 for u in state.uploads if u.status == "ready")
        rejected = sum(1 for u in state.uploads if u.status.startswith("rejected:"))
        failed = [
            u
            for u in state.uploads
            if u.status in {"failed", "timeout"} or u.status.startswith("unexpected:")
        ]
        print(f"\ningestion: {ready} ready, {rejected} correctly rejected, {len(failed)} failed")
        for u in failed:
            print(f"  - {u.file_id}: {u.status}")

        outcomes: list[QuestionOutcome] = []
        for index, question in enumerate(questions, start=1):
            outcome = _score_question(state, question, model=args.model, skip_chat=args.skip_chat)
            outcomes.append(outcome)
            rank = outcome.retrieval_rank if outcome.answerable else "-"
            print(
                f"[{index:>3}/{len(questions)}] {question.qid} {question.category:<15} "
                f"rank={rank} facts={outcome.facts_found}/{outcome.facts_total} "
                f"refused={outcome.refused} err={outcome.error[:60]}"
            )

    summary = _aggregate(outcomes)
    run_dir = corpus_dir() / "_results" / time.strftime("%Y%m%d-%H%M%S")
    _write_reports(run_dir, state.uploads, outcomes, summary, model=args.model)
    print(f"\nreports -> {run_dir}")
    print(json.dumps({k: v for k, v in summary.items() if k != "per_category"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
