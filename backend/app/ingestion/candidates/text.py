"""Text/Markdown/source-code candidate (#680)."""

from app.ingestion.candidates import Candidate

CANDIDATE = Candidate(
    "text",
    frozenset(
        {
            "text/plain",
            "text/markdown",
            "text/x-python",
            "text/x-rust",
            "text/javascript",
            "text/typescript",
            "text/x-c",
            "text/x-c++src",
            "text/x-java-source",
            "text/x-go",
            "text/x-sh",
            "text/x-sql",
        }
    ),
    frozenset({"text", "markdown"}),
)
