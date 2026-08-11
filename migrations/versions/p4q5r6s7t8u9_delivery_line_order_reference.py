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
    # FK creata SENZA nome esplicito nel vincolo: su SQLite (batch mode, usato
    # per ALTER TABLE) alembic pretende un nome per poterlo ricreare —
    # altrimenti fallisce con "Constraint must have a name" (stesso bug già
    # trovato in altre 3 migrazioni di questa catena). Su Postgres (Railway)
    # l'ALTER dirette funzionava comunque, per questo non era emerso prima.
    with op.batch_alter_table("delivery_lines") as batch_op:
        batch_op.add_column(sa.Column("sales_order_line_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_delivery_lines_sales_order_line_id", "sales_order_lines",
            ["sales_order_line_id"], ["id"],
        )


def downgrade():
    with op.batch_alter_table("delivery_lines") as batch_op:
        batch_op.drop_constraint("fk_delivery_lines_sales_order_line_id", type_="foreignkey")
        batch_op.drop_column("sales_order_line_id")
