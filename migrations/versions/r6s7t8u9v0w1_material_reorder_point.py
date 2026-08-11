"""material reorder point (scorta minima) per cruscotto Magazzino/Fabbisogno

Revision ID: r6s7t8u9v0w1
Revises: q5r6s7t8u9v0
Create Date: 2026-08-11
"""
from alembic import op
import sqlalchemy as sa

revision = "r6s7t8u9v0w1"
down_revision = "q5r6s7t8u9v0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("materials", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("reorder_point", sa.Numeric(14, 3), nullable=False, server_default="0")
        )
    # server_default solo per popolare le righe esistenti; da qui in poi lo
    # gestisce l'applicazione (default=0 nel modello).
    with op.batch_alter_table("materials", schema=None) as batch_op:
        batch_op.alter_column("reorder_point", server_default=None)


def downgrade():
    with op.batch_alter_table("materials", schema=None) as batch_op:
        batch_op.drop_column("reorder_point")
