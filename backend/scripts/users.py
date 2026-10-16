"""Manage the sign-in allowlist.

Run from ``backend/`` (locally: ``docker compose exec backend ...``; in prod:
``docker compose -f docker-compose.prod.yml run --rm n16chat-backend ...``):

    python -m scripts.users add <email> [--name "Nombre"] [--admin]
    python -m scripts.users list
    python -m scripts.users deactivate <email>
    python -m scripts.users import-access <email> on|off

``add`` on an existing email reactivates it and updates the name/role when
given. ``deactivate`` also ends every session of that user.

``import-access`` sets or clears ``users.can_import`` (ticket #96): the
WhatsApp import area (imports, backfill analysis runs, the sync extension's
key) needs an admin **with** it. A new admin starts without it. ``on`` is
refused for a non-admin; ``off`` also revokes the user's extension key.
"""

import argparse
import sys

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from app.core.database import SessionLocal
from app.models.user import ROLE_ADMIN, ROLE_MEMBER, Session, User
from app.services import extension_tokens
from app.services.auth import normalize_email


def add_user(db: DbSession, email: str, name: str | None, admin: bool) -> tuple[User, bool]:
    """Create or reactivate a user. Returns ``(user, created)``."""
    email = normalize_email(email)
    if "@" not in email:
        raise ValueError(f"not an email address: {email!r}")
    user = db.scalar(select(User).where(User.email == email))
    created = user is None
    if created:
        user = User(
            email=email,
            display_name=name or email.split("@", 1)[0],
            role=ROLE_ADMIN if admin else ROLE_MEMBER,
        )
        db.add(user)
    else:
        user.is_active = True
        if name:
            user.display_name = name
        if admin:
            user.role = ROLE_ADMIN
    db.commit()
    return user, created


def deactivate_user(db: DbSession, email: str) -> User | None:
    user = db.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None:
        return None
    user.is_active = False
    db.execute(delete(Session).where(Session.user_id == user.id))
    db.commit()
    return user


def set_import_access(db: DbSession, email: str, enabled: bool) -> User:
    """Set or clear ``can_import``. Raises ``ValueError`` for an unknown email
    or ``on`` for a non-admin. ``off`` revokes the active extension key."""
    email = normalize_email(email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        raise ValueError(f"no user {email}")
    if enabled and not user.is_admin:
        raise ValueError(f"{email} is not an admin (add it with --admin first)")
    user.can_import = enabled
    if not enabled:
        extension_tokens.revoke(db, user.id)
    db.commit()
    return user


def list_users(db: DbSession) -> list[User]:
    return list(db.scalars(select(User).order_by(User.email)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.users", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_add = sub.add_parser("add", help="add or reactivate an allowlisted user")
    p_add.add_argument("email")
    p_add.add_argument("--name", help="display name shown in the app")
    p_add.add_argument("--admin", action="store_true", help="give the admin role")
    sub.add_parser("list", help="list users")
    p_off = sub.add_parser("deactivate", help="block sign-in and end all sessions")
    p_off.add_argument("email")
    p_imp = sub.add_parser(
        "import-access", help="allow (on) or forbid (off) WhatsApp imports for an admin"
    )
    p_imp.add_argument("email")
    p_imp.add_argument("state", choices=["on", "off"])
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        if args.command == "add":
            try:
                user, created = add_user(db, args.email, args.name, args.admin)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            verb = "added" if created else "updated"
            print(f"{verb}: {user.email} ({user.display_name}, {user.role}, active)")
        elif args.command == "list":
            users = list_users(db)
            if not users:
                print("no users")
            for u in users:
                state = "active" if u.is_active else "inactive"
                imports = "imports" if u.is_import_admin else "-"
                print(f"{u.email}\t{u.display_name}\t{u.role}\t{state}\t{imports}")
        elif args.command == "deactivate":
            user = deactivate_user(db, args.email)
            if user is None:
                print(f"error: no user {normalize_email(args.email)}", file=sys.stderr)
                return 1
            print(f"deactivated: {user.email} (sessions ended)")
        elif args.command == "import-access":
            try:
                user = set_import_access(db, args.email, args.state == "on")
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(f"import access {args.state}: {user.email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
