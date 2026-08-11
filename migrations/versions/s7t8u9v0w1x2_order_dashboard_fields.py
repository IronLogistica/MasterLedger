"""ordini cliente/fornitore: campi cruscotto stile MasterLogistic (data
consegna, conferma, ritiro/consegna trasporto, priorità drag&drop)

Revision ID: s7t8u9v0w1x2
Revises: r6s7t8u9v0w1
Create Date: 2026-08-11
"""
from alembic import op
import sqlalchemy as sa

revision = "s7t8u9v0w1x2"
down_revision = "r6s7t8u9v0w1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("sales_orders", schema=None) as batch_op:
        batch_op.add_column(sa.Column("delivery_due_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("priority", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("sales_orders", schema=None) as batch_op:
        batch_op.alter_column("confirmed", server_default=None)
        batch_op.alter_column("priority", server_default=None)

    with op.batch_alter_table("purchase_orders", schema=None) as batch_op:
        batch_op.add_column(sa.Column("delivery_due_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("pickup_mode", sa.String(20), nullable=False, server_default="consegnano_loro"))
        batch_op.add_column(sa.Column("priority", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("purchase_orders", schema=None) as batch_op:
        batch_op.alter_column("confirmed", server_default=None)
        batch_op.alter_column("pickup_mode", server_default=None)
        batch_op.alter_column("priority", server_default=None)


def downgrade():
    with op.batch_alter_table("purchase_orders", schema=None) as batch_op:
        batch_op.drop_column("priority")
        batch_op.drop_column("pickup_mode")
        batch_op.drop_column("confirmed")
        batch_op.drop_column("delivery_due_date")

    with op.batch_alter_table("sales_orders", schema=None) as batch_op:
        batch_op.drop_column("priority")
        batch_op.drop_column("confirmed")
        batch_op.drop_column("delivery_due_date")
