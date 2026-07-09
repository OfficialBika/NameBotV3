# V2 → V3 audit notes

## Root causes found in V2

1. `require_lookup_scope` could stop lookup before global exact UID/SHA checks.
2. A wrong source scope could produce a false negative because global fallback was disabled by default.
3. Snapshot refresh was a full periodic reload; new Adding Bot records could remain invisible until refresh.
4. Photo matching used only pHash after exact UID/SHA.
5. Video matching used only three pHash frames.
6. The webhook runner performed Mongo ping and full snapshot loading in aiohttp startup before `web.run_app` bound the HTTP port.
7. User/group tracking writes could occur on lookup paths too frequently.

## V3 fixes

- Alias-aware exact indexes: `file_unique_ids[]`, `sha256_aliases[]`.
- Exact `pixel_sha256`, `video_signature`, and origin mapping.
- Source-scoped lookup first, global exact fallback second.
- Multi-hash photo scoring and pure-Python multi-index candidate buckets.
- Seven-point video fingerprint matching with duration buckets.
- Incremental Mongo `updated_at` synchronization plus periodic full safety rebuild.
- Render webhook port binding before Mongo/snapshot bootstrap.
- `/healthz` liveness and `/readyz` readiness separation.
- Environment-controlled branding with `Bika` defaults.
- Throttled user/group touch writes and cached free-user checks.
