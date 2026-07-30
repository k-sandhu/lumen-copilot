# 22. Group access model — group principals, and who a source is visible to

- **Status:** Accepted *(scope model decided by sponsor 2026-07-30; design accepted 2026-07-30)*
- **Date:** 2026-07-30
- **Builds on:** [spec 0004 §2.2](../specs/0004-security-and-domain-invariants.md) (grants, the INV-2 chokepoint, the `group`/`role` `revisit-at-implementation` follow-up), [ADR-0004](0004-architecture-boundaries-and-adapters.md) (one owning module per concern), [ADR-0009](0009-connector-framework-and-web-source.md) (the `sources` model), [ADR-0019](0019-connector-sdk-and-oauth.md) (mirrored ACLs and the freshness floor), [ADR-0021](0021-data-pack-connector.md) (data packs as sources — whose §5 this supersedes)

## Context

[ADR-0021 §5](0021-data-pack-connector.md) deferred tenant-wide pack installs because "it needs a source-sharing model that does not exist yet." **Sponsor decision (2026-07-30)** closes that: a source added by a **user** is visible to that user; a source added by an **admin** is visible to the **tenant**; and an admin can create **groups**, add users to them, and grant access to a group.

That is a general access primitive, not a data-pack feature — it changes who can retrieve *any* resource — so it gets its own ADR rather than a clause inside 0021.

The groundwork is already laid, deliberately:

- [spec 0004 §2.2](../specs/0004-security-and-domain-invariants.md) defines principals as `user | group | role` and lists `group`/`role` principals as an explicit `revisit-at-implementation` follow-up of [#18](https://github.com/k-sandhu/lumen-copilot/issues/18).
- `grants.principal_type` already admits `GROUP`, and the migration deliberately carries **no FK on `principal_id`** so group sharing lands "as a service change, not a migration" (`GrantPrincipalType` docstring). `grants_service.create_grant` currently rejects non-`USER` with a 422 `unsupported_principal_type` — one guard, one place.
- `retrieval/permissions.AllowSet` states in its own docstring that "a future revision (group/role membership) widens the principal set; no caller changes because callers consume this object, not the rule."

So this ADR is the reconfirmation that spec 0004 asks for when the follow-up is claimed, plus the two genuinely new decisions: **how tenant-wide is expressed**, and **which of the two permission branches carries source visibility**.

## Decision

1. **Groups are a tenant-scoped membership primitive.** New `groups` (`id, tenant_id, name, kind, created_by, created_at`) and `group_members` (`group_id, user_id, added_by, added_at`), both tenant-scoped with the same fail-closed RLS policy as `grants`. `name` is unique per tenant, case-insensitively. Groups are **not** roles: membership grants nothing by itself: it only makes a group grant apply.

2. **Group management is admin-only** (`Role.ADMIN`, INV-5 → 403 for a member), tenant-bound from the token and never from request input (spec 0004 §2.3). A group in another tenant is a 404, never a 403 (INV-1, existence non-disclosure). Every create/rename/delete/add-member/remove-member emits an audit event (INV-6).

3. **Tenant-wide is a system group with implicit membership.** Each tenant has exactly one `kind='system'` group ("All members") that cannot be renamed, deleted, or explicitly populated. Its membership is **derived, not materialized**: the membership resolver returns a user's explicit group ids *plus* their tenant's system group id.

   This is the alternative to materializing a row per user, and it is chosen because materialized membership has a real failure mode — a user created after the group must be back-filled, and a missed hook silently denies access — while a derived membership cannot drift. It also means "tenant-wide" needs no second mechanism: it is just a group grant, so there is **exactly one sharing path** to review and test.

4. **Grants widen to `GROUP` principals — the service change the schema anticipated.** `create_grant` accepts `principal_type=GROUP` after validating the group exists **in the caller's tenant** (cross-tenant → 404). `ROLE` stays rejected as `unsupported_principal_type`; nothing needs it yet, and admitting it unreviewed would widen INV-2 for free.

5. **The allow-set carries the requester's group ids, and the predicate honours them in both of its two homes.** `AllowSet` gains `group_ids`; the grant `EXISTS` in `retrieval/queries.py` matches `(principal_type='user' AND principal_id = me) OR (principal_type='group' AND principal_id IN my_group_ids)`, and the OpenSearch mirror in `search/filters.py` gains the identical terms. Those two functions are the **only** places the rule lives (stated in `_document_permitted`'s docstring) and they change in the same commit, with a test that asserts they agree.

6. **Source visibility rides the grant branch, not the mirrored-ACL branch.** `sources.visibility ∈ {private, tenant, group}` plus a nullable `sources.group_id`. A member may create only a `private` source; `tenant` and `group` are admin-only (T1, INV-5). The connector framework issues a **collection-level grant** to the corresponding principal — none for `private`, the system group for `tenant`, the named group for `group` — and collection grants already cascade to documents (spec 0004 §2.2), so **no new predicate branch is introduced**.

   The alternative — reusing `acl_enforced` / `acl_principals` — is rejected on purpose. That branch is for ACLs **mirrored from an external system**, and it is gated by a freshness floor: a mirror older than `CONNECTOR_ACL_MAX_AGE_HOURS` admits no one, including the owner ([ADR-0019](0019-connector-sdk-and-oauth.md)). A data pack's visibility is authored by Lumen and *cannot* drift from an upstream, so it must never expire — a tenant pack silently vanishing from retrieval hours after install would be a severe, quiet failure. Keeping locally-authored visibility on the grant branch preserves the freshness semantics as an honest statement about mirrored ACLs only.

7. **Revocation is immediate.** Group membership is resolved per request from the database, never cached in the token or the principal, so removing a user from a group — or deleting the group, which cascades its grants — takes effect on the next request. Access is therefore never carried by a still-valid access token.

8. **Deferred:** nested groups; `ROLE` grants; per-document (as opposed to per-collection) group grants; directory/SCIM-driven membership; and a self-service sharing UI for ordinary documents (spec 0004's other outstanding follow-up, which this ADR does not close).

## Consequences

- **Data-pack installs get their scope model** ([ADR-0021 §5](0021-data-pack-connector.md) is superseded by §6 above): user-added packs stay owner-scoped, admin-added packs land tenant-wide or on a named group, with no bespoke pack permission code.
- **INV-2 widens, so the negative tests carry the weight.** Required: a non-member is excluded from group-granted documents; a member is admitted; removing the member revokes on the next request; a group from another tenant is a 404; a non-admin creating a `tenant`/`group` source is a 403; and the SQL predicate and the engine mirror agree on the same fixture. A gap here is a blocking defect, not a follow-up.
- **One extra query per request** to resolve group membership. It is a small indexed read on `(tenant_id, user_id)`; caching it on the principal is deliberately *not* done, because a cached membership is exactly what would make revocation lag (§7). Revisit only with measurement.
- **The system group is visible but not editable.** It appears in the admin group list so an admin can see that "All members" is what a tenant-wide grant targets, with membership shown as derived rather than listed.
- **`grants.principal_id` still has no FK**, by design — it spans namespaces. The service remains the only integrity guard, which is why §4 validates the group's tenant before writing.
- **Delivery order** ([ADR-0008](0008-conflict-free-parallel-delivery.md) M2 shape): serialized prep (this ADR → migration → contract) then parallel build (groups service + predicate widening BE ‖ admin Groups panel FE), with the datapack connector ([ADR-0021](0021-data-pack-connector.md)) landing on top once visibility exists.
