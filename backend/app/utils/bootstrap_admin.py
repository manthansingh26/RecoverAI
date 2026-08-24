"""Bootstrap the initial ADMIN operator for RecoverAI.

Creates the first ADMIN operator if none exists. The initial password is
generated randomly (CSPRNG), never hardcoded, never logged, and stored only
as an Argon2id hash. The operator is forced to change it on first use via
``must_change_password``.

Usage:
    python -m app.utils.bootstrap_admin --email admin@example.com

The generated password is printed to stdout exactly once. Save it securely
(outside this terminal) before closing — it cannot be recovered later.
"""

import argparse
import logging
import secrets
import sys
import uuid

from datetime import datetime, timezone

from app.core.roles import OperatorRole
from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.operator import Operator
from sqlalchemy import select

logger = logging.getLogger(__name__)


def _generate_initial_password() -> str:
    """Generate a strong random initial password (CSPRNG, ~144 bits)."""
    return secrets.token_urlsafe(18)


def bootstrap_admin(email: str) -> bool:
    """Create the first ADMIN operator if none exists.

    Args:
        email: Email address for the admin operator (normalized to lowercase).

    Returns:
        True if an admin was created, False if an admin already exists.

    Raises:
        RuntimeError: If the email is invalid or the database write fails.
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise RuntimeError("A valid email address is required.")

    db = SessionLocal()
    try:
        existing_admin = db.execute(
            select(Operator).where(Operator.role == OperatorRole.ADMIN.value)
        ).scalar_one_or_none()
        if existing_admin is not None:
            print(
                f"An ADMIN operator already exists ({existing_admin.email}). "
                "Refusing to create another bootstrap admin. "
                "To provision additional operators, extend the admin tooling "
                "(out of scope for Milestone 14A)."
            )
            return False

        initial_password = _generate_initial_password()

        admin = Operator(
            id=uuid.uuid4(),
            email=email,
            password_hash=hash_password(initial_password),
            role=OperatorRole.ADMIN.value,
            enabled=True,
            must_change_password=True,  # force password change after bootstrap
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(admin)
        db.commit()

        print()
        print("=" * 72)
        print("RecoverAI bootstrap admin created successfully.")
        print(f"  Email: {admin.email}")
        print(f"  Role : {OperatorRole.ADMIN.value}")
        print()
        print("  INITIAL PASSWORD (shown once, cannot be recovered):")
        print(f"    {initial_password}")
        print()
        print("  You will be required to change this password on first login.")
        print("  Do NOT paste this password into logs or issue trackers.")
        print("=" * 72)
        print()
        return True
    finally:
        db.close()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Bootstrap the first ADMIN operator for RecoverAI."
    )
    parser.add_argument(
        "--email",
        required=True,
        help="Email address for the initial admin operator.",
    )
    args = parser.parse_args()

    # Ensure the app never logs the password. Configure a minimal handler
    # bound to ERROR so accidental debug lines never surface credentials.
    logging.basicConfig(level=logging.ERROR)

    try:
        bootstrap_admin(args.email)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
