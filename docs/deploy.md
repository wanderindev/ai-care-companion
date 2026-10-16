# Deploy & infrastructure

n16chat runs on the shared `pic` droplet (`<user>@<droplet-ip>`) next to
panama-in-context, feliu-dev and atelier-loyalty-app. Apart from the domain
registration, nothing here costs extra: the droplet, the managed Postgres
cluster and the Spaces subscription already exist.

## Inventory

| Piece | Value | Status |
|---|---|---|
| Domain | `app.example.com` (registrar GoDaddy, DNS on DigitalOcean) | done |
| DNS | `A @` and `A www` → <droplet-ip>; `CAA @ 0 issue "letsencrypt.org"` | done 2026-10-08 |
| Email DNS (Resend) | CNAMEs `send` and `rsend` → `*.forge.rmta.net`, DKIM TXT `resend._domainkey`, DMARC TXT `_dmarc` | verified 2026-10-08 |
| Email sending | Resend over SMTP (`smtp.resend.com:465`, user `resend`, password = `RESEND_API_KEY`), sender `EMAIL_FROM` (`n16chat <acceso@app.example.com>`). Without `RESEND_API_KEY` production sends **no** magic links (logs a warning), so nobody can sign in | done 2026-10-08 (sending key in `~/n16chat/.env`) |
| TLS | Let's Encrypt cert name `app.example.com` (app.example.com + www) in `/opt/letsencrypt`, issued by `pic-certbot` (webroot), renewed by its loop | done 2026-10-08 |
| Database | DB `n16chat`, owner user `n16chat`, on cluster `<managed-cluster>`; `unaccent` extension installed. From the droplet use the **private** host `<managed-postgres-host>:25060`, `sslmode=require` | done 2026-10-08 |
| DB pool | cluster `max_connections` = 50, shared by 4 apps. Keep `pool_size=2, max_overflow=2` per process (backend 2 workers + worker 1) | budget |
| Spaces | bucket `n16chat` (nyc3, **private**) + access key limited to that bucket (read/write/delete). **Versioning: enabled** 2026-10-08 (the console has no toggle and the scoped key cannot change bucket settings: it was set with a temporary full-access Spaces key, deleted right after, via `aws s3api put-bucket-versioning --bucket n16chat --versioning-configuration Status=Enabled --endpoint-url https://nyc3.digitaloceanspaces.com`; check with `get-bucket-versioning`). Overwritten/deleted objects keep noncurrent versions, which count toward storage. Objects: `<kind>/<yyyy>/<sha256>.<ext>`, `kind` = `images`/`documents`/`audio`; the browser gets presigned GET URLs (≤ 10 min) for `https://nyc3.digitaloceanspaces.com/n16chat/…` — no CORS rule needed for `<img>`/`<audio>`/download links | done 2026-10-08 |
| Images | `ghcr.io/wanderindev/n16chat-backend` (backend + worker), `ghcr.io/wanderindev/n16chat-frontend` — private GHCR packages linked to this repo, tagged `latest` + git SHA, built by `.github/workflows/deploy.yml` (see [Deploy pipeline](#deploy-pipeline)) | done (ticket #2) |
| Compose dir | `~/n16chat` on the droplet: `docker-compose.prod.yml` + `.env` (chmod 600), containers on the external `shared-web` network with fixed `container_name`s `n16chat-backend`, `n16chat-worker`, `n16chat-frontend` (the vhost and Watchtower depend on these names). Compose **service** names are the same `n16chat-*` strings: panama-in-context and feliu-dev already have services called `backend`/`frontend` on `shared-web`, so plain names would resolve to several containers | done 2026-10-08 |
| Edge vhost | `~/pic/vhosts/app.example.com.conf`, bind-mounted into `pic-frontend` (backup of the edited compose: `~/pic/docker-compose.prod.yml.bak-n16chat`) | done 2026-10-08 |
| Auto-deploy | add `n16chat-backend n16chat-worker n16chat-frontend` to `pic-watchtower`'s `command:` in `~/pic/docker-compose.prod.yml` (backup: `.bak-watchtower`) | done 2026-10-08 |

## Deploy pipeline

```
merge to main ─▶ CI (ci.yml) green ─▶ Deploy (deploy.yml, workflow_run)
   ─▶ build docker/{backend,frontend}/Dockerfile.prod (only the side that changed)
   ─▶ push ghcr.io/wanderindev/n16chat-{backend,frontend}:{latest,<sha>}
   ─▶ pic-watchtower pulls :latest and recreates the n16chat-* containers
```

- **Trigger**: `workflow_run` on CI for `main`; every job checks
  `conclusion == 'success'`, so a red main never ships. `workflow_dispatch`
  rebuilds both images. The workflow never touches the droplet.
- **Changed services**: `backend/` or `docker/backend/` → backend image (which
  the worker also runs); `frontend/` or `docker/frontend/` → frontend image.
  Doc-only merges build nothing.
- **Auth**: pushes with the workflow's `GITHUB_TOKEN` (`packages: write`); no
  repo secret needed. The Dockerfiles' `org.opencontainers.image.source` label
  links each package to this repo, so the packages stay private and the
  workflow can manage them.
- **Retention**: the `Prune old image versions` job keeps the newest 5
  versions of each package (`latest` + 4 SHA rollback targets) and deletes the
  rest. If it ever fails with a permission error, give the `n16chat`
  repository the **Admin** role in each package's *Manage Actions access*
  settings; the images are already pushed by then.
- **Images**: backend = `python:3.12-slim`, `uvicorn app.main:app --workers 2
  --proxy-headers`; worker = same image, `python -m app.worker` (polls the `jobs`
  table, migration 0005; until that migration is applied it only logs
  `worker poll failed: ProgrammingError` every 30 s); frontend =
  static Vite build served by `nginx` on :80 (SPA fallback to `index.html`,
  `immutable` caching for `/assets/*`, `no-cache` for `index.html`, `sw.js`,
  `registerSW.js`, `workbox-*.js`, `manifest.webmanifest`; `/healthz` for the
  healthcheck).
- **Memory** (droplet is 4 GB, shared): backend 384 MB, worker 256 MB,
  frontend 64 MB (`mem_limit` in `docker-compose.prod.yml`). Printable PDFs
  (`GET /patients/{id}/meds/print.pdf`) render in a short-lived WeasyPrint
  child process (~0.5 s, peak ~100 MB, at most one per uvicorn worker), so
  the two API workers stay at ~100 MB each instead of keeping WeasyPrint
  resident (+40–70 MB each, measured).
- **PDF system libraries**: the backend image installs `libpango-1.0-0`,
  `libpangoft2-1.0-0`, `libharfbuzz-subset0` and `fonts-dejavu-core` for
  WeasyPrint (image ≈ 369 MB → 459 MB); the dev image (`backend/Dockerfile`)
  and the CI backend job install the same packages.
- **Disk for WhatsApp imports**: the browser uploads an export zip in
  `WHATSAPP_CHUNK_MB` chunks (default 8 MB, far below the edge's 200 MB body
  limit) that go straight into an S3 multipart upload on Spaces, so the API
  holds one chunk in memory and nothing on disk. The worker then copies the
  whole zip (several GB for a full export with media) into its container's
  `/tmp` (the writable layer, i.e. droplet disk) plus one media file at a
  time, and deletes them when the job ends: keep free disk above the largest
  export. Abandoned uploads are aborted from the UI ("Cancelar subida"); one
  left half-done otherwise keeps its parts in the bucket.
- **Secrets in logs**: uvicorn's access log redacts
  `/api/v1/calendar/<token>.ics` and any `…token=` query parameter
  (`backend/app/core/logging.py`); `n16chat-frontend`'s nginx logs paths
  without query strings or referers. The edge `pic-frontend` logs full request
  lines (path + query string) for every site, which is why the vhost below turns access
  logging off for the calendar feed; magic-link URLs carry their token in the
  URL fragment (`/acceso#token=…`), which never reaches a server log — the SPA
  POSTs it to `/api/v1/auth/verify` (a GET never consumes a token).
- **Client IP trust model**: every `location` that proxies to the backend
  **must** keep `proxy_set_header X-Real-IP $remote_addr;` (it overwrites any
  client-sent value). The backend's auth rate limits read the client IP from
  `X-Real-IP` only (`backend/app/api/deps.py` `client_ip`, validated with
  `ipaddress`). `request.client.host` is not trustworthy behind the edge:
  uvicorn runs with `--proxy-headers --forwarded-allow-ips "*"` and takes the
  *leftmost* `X-Forwarded-For` entry, which the browser chooses, while the
  edge's `$proxy_add_x_forwarded_for` only appends the real IP.
- **Migrations** are never run on container start. After a merge that adds a
  file under `backend/alembic/versions/`, once Watchtower has pulled the new
  image: `cd ~/n16chat && docker compose -f docker-compose.prod.yml run --rm n16chat-backend alembic upgrade head`.
- **Rollback**: retag an older SHA as `latest` from a machine logged in to
  GHCR with `write:packages`:
  `docker pull ghcr.io/wanderindev/n16chat-backend:<sha> && docker tag ghcr.io/wanderindev/n16chat-backend:<sha> ghcr.io/wanderindev/n16chat-backend:latest && docker push ghcr.io/wanderindev/n16chat-backend:latest`
  (Watchtower picks it up). Migrations are not undone by a rollback.
- **New `.env` keys**: Watchtower recreates containers with the env they were
  first created with; a new `.env` key only takes effect after
  `cd ~/n16chat && docker compose -f docker-compose.prod.yml up -d`
  (operator). Every PR that needs a new key names it.

## Operator setup steps (done 2026-10-08)

All done. Kept as the record of how production was set up and for
rebuilding it. Ticket #2 (merged) added `.env.example`,
`docker-compose.prod.yml` and the images; steps 2–6 use them.

1. **Spaces**: in the DO console create bucket `n16chat` in nyc3 (File Listing:
   Restricted), enable versioning (API only, see Inventory), then create an access key with access
   limited to `n16chat` (read/write/delete). DO shows the secret **once** —
   keep it in a password manager until step 2.
2. **`.env`**: `mkdir -p ~/n16chat && chmod 700 ~/n16chat`, copy
   `.env.example` to `~/n16chat/.env`, `chmod 600`, then fill it in:
   - `DATABASE_URL` with user `n16chat` (password:
     `doctl databases user get d699bab0-c322-4b58-b90b-98bae0632b30 n16chat`)
     and the private host above, `sslmode=require`. **Percent-encode the
     password** if it contains URL-special characters (`@ : / ? # [ ] % &`);
     `.env.example` shows a one-liner for it;
   - `DO_SPACES_KEY`, `DO_SPACES_SECRET`, `DO_SPACES_BUCKET=n16chat`,
     `DO_SPACES_ENDPOINT=https://nyc3.digitaloceanspaces.com`,
     `DO_SPACES_REGION=nyc3`;
   - `RESEND_API_KEY` (a *sending access* key for `app.example.com`, created in
     Resend once the domain is verified) and `APP_BASE_URL=https://app.example.com`;
   - the rest per `.env.example`.
3. **Compose**: `scp docker-compose.prod.yml` to `~/n16chat`, then in that dir
   run the migrations **before** starting the services:
   `docker compose -f docker-compose.prod.yml run --rm n16chat-backend alembic upgrade head`,
   then `docker compose -f docker-compose.prod.yml up -d`.
   Allowlist the first (admin) user — the users table starts empty and there
   is no sign-up:
   `docker compose -f docker-compose.prod.yml run --rm n16chat-backend python -m scripts.users add <email> --name "<Nombre>" --admin`
   (then `add` each family member without `--admin`; `list` / `deactivate
   <email>` manage them).
   Migration 0003 seeds the two patients (`papa`, `mama`) with display names
   only. Fill in their private fields (full name, ID number/CIP, birth date,
   diagnoses, notes) in the app, or with
   `… run --rm n16chat-backend python -m scripts.patients set papa --full-name - --by <email>`
   (`-` reads the value from stdin so it stays out of shell history; `list`
   shows which fields are filled, never their values). Never put these in git.
   The one-time bitácora import (needs `ANTHROPIC_API_KEY`) is in
   `docs/import.md`. The worker's lab PDF extraction (`lab_extract` jobs,
   queued after each WhatsApp import) also needs `ANTHROPIC_API_KEY` in the
   `.env` the worker reads; without it those jobs fail (nothing is recorded,
   so they can be re-queued once the key is set). Lab portal links found in
   the chat are only listed in the app until `LAB_LINK_HOSTS` names the lab
   hosts the worker may download from (then run
   `… run --rm n16chat-backend python -m scripts.lab_links scan` to record
   links in already-imported messages and queue the untried ones).
   Voice notes of `daily`/`backfill` imports are transcribed by the worker
   (`transcribe_audio` jobs) with OpenAI, which needs `OPENAI_API_KEY` in the
   same `.env` (optional: `TRANSCRIPTION_MODEL`, default `gpt-transcribe`;
   `TRANSCRIPTION_MAX_MB`, default 25). Without it those jobs fail with a
   generic message and record nothing.
   Images and PDFs of `daily`/`backfill` imports are classified by the worker
   (`classify_media` jobs, `ANTHROPIC_API_KEY`; optional `CLASSIFICATION_MODEL`,
   default `claude-haiku-5-5`); only those it calls lab reports go on to
   `lab_extract`. `labs_only` PDFs still go straight to `lab_extract`.
   A key added to `.env` later needs
   `cd ~/n16chat && docker compose -f docker-compose.prod.yml up -d`:
   Watchtower recreates containers with the env they were first created with.
   Watchtower already holds GHCR credentials in `~/.docker/config.json`
   (shared with loyalty-app) — never `docker logout` on the droplet.
4. **Vhost** (only once the containers are running):
   1. Write `~/pic/vhosts/app.example.com.conf` (template below).
   2. **Validate before touching the ingress** — the running `pic-frontend`
      serves every site:
      `docker cp ~/pic/vhosts/app.example.com.conf pic-frontend:/etc/nginx/conf.d/zz-n16chat-test.conf && docker exec pic-frontend nginx -t; docker exec pic-frontend rm /etc/nginx/conf.d/zz-n16chat-test.conf`.
      Do not continue unless `nginx -t` reports success.
   3. Add `- ./vhosts/app.example.com.conf:/etc/nginx/conf.d/app.example.com.conf:ro`
      to the `frontend` service volumes in `~/pic/docker-compose.prod.yml`
      (the existing adgseguros/calcfina mounts are droplet-only edits — they
      are not in the panama-in-context repo).
   4. A new mount needs the container recreated:
      `cd ~/pic && docker compose -f docker-compose.prod.yml up -d --no-deps frontend`
      (a few seconds of downtime for **every** site; `--no-deps` keeps
      compose from touching `pic-backend`, which `frontend` depends on).
   5. Check `https://app.example.com`, `https://panamaincontext.com`,
      `https://feliu.dev`, `https://loyalty.atelierm.store`.
      If anything is down, remove the mount line and run
      `cd ~/pic && docker compose -f docker-compose.prod.yml up -d --no-deps frontend` again.
5. **Watchtower**: add `n16chat-backend n16chat-worker n16chat-frontend` to
   `pic-watchtower`'s `command:` in `~/pic/docker-compose.prod.yml` and
   `cd ~/pic && docker compose -f docker-compose.prod.yml up -d --no-deps watchtower`.
   Then confirm Watchtower can pull the new packages with the droplet's GHCR
   credentials (a classic PAT with `read:packages`, shared with loyalty-app):
   after the next push, `docker logs pic-watchtower` must show the n16chat
   images being checked/updated, not `unauthorized` / `denied`. If it fails,
   check that the PAT's owner can read the private `n16chat-*` packages.
6. Flip `CLAUDE.md` `## Shipping` (created by ticket #1) to "deploy live" with
   the verification steps (done).

### Vhost template

Mirrors the existing vhosts (IPv4 listeners only). Uses Docker's embedded DNS
with variables so nginx still starts (and every other site stays up) if the
n16chat containers are stopped or missing — this site alone returns 502.

```nginx
# app.example.com — SPA (n16chat-frontend) + API (n16chat-backend) on shared-web.
server {
    listen 80;
    server_name app.example.com www.app.example.com;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://app.example.com$request_uri; }
}

server {
    listen 443 ssl;
    server_name www.app.example.com;
    ssl_certificate     /etc/letsencrypt/live/app.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/app.example.com/privkey.pem;
    return 301 https://app.example.com$request_uri;
}

server {
    listen 443 ssl;
    server_name app.example.com;
    ssl_certificate     /etc/letsencrypt/live/app.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/app.example.com/privkey.pem;

    resolver 127.0.0.11 valid=30s ipv6=off;
    set $n16_api http://n16chat-backend:8000;
    set $n16_web http://n16chat-frontend:80;

    client_max_body_size 200m;   # WhatsApp export zips

    # Secret per-user calendar feed token is in the path: keep it out of logs.
    location ~ ^/api/v1/calendar/ {
        access_log off;
        proxy_pass $n16_api;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/ {
        proxy_pass $n16_api;
        proxy_http_version 1.1;
        proxy_set_header Connection        "";
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;           # chat SSE streaming
        proxy_request_buffering off;   # large zip uploads stream to the backend
        proxy_connect_timeout 10s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }

    location / {
        proxy_pass $n16_web;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
