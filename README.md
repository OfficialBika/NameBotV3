# Bika Lookup Bot V3

V3 of the Telegram card-media lookup bot, upgraded to consume the richer data written by the Adding Bot V3 while remaining backward compatible with V2 MongoDB documents.

## What changed

### V3 exact lookup layers

Lookup order:

1. Forward origin `(source_origin.chat_id, source_origin.message_id)`
2. `file_unique_id` plus `file_unique_ids[]` aliases
3. `sha256` plus `sha256_aliases[]`
4. `photo_fingerprint.pixel_sha256`
5. `video_fingerprint.video_signature`
6. Source-scoped photo multi-hash search
7. Source-scoped video multi-frame search
8. Controlled global similarity fallback

The lookup bot still reads old V2 fields (`file_unique_id`, `sha256`, `phash`, `frame_hashes`) so existing database records continue to work.

### Photo V3

Reads and compares:

- pHash
- large pHash
- dHash
- wHash
- colorHash
- canonical pixel SHA256
- crop-resistant hash metadata

Candidate search uses a pure-Python multi-index hash bucket system. No paid service, Redis, or FAISS dependency is required.

### Video V3

Reads and compares:

- legacy 20/50/80% pHash frames
- V3 5/15/30/50/70/85/95% frame fingerprints
- pHash + dHash per sample
- duration/fps/frame_count metadata
- exact `video_signature`

Duration buckets reduce the number of videos that need multi-frame comparison.

### Faster Adding Bot visibility

- Full Mongo snapshot at startup
- Incremental `updated_at` sync, default every 10 seconds
- Safety full rebuild, default every hour

### Render Web Service fix

In V2, webhook startup performed Mongo connection and full snapshot loading before aiohttp finished startup and bound the HTTP port. V3 binds `HOST:PORT` first, serves `/healthz` immediately, then performs Mongo/snapshot bootstrap. The webhook route returns 503 until ready so Telegram can retry updates.

Endpoints:

- `/` — liveness/status
- `/healthz` — Render liveness check, always responds after port bind
- `/readyz` — 200 only after Mongo + snapshot + webhook setup finish
- configured webhook path, default `/webhook`

## Environment branding

All main user-visible names can be controlled from env. Defaults remain **Bika**:

```env
BRAND_NAME=Bika
BOT_DISPLAY_NAME=Bika
POWERED_BY_NAME=Bika
POWERED_BY_USERNAME=@Official_Bika
STATUS_TITLE=Bika DATABASE STATUS
STATS_TITLE=Bika BOT STATS
SERVICE_NAME=Bika
```

## Render example

Set:

```env
MODE=webhook
USE_WEBHOOK=true
PUBLIC_URL=https://your-service.onrender.com
WEBHOOK_PATH=/webhook
WEBHOOK_SECRET=change-this-secret
```

Do not hard-code `PORT` on Render; Render injects it automatically.

Start command:

```bash
python main.py
```

Health check path:

```text
/healthz
```

A `render.yaml` is included.

## VPS

```bash
bash scripts/install_vps.sh
nano .env
source .venv/bin/activate
python main.py
```

PM2:

```bash
pm2 start main.py --name bikanamev3 --interpreter .venv/bin/python --time
pm2 save
```

## Important V3 defaults

```env
REQUIRE_LOOKUP_SCOPE=false
V3_GLOBAL_EXACT_FALLBACK=true
V3_GLOBAL_SIMILARITY_FALLBACK=true
SNAPSHOT_INCREMENTAL_SYNC_SECONDS=10
SNAPSHOT_FULL_REBUILD_SECONDS=3600
PHOTO_MULTI_SCORE_MIN=0.80
GLOBAL_PHOTO_MULTI_SCORE_MIN=0.88
```

## Admin commands

- `/gapprove`
- `/gunapprove`
- `/refresh`
- `/status`
- `/stats`
- `/free`
- `/unfree`
- `/freecheck`
