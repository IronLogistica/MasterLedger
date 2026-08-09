"""Aggiunge deliveries.stock_verified — traccia se il DDT è stato
registrato con controllo giacenza reale su MasterLogistic-WMS o col bypass
(WMS non ancora collegato). Vedi blueprints/sd/routes.py, deliveries().

Revision ID: k8l9m0n1o2p3
Revises: k7l8m9n0o1p2
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'k8l9m0n1o2p3'
down_revision = 'k7l8m9n0o1p2'
branch_labels = None
depends_on = None


def upgrade():
    b = op.get_bind()
    insp = inspect(b)
    if "deliveries" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("deliveries")}
        if "stock_verified" not in cols:
            with op.batch_alter_table("deliveries") as batch:
                batch.add_column(sa.Column("stock_verified", sa.Boolean(), server_default=sa.true()))


def downgrade():
    with op.batch_alter_table("deliveries") as batch:
        batch.drop_column("stock_verified")
