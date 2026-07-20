"""Google Drive connector package (F-CB-2 #453, ADR-0019 §5).

Auto-discovered by the registry via the module-level ``CONNECTOR`` sentinel
(ADR-0008 §3) — no registry edit. The single module boundary that talks to
Google (ADR-0004): Drive REST v3 + Google's OAuth endpoints, plain httpx over
the framework's guarded clients.
"""

from app.connectors.gdrive.connector import GdriveConnector

CONNECTOR = GdriveConnector()

__all__ = ["CONNECTOR", "GdriveConnector"]
