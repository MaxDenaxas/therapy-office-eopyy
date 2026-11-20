"""admin sessions (M5.0)

Server-side administrator sessions. Only SHA-256 hashes of the session token and
the CSRF token are stored. ``expires_at`` is the absolute limit fixed at login;
the idle limit is computed from ``last_seen_at``. Both are checked independently
by the application.

One trigger keeps a session's identity fixed: the owner, both hashes, the
creation time and the absolute expiry never change, and a revocation is final.
Only ``last_seen_at`` (touch) and ``revoked_at`` (once) may be updated.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIXED = """CREATE TRIGGER admin_sessions_fixed BEFORE UPDATE ON admin_sessions
WHEN NEW.admin_id IS NOT OLD.admin_id
  OR NEW.token_hash IS NOT OLD.token_hash
  OR NEW.csrf_hash IS NOT OLD.csrf_hash
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.expires_at IS NOT OLD.expires_at
  OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at)
BEGIN SELECT RAISE(ABORT, 'a session can only be touched or revoked'); END"""


def upgrade() -> None:
    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("admin_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "length(csrf_hash) = 64 AND csrf_hash NOT GLOB '*[^0-9a-f]*'",
            name=op.f("ck_admin_sessions_csrf_hash_sha256"),
        ),
        sa.CheckConstraint(
            "length(token_hash) = 64 AND token_hash NOT GLOB '*[^0-9a-f]*'",
            name=op.f("ck_admin_sessions_token_hash_sha256"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_admin_sessions_expires_after_created")
        ),
        sa.CheckConstraint(
            "last_seen_at >= created_at", name=op.f("ck_admin_sessions_last_seen_after_created")
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name=op.f("ck_admin_sessions_revoked_after_created"),
        ),
        sa.ForeignKeyConstraint(
            ["admin_id"],
            ["admin_account.id"],
            name=op.f("fk_admin_sessions_admin_id_admin_account"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_admin_sessions_token_hash")),
    )
    with op.batch_alter_table("admin_sessions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_admin_sessions_expires_at"), ["expires_at"], unique=False
        )
    op.execute(_FIXED)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS admin_sessions_fixed")
    with op.batch_alter_table("admin_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_admin_sessions_expires_at"))
    op.drop_table("admin_sessions")
