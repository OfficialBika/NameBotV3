# Bika Lookup Bot V3.1

Telegram card-media lookup bot with two selectable lookup engines. It consumes the richer fields written by Adding Bot V3 while remaining backward compatible with V2 MongoDB records.

## Lookup engine modes

Select with:

```env
LOOKUP_ENGINE_MODE=snapshot
```

or:

```env
LOOKUP_ENGINE_MODE=sqlite
```

When the variable is omitted, `snapshot` is used for backward compatibility.

### Snapshot mode

The existing V3 RAM engine:

- MongoDB projection-only full load at startup
- exact origin, UID aliases, SHA aliases, pixel SHA and video signature indexes in RAM
- photo hash chunk candidate index in RAM
- video duration buckets in RAM
- incremental `updated_at` sync
- periodic safety full rebuild

Snapshot mode is fastest when enough RAM is available.

### SQLite hybrid mode

Low-RAM persistent-index engine:

- MongoDB remains source of truth and is read-only from the lookup bot
- Mongo indexed direct lookup for origin, UID aliases, SHA aliases, pixel SHA and video signature
- local SQLite WAL database for photo/video similarity candidate search
- compact serialized lookup payloads in SQLite for candidate verification
- small in-process result/miss caches remain in RAM
- first SQLite build runs in background; exact Mongo lookup is usable immediately
- later syncs read only documents with `updated_at` newer than the saved watermark

Default local index path:

```text
data/fingerprint_index.db
```

The SQLite file is disposable. Deleting it does not delete MongoDB data; it can be rebuilt from MongoDB.

## Lookup order

1. Forward origin exact match
2. `file_unique_id` + `file_unique_ids[]`
3. file byte SHA256 + `sha256_aliases[]`
4. `photo_fingerprint.pixel_sha256`
5. `video_fingerprint.video_signature`
6. source-scoped photo multi-hash similarity
7. source-scoped video multi-frame similarity
8. controlled global similarity fallback

## Photo V3

- pHash
- large pHash
- dHash
- wHash
- colorHash
- canonical pixel SHA256
- crop-resistant hash metadata

## Video V3

- legacy 20/50/80% frame pHash
- V3 5/15/30/50/70/85/95% samples
- pHash + dHash per sample
- duration/fps/frame count metadata
- exact sampled `video_signature`
- duration-bucket candidate reduction

## Main environment variables

```env
# Engine: snapshot or sqlite
LOOKUP_ENGINE_MODE=snapshot

# Snapshot mode
SNAPSHOT_STARTUP_LOAD=true
SNAPSHOT_BACKGROUND_REFRESH=true
SNAPSHOT_INCREMENTAL_SYNC_SECONDS=10
SNAPSHOT_FULL_REBUILD_SECONDS=3600
SNAPSHOT_BATCH_SIZE=500

# SQLite mode
SQLITE_INDEX_PATH=data/fingerprint_index.db
SQLITE_SYNC_SECONDS=10
SQLITE_BUILD_ON_START=true
SQLITE_REBUILD_ON_START=false
SQLITE_BATCH_SIZE=500
SQLITE_BUSY_TIMEOUT_MS=5000
SQLITE_FULL_REBUILD_SECONDS=0

# Direct exact Mongo query guard
MONGO_EXACT_QUERY_TIMEOUT_MS=5000
```

For a VPS where the full snapshot is timing out or using too much RAM, use:

```env
LOOKUP_ENGINE_MODE=sqlite
```

To roll back instantly:

```env
LOOKUP_ENGINE_MODE=snapshot
```

then restart the process.

## Render webhook behavior

The HTTP port is bound before Mongo/backend bootstrap. `/healthz` responds immediately after port bind. The webhook endpoint returns 503 until core bootstrap has completed, allowing Telegram to retry.

Endpoints:

- `/` — liveness/status
- `/healthz` — liveness
- `/readyz` — readiness
- configured webhook route, default `/webhook`

Render example:

```env
MODE=webhook
USE_WEBHOOK=true
PUBLIC_URL=https://your-service.onrender.com
WEBHOOK_PATH=/webhook
WEBHOOK_SECRET=change-this-secret
```

Do not hard-code `PORT` on Render.

## Branding

All main user-visible names are configurable. Defaults remain Bika.

```env
BRAND_NAME=Bika
BOT_DISPLAY_NAME=Bika
POWERED_BY_NAME=Bika
POWERED_BY_USERNAME=@Official_Bika
STATUS_TITLE=Bika DATABASE STATUS
STATS_TITLE=Bika BOT STATS
SERVICE_NAME=Bika
```

## VPS

```bash
git clone https://github.com/OfficialBika/NameBotV3.git
cd NameBotV3
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
nano .env
python main.py
```

PM2:

```bash
pm2 start main.py --name bikanamev3 --interpreter .venv/bin/python --time
pm2 save
```

After changing engine mode:

```bash
pm2 restart bikanamev3
```

## Admin commands

- `/gapprove`
- `/gunapprove`
- `/refresh` — Snapshot full refresh or SQLite delta sync depending on engine mode
- `/rebuildindex` — SQLite full index rebuild, owner/sudo only
- `/status`
- `/stats`
- `/free`
- `/unfree`
- `/freecheck`
