"""DeliveryLine.sales_order_line_id — riferimento esplicito alla riga ordine
di origine, per storni non ambigui quando un ordine ha lo stesso articolo
su più righe (stesso SKU a prezzi diversi).

Revision ID: p4q5r6s7t8u9
Revises: o3p4q5r6s7t8
"""
from alembic import op
import sqlalchemy as sa

revision = "p4q5r6s7t8u9"
down_revision = "o3p4q5r6s7t8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("delivery_lines") as batch_op:
        batch_op.add_column(sa.Column("sales_order_line_id", sa.Integer(),
                                      sa.ForeignKey("sales_order_lines.id"), nullable=True))


def downgrade():
    with op.batch_alter_table("delivery_lines") as batch_op:
        batch_op.drop_column("sales_order_line_id")
