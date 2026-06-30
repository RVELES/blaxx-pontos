"""P2P 60s cancel window (ToS Sec. 9.2 — coherence guarantee).

Adiciona:
  - transfers.status        ENUM('pending','committed','cancelled')
  - transfers.committed_at  TIMESTAMP nullable
  - transfers.cancelled_at  TIMESTAMP nullable

Idempotente: cada ADD COLUMN checa existência antes.

Revision ID: 20260630_0004
Revises: 20260630_0003
Create Date: 2026-06-30
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260630_0004"
down_revision = "20260630_0003"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    """Adiciona colunas de status à tabela transfers.

    Estratégia de migração:
    - Linhas pré-existentes: status='committed' (já efetivadas no modelo antigo)
    - Novas linhas: status='pending' até committed_at <= NOW(), aí promovem
    """
    if not _has_column("transfers", "status"):
        op.add_column(
            "transfers",
            sa.Column(
                "status",
                sa.String(16),
                nullable=False,
                server_default="committed",
            ),
        )

    if not _has_column("transfers", "committed_at"):
        op.add_column(
            "transfers",
            sa.Column("committed_at", sa.DateTime, nullable=True),
        )
        # Backfill: linhas antigas já são commited — committed_at = created_at
        op.execute(
            "UPDATE transfers SET committed_at = created_at "
            "WHERE committed_at IS NULL AND status = 'committed'"
        )

    if not _has_column("transfers", "cancelled_at"):
        op.add_column(
            "transfers",
            sa.Column("cancelled_at", sa.DateTime, nullable=True),
        )

    # Index para promoção rápida — busca pending com committed_at <= NOW()
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_idx = {i["name"] for i in insp.get_indexes("transfers")}
    if "ix_transfers_status_committed_at" not in existing_idx:
        op.create_index(
            "ix_transfers_status_committed_at",
            "transfers",
            ["status", "committed_at"],
        )


def downgrade() -> None:
    """Remove colunas. Cuidado: cancela funcionalidade da janela de 60s."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_idx = {i["name"] for i in insp.get_indexes("transfers")}
    if "ix_transfers_status_committed_at" in existing_idx:
        op.drop_index("ix_transfers_status_committed_at", table_name="transfers")
    if _has_column("transfers", "cancelled_at"):
        op.drop_column("transfers", "cancelled_at")
    if _has_column("transfers", "committed_at"):
        op.drop_column("transfers", "committed_at")
    if _has_column("transfers", "status"):
        op.drop_column("transfers", "status")
