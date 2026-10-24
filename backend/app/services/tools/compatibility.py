"""Legacy names share a read capability; explicit saved allowlists stay exact."""

FAMILIES = (
    frozenset({"search_passages", "search_text"}),
    frozenset({"find_documents", "search_documents", "list_documents"}),
    frozenset({"read_document", "get_document"}),
)
LEGACY = frozenset({"search_text", "search_documents", "list_documents", "get_document"})


def permitted_names(allowed: frozenset[str], blocked: set[str]) -> frozenset[str]:
    denied = set(blocked)
    for family in FAMILIES:
        if family & blocked:
            denied.update(family)
    return allowed - denied
