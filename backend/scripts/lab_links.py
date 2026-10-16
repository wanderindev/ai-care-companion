"""Find lab portal links in WhatsApp messages already imported (operator CLI).

Run from ``backend/`` (locally: ``docker compose exec backend ...``; in prod:
``docker compose -f docker-compose.prod.yml run --rm n16chat-backend ...``):

    python -m scripts.lab_links scan

New imports record their links themselves (the ``after_import`` hook); this
covers messages imported before #16. It records every new lab link
(``app.services.labs.links.record_links``; idempotent: links are deduped on
the normalized URL), then queues a ``lab_link_fetch`` job for each pending
link that was never tried and whose host is now in ``LAB_LINK_HOSTS`` (run it
again after changing that setting). It prints counts only, never URLs.
"""

import argparse

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal
from app.models.whatsapp import WhatsappMessage
from app.services.labs import links

BATCH = 500


def scan(db: DbSession, settings: Settings) -> tuple[int, int, int]:
    """``(messages scanned, links recorded, links queued)``. Commits per batch."""
    scanned = recorded = queued = 0
    last_id = 0
    while True:
        batch = list(
            db.scalars(
                select(WhatsappMessage)
                .where(WhatsappMessage.id > last_id, WhatsappMessage.text.ilike("%http%"))
                .order_by(WhatsappMessage.id)
                .limit(BATCH)
            )
        )
        if not batch:
            break
        last_id = batch[-1].id
        scanned += len(batch)
        created = links.record_links(db, batch, settings)
        recorded += len(created)
        queued += sum(1 for link in created if link.job_id is not None)
        db.commit()
    queued += links.enqueue_untried(db, settings)
    db.commit()
    return scanned, recorded, queued


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.lab_links", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="record lab links in imported messages and queue fetches")
    parser.parse_args(argv)
    with SessionLocal() as db:
        scanned, recorded, queued = scan(db, get_settings())
    print(
        f"{scanned} message(s) with a URL scanned, {recorded} new lab link(s) recorded, "
        f"{queued} download(s) queued."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
