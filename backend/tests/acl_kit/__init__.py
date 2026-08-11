"""The ACL-mirror negative-test kit — INV-2 proofs per connector (F-CB-3, #454).

A **reusable, parametrized** suite proving the ADR-0019 §2/§3 deny-by-default
guarantees for *any* connector that declares ``map_acl``. The point is not "more
Drive tests": it is that the next managed connector **inherits** its INV-2 proof
instead of re-writing one. Adding a connector therefore means adding a
:class:`~tests.acl_kit.subject.AclSubject` fixture — never a new test file.

Layout::

    subject.py    the connector-agnostic contract (AclCase / AclSubject) +
                  the required-case vocabulary every subject must supply
    gdrive.py     the `gdrive` subject — the real app.connectors.gdrive.acl.map_acl
    synthetic.py  a deliberately alien fake connector, proving the kit assumes
                  nothing about Drive's raw vocabulary
    subjects.py   the SUBJECTS tuple + the registry-completeness gate
    engine.py     a faithful in-memory evaluator of the engine filter DSL, driven
                  through the REAL OpenSearchStore over httpx.MockTransport
    world.py      the seeded two-store world (SQLite + the fake engine) and the
                  independent visibility oracle both stores are checked against

Test modules (all parametrized over ``SUBJECTS``)::

    test_mapping.py          effective-read admit/deny fixtures + never-escalate
    test_stores.py           INV-1/INV-2 in Postgres AND OpenSearch, in parity
    test_retrieval_paths.py  every retrieval path + "no unfiltered builder"
    test_write_mode.py       the mandatory write-mode argument + §3 cascade
    test_token_hygiene.py    INV-6 audit + the no-token-material guarantees
"""
