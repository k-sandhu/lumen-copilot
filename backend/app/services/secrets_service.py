"""Secrets vault use-cases — store / list / delete / internal-get (issue #209).

The orchestration layer for the **encrypted, per-tenant secrets vault**: the
single chokepoint for third-party credentials the platform stores on a user's
behalf (MCP auth tokens/headers, a hosted web-search key, future connector
credentials). Credential handling is a single-chokepoint concern like auth/audit
— one owning place to get envelope-encryption, masking, and access rules right.

**The write-only guarantee (AC-1/AC-3).** Everything that leaves this service is
plaintext-free:

* :meth:`store_secret` encrypts the plaintext and returns a
  :class:`~app.domain.entities.SecretRef` — id + metadata + a masked ``hint``,
  never the value.
* :meth:`list_secrets` yields ``SecretRef``s (metadata + hint only).
* :meth:`delete_secret` returns a bool.
* :meth:`get_secret_plaintext` is the **only** method that returns a plaintext,
  and it is **internal**: it is called in-process by the MCP / search adapter at
  invoke time and is deliberately never wired to a router (the architecture test
  asserts no ``api/`` module imports this service or the cipher). The cipher
  itself (``app.core.crypto``) is imported *only* here.

**Authorization (deny-by-default, spec 0004 §2.2 + §2.3).** A secret is owned by
its ``owner_id`` within its ``tenant_id``. Only the **owner** — or a **tenant
admin** (``Role.ADMIN``) — may read, use, or delete it. Everything is
tenant-scoped (INV-1): a secret in another tenant, or one the caller neither owns
nor admins, is treated as **non-existent** — the operation raises
:class:`~app.core.errors.NotFoundError` (404), never 403 (existence
non-disclosure, spec 0004 §2.1). A denied read/use/delete emits a
``permission.denied`` audit event; the successful lifecycle emits
``secret.created`` / ``secret.accessed`` / ``secret.deleted`` (INV-6). Crucially,
``secret.accessed`` records **who/what** read a secret (the actor — an admin, or
the system on an adapter's behalf), never the value.

The ``tenant_id`` and the acting ``owner_id`` come from the resolved principal
(``auth/``), never from request input (spec 0004 §2.3). The caller owns the
transaction boundary; the audit write is flushed, not committed, so it commits
atomically with the secret change.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import EncryptedSecret, SecretsCipher
from app.core.errors import NotFoundError, ValidationError
from app.db.repositories import SecretRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, Role, Secret, SecretKind, SecretRef
from app.services.audit import AuditSink

# The masked tail length for a secret's ``hint`` (the UI shows "which secret"
# without the value). Only the last few characters, and only when the plaintext is
# comfortably longer than the reveal, so the hint never approaches the secret.
_HINT_REVEAL = 4
# Below this length nothing is revealed — a short secret would leak too much.
_HINT_MIN_LENGTH_TO_REVEAL = 8
_HINT_MASK = "****"


def _make_hint(plaintext: str) -> str:
    """Build a non-reversing masked hint for a plaintext (e.g. ``****abcd``).

    Reveals the last :data:`_HINT_REVEAL` characters only when the plaintext is
    long enough (:data:`_HINT_MIN_LENGTH_TO_REVEAL`) that the tail is a small
    fraction of it; otherwise reveals nothing (``****``). The hint is metadata for
    display, never a way back to the value.
    """
    if len(plaintext) >= _HINT_MIN_LENGTH_TO_REVEAL:
        return f"{_HINT_MASK}{plaintext[-_HINT_REVEAL:]}"
    return _HINT_MASK


def _to_ref(secret: Secret) -> SecretRef:
    """Project a full :class:`Secret` (with ciphertext) to the plaintext-free ref."""
    return SecretRef(
        id=secret.id,
        tenant_id=secret.tenant_id,
        owner_id=secret.owner_id,
        name=secret.name,
        kind=secret.kind,
        hint=secret.hint,
        created_at=secret.created_at,
        updated_at=secret.updated_at,
    )


class SecretsService:
    """Store / list / delete credentials + an internal-only plaintext get (issue #209).

    Constructed per-request with the session, the resolved ``tenant_id`` +
    ``owner_id`` + the caller's ``roles`` (all from the token, never request
    input), the envelope :class:`~app.core.crypto.SecretsCipher`, and the audit
    sink + correlation context. All ownership/tenancy enforcement lives here; the
    repository enforces tenancy (INV-1) and this service enforces the
    owner-or-admin rule. A foreign/unauthorized secret is reported as 404
    (existence non-disclosure), never 403.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        roles: tuple[Role, ...],
        cipher: SecretsCipher,
        audit: AuditSink,
        request_id: str,
        source_ip: str,
    ) -> None:
        self._session = session
        self._secrets = SecretRepository(session, tenant_id)
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._is_admin = Role.ADMIN in roles
        self._cipher = cipher
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip

    # --- internal: authorization -------------------------------------------

    def _may_access(self, secret: Secret) -> bool:
        """Whether the caller may read/use/delete ``secret`` (owner or tenant admin)."""
        return self._is_admin or secret.owner_id == self._owner_id

    async def _load_owned_or_404(
        self, secret_id: UUID, *, actor: AuditActor
    ) -> Secret:
        """Load a secret the caller may access, or raise 404 (+ a denied audit).

        The deny-by-default gate: the secret must exist in this tenant **and** be
        owned by the caller (or the caller must be a tenant admin). A secret that
        is missing, in another tenant, or owned by someone else (and the caller is
        not an admin) raises :class:`NotFoundError` (404, existence
        non-disclosure) after a ``permission.denied`` audit event. A non-owner is
        indistinguishable from a non-existent id — both 404 — so the vault never
        reveals that a foreign/unauthorized secret exists.
        """
        secret = await self._secrets.get(secret_id)
        if secret is None or not self._may_access(secret):
            await self._audit.emit(
                action=AuditAction.PERMISSION_DENIED,
                actor=actor,
                resource_type="secret",
                resource_id=str(secret_id),
                outcome=AuditOutcome.DENIED,
                request_id=self._request_id,
                source_ip=self._source_ip,
                metadata={"reason": "not_owner_or_admin"},
            )
            raise NotFoundError("Secret not found.")
        return secret

    # --- use-cases ----------------------------------------------------------

    async def store_secret(
        self,
        *,
        name: str,
        kind: SecretKind,
        plaintext: str,
    ) -> SecretRef:
        """Encrypt + persist a credential; return its plaintext-free ref (AC-1).

        Steps (fail-closed):

        1. **Validate** — ``name`` is non-blank and ``plaintext`` is non-empty; a
           blank/empty value is a 422 :class:`ValidationError` (INV-8), nothing is
           persisted. (An empty secret is meaningless and would make a misleading
           hint.)
        2. **Encrypt** — the plaintext goes through the envelope cipher
           (``app.core.crypto``, AES-256-GCM) → ciphertext + nonce + key_version.
           The plaintext is not stored, logged, or returned.
        3. **Persist** — upsert on ``(tenant, owner, name)``: a new name inserts,
           an existing name **rotates the value in place** (the handle stays
           stable). Stored with a masked ``hint`` (never the value).
        4. **Audit** — emit ``secret.created`` (INV-6) with metadata (name, kind,
           hint) but **never** the plaintext.

        Returns a :class:`SecretRef` (id + metadata + hint) — the only shape any
        HTTP surface may serialize (no value, no ciphertext).

        Raises:
            ValidationError: ``name`` is blank or ``plaintext`` is empty (422).
        """
        if not name.strip():
            raise ValidationError("Secret name must not be blank.", code="invalid_secret_name")
        if not plaintext:
            raise ValidationError("Secret value must not be empty.", code="empty_secret_value")

        envelope: EncryptedSecret = self._cipher.encrypt(plaintext)
        secret = await self._secrets.upsert(
            owner_id=self._owner_id,
            name=name,
            kind=kind,
            ciphertext=envelope.ciphertext,
            nonce=envelope.nonce,
            key_version=envelope.key_version,
            hint=_make_hint(plaintext),
            created_by=self._owner_id,
        )
        await self._audit.emit(
            action=AuditAction.SECRET_CREATED,
            actor=AuditActor.user(self._owner_id),
            resource_type="secret",
            resource_id=str(secret.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"name": secret.name, "kind": secret.kind.value, "hint": secret.hint},
        )
        return _to_ref(secret)

    async def list_secrets(self) -> list[SecretRef]:
        """List the caller's secrets as plaintext-free refs (metadata + hint only).

        Returns a :class:`SecretRef` per secret the caller owns (oldest first) —
        id, name, kind, and the masked hint, **never** the value or ciphertext
        (AC-1). A tenant admin listing sees only their own here; per-owner admin
        listing is a follow-up if a management UI needs it (kept minimal + in
        scope). No audit event (a metadata read of one's own secrets).
        """
        secrets = await self._secrets.list_for_owner(self._owner_id)
        return [_to_ref(s) for s in secrets]

    async def delete_secret(self, secret_id: UUID) -> None:
        """Delete a secret the caller owns (or admins), or 404.

        Authorization-gated (owner-or-admin, else 404 + ``permission.denied``).
        Deletes the row and emits ``secret.deleted`` (INV-6). Idempotent from the
        caller's view: a missing/foreign/non-owned id is a 404 (never revealing it
        existed), a real owned one is removed.

        Raises:
            NotFoundError: the secret is missing, in another tenant, or not owned
                by the (non-admin) caller — reported as 404.
        """
        secret = await self._load_owned_or_404(
            secret_id, actor=AuditActor.user(self._owner_id)
        )
        await self._secrets.delete(secret.id)
        await self._audit.emit(
            action=AuditAction.SECRET_DELETED,
            actor=AuditActor.user(self._owner_id),
            resource_type="secret",
            resource_id=str(secret.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"name": secret.name, "kind": secret.kind.value},
        )

    async def get_secret_plaintext(
        self,
        secret_id: UUID,
        *,
        accessor: AuditActor | None = None,
    ) -> str:
        """**Internal only** — decrypt + return a secret's plaintext (never via a router).

        The one method that yields a plaintext. It is called **in-process** by the
        MCP / search adapter at invoke time to obtain the credential it must send
        upstream; it is deliberately never exposed through any HTTP surface (the
        architecture test asserts no ``api/`` module imports this service). Steps
        (fail-closed):

        1. **Authorize** — the secret must exist in this tenant and be
           owner-or-admin accessible, else 404 + ``permission.denied``
           (``_load_owned_or_404``). Cross-tenant / non-owned is 404 (INV-1/INV-2).
        2. **Decrypt** — the envelope is decrypted with the configured key
           (``app.core.crypto``). A wrong/rotated key or tampered ciphertext
           **fails closed** — the cipher raises
           :class:`~app.core.crypto.SecretDecryptionError` and no plaintext is
           returned (AC-2). The error is left to propagate (a 500 to any accidental
           caller) rather than swallowed.
        3. **Audit** — emit ``secret.accessed`` (AC-5/INV-6) recording **who/what**
           read it (the ``accessor`` — the adapter/system, or the acting user by
           default), **never** the value.

        Args:
            secret_id: The secret to read.
            accessor: Who is reading it — pass the adapter/system actor (e.g.
                :meth:`AuditActor.system`) so the audit trail names the reader.
                Defaults to the acting user.

        Returns:
            The decrypted plaintext credential.

        Raises:
            NotFoundError: the secret is missing, in another tenant, or not owned
                by the (non-admin) caller — 404.
            SecretDecryptionError: the ciphertext did not authenticate under the
                configured key (fail-closed — no plaintext returned).
        """
        actor = accessor if accessor is not None else AuditActor.user(self._owner_id)
        secret = await self._load_owned_or_404(secret_id, actor=actor)
        # Decrypt only after authorization. A wrong key raises (fail-closed) — the
        # `secret.accessed` audit below is reached only on a successful decrypt, so
        # a failed read is not recorded as an access (it is a crypto failure).
        plaintext = self._cipher.decrypt(
            EncryptedSecret(
                ciphertext=secret.ciphertext,
                nonce=secret.nonce,
                key_version=secret.key_version,
            )
        )
        await self._audit.emit(
            action=AuditAction.SECRET_ACCESSED,
            actor=actor,
            resource_type="secret",
            resource_id=str(secret.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"name": secret.name, "kind": secret.kind.value},
        )
        return plaintext


def build_secrets_service(
    session: AsyncSession,
    *,
    settings: Settings,
    tenant_id: UUID,
    owner_id: UUID,
    roles: tuple[Role, ...],
    audit: AuditSink,
    request_id: str,
    source_ip: str,
) -> SecretsService:
    """Assemble a :class:`SecretsService` from settings (the production wiring).

    The envelope :class:`~app.core.crypto.SecretsCipher` is built here from the
    configured master key (``core/config`` is the single env reader). This factory
    keeps the cipher import confined to the secrets service (the ADR-0004
    chokepoint the architecture test enforces): a caller that needs a vault-backed
    service — e.g. the MCP registration service — asks for one here rather than
    importing the cipher itself, so no ``api/`` module ever touches plaintext.
    """
    return SecretsService(
        session,
        tenant_id=tenant_id,
        owner_id=owner_id,
        roles=roles,
        cipher=SecretsCipher(settings.secrets_encryption_key),
        audit=audit,
        request_id=request_id,
        source_ip=source_ip,
    )


__all__ = ["SecretsService", "build_secrets_service"]
