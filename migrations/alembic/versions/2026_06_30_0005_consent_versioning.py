"""Consent versioning (LGPD Art. 8º §5º) — evidence fields.

Adiciona à tabela user_consents:
  - user_agent  VARCHAR(500) nullable
  - text_hash   VARCHAR(64)  nullable  (SHA-256 do termo aceito)
  - status      VARCHAR(16)  default 'accepted'
  - revokes_consent_id  FK self-referential (revogação aponta para o original)

Cria índices:
  - ix_user_consents_user_id
  - ix_user_consents_type
  - ix_user_consents_accepted_at

Idempotente.

Revision ID: 20260630_0005
Revises: 20260630_0004
Create Date: 2026-06-30
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260630_0005"
down_revision = "20260630_0004"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return any(c["name"] == column for c in insp.get_columns(table))


def _has_index(table: str, name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return any(i["name"] == name for i in insp.get_indexes(table))


def upgrade() -> None:
    if not _has_column("user_consents", "user_agent"):
        op.add_column(
            "user_consents",
            sa.Column("user_agent", sa.String(500), nullable=True),
        )

    if not _has_column("user_consents", "text_hash"):
        op.add_column(
            "user_consents",
            sa.Column("text_hash", sa.String(64), nullable=True),
        )

    if not _has_column("user_consents", "status"):
        op.add_column(
            "user_consents",
            sa.Column(
                "status",
                sa.String(16),
                nullable=False,
                server_default="accepted",
            ),
        )

    if not _has_column("user_consents", "revokes_consent_id"):
        op.add_column(
            "user_consents",
            sa.Column(
                "revokes_consent_id",
                sa.String(32),
                nullable=True,
            ),
        )
        # Sem foreign key explícita no SQLite para evitar migração custosa.
        # Em Postgres adicionamos como FK proper.
        bind = op.get_bind()
        if bind.dialect.name == "postgresql":
            op.create_foreign_key(
                "fk_user_consents_revokes",
                source_table="user_consents",
                referent_table="user_consents",
                local_cols=["revokes_consent_id"],
                remote_cols=["id"],
                ondelete="SET NULL",
            )

    # Índices para queries comuns (timeline por user, busca por tipo, ordenação por data)
    if not _has_index("user_consents", "ix_user_consents_user_id"):
        op.create_index("ix_user_consents_user_id", "user_consents", ["user_id"])
    if not _has_index("user_consents", "ix_user_consents_type"):
        op.create_index("ix_user_consents_type", "user_consents", ["type"])
    if not _has_index("user_consents", "ix_user_consents_accepted_at"):
        op.create_index("ix_user_consents_accepted_at", "user_consents", ["accepted_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index("user_consents", "ix_user_consents_accepted_at"):
        op.drop_index("ix_user_consents_accepted_at", table_name="user_consents")
    if _has_index("user_consents", "ix_user_consents_type"):
        op.drop_index("ix_user_consents_type", table_name="user_consents")
    if _has_index("user_consents", "ix_user_consents_user_id"):
        op.drop_index("ix_user_consents_user_id", table_name="user_consents")
    if bind.dialect.name == "postgresql":
        try:
            op.drop_constraint("fk_user_consents_revokes", "user_consents", type_="foreignkey")
        except Exception:
            pass
    for col in ("revokes_consent_id", "status", "text_hash", "user_agent"):
        if _has_column("user_consents", col):
            op.drop_column("user_consents", col)
