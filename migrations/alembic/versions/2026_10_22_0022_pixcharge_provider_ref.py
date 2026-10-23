"""pix_charges.provider_ref — id do checkout do C6 (compra com cartão).

Aditiva e nula: o PIX não usa a coluna, e o código antigo segue funcionando
com ela presente (nenhum SELECT antigo a referencia). Necessária porque o
Checkout C6 identifica o link por um UUID próprio, que não é o nosso txid e
não pode ser deduzido da URL de pagamento.

Revision ID: 20261022_0022
Revises: 20260903_0021
"""
from alembic import op
import sqlalchemy as sa

revision = "20261022_0022"
down_revision = "20260903_0021"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    cols = sa.inspect(op.get_bind()).get_columns(table)
    return any(c["name"] == column for c in cols)


def upgrade() -> None:
    # Idempotente: em dev o _sync_model_columns pode ter criado a coluna antes.
    if _has_column("pix_charges", "provider_ref"):
        return
    with op.batch_alter_table("pix_charges") as batch:
        batch.add_column(sa.Column("provider_ref", sa.String(length=64), nullable=True))


def downgrade() -> None:
    if not _has_column("pix_charges", "provider_ref"):
        return
    with op.batch_alter_table("pix_charges") as batch:
        batch.drop_column("provider_ref")
