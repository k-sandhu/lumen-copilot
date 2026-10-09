"""The connector conformance kit (F-CB-5 #456, ADR-0019 §4).

A **library**, not a test module: every rule the SDK imposes on a connector is
one function here that raises ``AssertionError`` with an actionable message.
``tests/test_connector_conformance.py`` applies them to every connector the real
registry discovers, and — with synthetic offenders — proves each rule actually
bites.

Three pieces:

* :mod:`tests.conformance.kit` — the protocol/capability rules (surface,
  domain-types-only returns, typed errors, OAuth spec shape, cursor round-trip
  + expiry fallback, fail-closed/pure/never-escalating ``map_acl``);
* :mod:`tests.conformance.prohibitions` — the ADR-0019 §4 execution-context
  prohibitions, pinned **structurally** by an AST scan of the connector package
  (no vault, no DB, no mutable module state);
* :mod:`tests.conformance.harnesses` — one offline harness per connector: the
  fixtures the rules need (a source, a framework-shaped ``ConnectorRun`` over a
  ``MockTransport``, invalid configs, ACL cases, cursor scenarios). A registered
  connector without a harness fails the kit — that is how a new connector is
  forced to prove itself.
"""

from __future__ import annotations

__all__: list[str] = []
