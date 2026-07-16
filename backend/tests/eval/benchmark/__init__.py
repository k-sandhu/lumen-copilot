"""Real-world RAG benchmark dataset — corpus manifest + grounded question bank (#420).

Extends the tiny in-code golden set (:mod:`tests.eval.golden`, #29) with a
reproducible benchmark over **real files downloaded from the internet**: every
upload-allowlisted format (PDF/DOCX/PPTX/XLSX/TXT/MD), sizes from ~13 KB to
~32 MB, multiple domains and languages, plus deliberate negative files the
pipeline must reject.

The corpus binaries are **never committed** — :mod:`tests.eval.benchmark.download`
fetches them into a git-ignored corpus dir and verifies the sha256 pins in
``checksums.json``. The question bank (``questions.jsonl``) is authored against
the text the **real ingestion parsers** extract, so every evidence quote is
machine-verifiable (:mod:`tests.eval.benchmark.verify`).

See ``README.md`` in this package for methodology and usage.
"""

from tests.eval.benchmark.bank import (
    BenchmarkQuestion,
    Evidence,
    golden_documents,
    golden_questions,
    load_questions,
)
from tests.eval.benchmark.manifest import (
    CORPUS,
    CorpusFile,
    corpus_dir,
    entry_by_id,
    load_checksums,
    size_class,
)

__all__ = [
    "CORPUS",
    "BenchmarkQuestion",
    "CorpusFile",
    "Evidence",
    "corpus_dir",
    "entry_by_id",
    "golden_documents",
    "golden_questions",
    "load_checksums",
    "load_questions",
    "size_class",
]
