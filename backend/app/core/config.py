import re
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Chrome extension ids are 32 letters a–p.
_EXTENSION_ORIGIN = re.compile(r"chrome-extension://[a-p]{32}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Unknown env vars must NOT crash the app: a new key in the prod .env
        # ahead of the code that reads it would otherwise crash-loop the
        # backend on startup (lesson learned in panama-in-context).
        extra="ignore",
    )

    # Application
    app_name: str = "n16chat API"
    version: str = "0.1.0"
    debug: bool = False
    environment: str = "production"

    # Only the ``app`` logger namespace is raised to this level; third-party
    # libraries stay at WARNING. Logs never contain medical content.
    log_level: str = "INFO"

    # Database (default matches the local docker-compose `db` service)
    database_url: str = "postgresql://n16chat:localdev@localhost:5434/n16chat"

    # Public origin of the SPA (and of /api, same origin). Magic links point to
    # f"{app_base_url}/acceso#token=...".
    app_base_url: str = "http://localhost:5180"

    # Auth sessions (docs/DESIGN.md §2). The cookie is Secure unless this is
    # set to false explicitly or the environment is development/test.
    session_cookie_name: str = "n16_session"
    session_cookie_secure: bool | None = None
    session_days: int = 30

    # Magic-link and calendar-invite email. Sent over plain SMTP so the code stays provider
    # agnostic; the defaults target Resend (SMTPS on 465, user "resend",
    # password = the API key). In development/test the link is logged instead
    # of sent; in production without RESEND_API_KEY nothing is sent (warning).
    email_from: str = "n16chat <acceso@app.example.com>"
    resend_api_key: str = ""
    smtp_host: str = "smtp.resend.com"
    smtp_port: int = 465
    smtp_user: str = "resend"

    # DO Spaces (S3-compatible) for attachments; docs/DESIGN.md §4 "Files".
    # Without DO_SPACES_KEY/SECRET uploads answer 503 (the app still starts).
    # Local dev points these at MinIO (docker-compose.yml). The browser loads
    # files from presigned URLs signed for DO_SPACES_PUBLIC_ENDPOINT (defaults
    # to DO_SPACES_ENDPOINT; only differs when the backend reaches the store
    # through another host, e.g. `minio:9000` vs `localhost:9010`).
    do_spaces_key: str = ""
    do_spaces_secret: str = ""
    do_spaces_bucket: str = "n16chat"
    do_spaces_endpoint: str = "https://nyc3.digitaloceanspaces.com"
    do_spaces_region: str = "nyc3"
    do_spaces_public_endpoint: str = ""

    # Attachment limits. Presigned GET URLs live at most 10 minutes (DESIGN §4);
    # larger values are capped. The edge nginx allows bodies up to 200 MB.
    attachment_max_mb: int = 25
    attachment_url_ttl_seconds: int = 300

    # WhatsApp export imports (DESIGN §5.6, app/services/whatsapp_import). The
    # zip is uploaded in chunks of WHATSAPP_CHUNK_MB (clamped to 5–64: S3
    # multipart parts must be ≥ 5 MB, and each request stays far below the
    # edge's 200 MB body limit) up to WHATSAPP_IMPORT_MAX_MB in total. The
    # worker reads each media file of the zip up to WHATSAPP_MEDIA_MAX_MB
    # (declared sizes inside a zip cannot be trusted); larger files are skipped.
    whatsapp_import_max_mb: int = 8192
    whatsapp_chunk_mb: int = 8
    whatsapp_media_max_mb: int = 200
    # WhatsApp Web sync extension (DESIGN §6.8, docs/extension-api.md):
    # comma-separated origins allowed by CORS, each exactly
    # "chrome-extension://<32-letter id>" (anything else is ignored). Empty
    # (default): no CORS middleware at all. CORS never allows credentials: the
    # extension authenticates with its bearer token, never with the cookie.
    extension_origins: str = ""

    # Claude API (docs/DESIGN.md §4): the one-time bitácora and medication-list
    # importers (scripts/import_bitacora.py, scripts/import_meds.py) and the
    # worker's lab PDF extraction
    # (app/services/labs). Without the key the importer refuses to run and lab
    # extraction jobs fail (nothing is recorded, so they can be re-run).
    anthropic_api_key: str = ""
    extraction_model: str = "claude-opus-5-5"
    # Largest lab PDF sent to Claude. PDF input allows 32 MB per request and
    # base64 adds a third, so never above ~23; the default leaves headroom in
    # the 256 MB worker (raw + base64 + request body are in memory at once).
    # Larger PDFs are recorded as failed ("too_large") and never sent.
    lab_pdf_max_mb: int = 15
    # Lab portal links found in WhatsApp messages (app/services/labs/links.py):
    # comma-separated host globs ("resultados.labprueba.example,*.lab.example")
    # the worker may download a PDF from. Empty (default): links are recorded
    # for the «Enlaces pendientes» list but never fetched automatically.
    lab_link_hosts: str = ""
    # Image/PDF classification of `daily`/`backfill` WhatsApp media (DESIGN
    # §6.7 step 3, app/services/media_classification): one call per file
    # content, a four-way category and a short description, so a cheap
    # vision-capable model is enough; lab reports are then read by
    # EXTRACTION_MODEL. Uses ANTHROPIC_API_KEY; without it the jobs fail
    # (nothing recorded, re-run once it is set).
    classification_model: str = "claude-haiku-5-5"

    # Chat (DESIGN §6.6, app/services/chat): the model behind the family chat
    # (tool use over the data, streamed), the cheap model that titles a new
    # session from its first exchange, and the cap on tool rounds per answer
    # (clamped to 1–20). Uses ANTHROPIC_API_KEY; without it the chat answers
    # 503 and the rest of the app works.
    chat_model: str = "claude-sonnet-5-5"
    chat_title_model: str = "claude-haiku-5-5"
    chat_max_tool_iterations: int = 8

    # Daily WhatsApp analysis (DESIGN §6.7 step 4, app/services/daily_analysis):
    # new messages of a `daily` import, grouped by Panamá day, go to Claude with
    # the record context and come back as suggestions for «Pendientes». Opus by
    # default: medical judgement (what is a real medication change, what is a
    # duplicate) matters more than price at one call per day of messages. At
    # most ANALYSIS_MAX_DAYS_PER_JOB days per job (clamped to ≥ 1); the rest go
    # to a follow-up job. Uses ANTHROPIC_API_KEY; without it the job fails
    # (re-run once it is set).
    analysis_model: str = "claude-opus-5-5"
    analysis_max_days_per_job: int = 14
    # Backfill analysis runs (DESIGN §6.7 step 6, daily_analysis/backfill.py):
    # started by an admin over a date range of stored messages. Default cost
    # limit per run (USD, from the API usage; the run stops when it is reached
    # and can be resumed with a higher one) and the longest range accepted.
    backfill_max_cost_usd: float = 20.0
    backfill_max_range_days: int = 366

    # Voice-note transcription (DESIGN §6.7 step 3, app/services/transcription):
    # the worker sends WhatsApp audio from `daily`/`backfill` imports to the
    # provider (only `openai` exists). Without OPENAI_API_KEY the transcription
    # jobs fail with a generic message (nothing recorded, so they can be re-run
    # once the key is set); the import itself is unaffected. Files over
    # TRANSCRIPTION_MAX_MB (OpenAI's limit is 25 MB) are recorded as
    # too_large and never sent.
    transcription_provider: str = "openai"
    transcription_model: str = "gpt-transcribe"
    transcription_max_mb: int = 25
    openai_api_key: str = ""

    @property
    def storage_configured(self) -> bool:
        return bool(self.do_spaces_key and self.do_spaces_secret and self.do_spaces_bucket)

    @property
    def lab_link_host_patterns(self) -> tuple[str, ...]:
        return tuple(p.strip().lower() for p in self.lab_link_hosts.split(",") if p.strip())

    @property
    def extension_origin_list(self) -> tuple[str, ...]:
        """The valid ``chrome-extension://<id>`` entries of EXTENSION_ORIGINS."""
        entries = (o.strip().rstrip("/").lower() for o in self.extension_origins.split(","))
        return tuple(o for o in entries if _EXTENSION_ORIGIN.fullmatch(o))

    @property
    def is_local(self) -> bool:
        """Local development or the test suite (not a deployed environment)."""
        return self.environment.lower() in {"development", "dev", "local", "test"}

    @property
    def cookie_secure(self) -> bool:
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return not self.is_local

    @property
    def email_dev_mode(self) -> bool:
        """Log magic links instead of sending them (local environments only)."""
        return self.is_local


@lru_cache
def get_settings() -> Settings:
    return Settings()
