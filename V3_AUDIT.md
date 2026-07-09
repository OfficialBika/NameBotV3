# V3.1 audit notes

## Earlier V2/V3 issues addressed

1. Source scope errors could cause false negatives before global exact fallback.
2. Old records exposed only one UID/SHA while Adding Bot V3 can write alias arrays.
3. Photo lookup relied too heavily on pHash alone.
4. Video lookup used only three frame pHashes.
5. Snapshot startup could transfer complete Mongo documents and time out on large collections.
6. Render webhook startup previously performed expensive work before the HTTP port was bound.

## V3.1 dual-engine changes

- Added `LOOKUP_ENGINE_MODE=snapshot|sqlite`, default `snapshot`.
- Kept the RAM Snapshot engine and added Mongo projection/batch reads.
- Added read-only direct exact Mongo lookup backend.
- Added persistent SQLite WAL fingerprint index for similarity candidate search.
- Added background first-build and `updated_at` delta synchronization for SQLite mode.
- Added engine-aware `/refresh` and SQLite `/rebuildindex`.
- Added engine-aware `/status`, `/stats`, `/healthz`, and `/readyz` reporting.
- Kept result and miss TTL caches in RAM for both modes.
- Kept MongoDB as the source of truth; SQLite is rebuildable and disposable.

## SQLite mode safety model

MongoDB operations performed by the lookup engine are reads only. The SQLite database stores a local secondary lookup index. Removing the SQLite file cannot remove or modify MongoDB card data.
