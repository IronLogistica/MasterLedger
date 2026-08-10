"""Aggiunge deliveries.accrual_entry_id e il conto 141000 Fatture da
Emettere — per registrare il ricavo di competenza sui DDT spediti ma non
ancora fatturati a fine periodo (rateo attivo), stornato automaticamente
quando arriva la fattura vera. Vedi blueprints/sd/routes.py.

Revision ID: l0m1n2o3p4q5
Revises: k9l0m1n2o3p4
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'l0m1n2o3p4q5'
down_revision = 'k9l0m1n2o3p4'
branch_labels = None
depends_on = None


def upgrade():
    b = op.get_bind()
    insp = inspect(b)
    if "deliveries" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("deliveries")}
        if "accrual_entry_id" not in cols:
            with op.batch_alter_table("deliveries") as batch:
                batch.add_column(sa.Column("accrual_entry_id", sa.Integer()))
                batch.create_foreign_key("fk_deliveries_accrual_entry_id", "journal_entries",
                                         ["accrual_entry_id"], ["id"])


def downgrade():
    with op.batch_alter_table("deliveries") as batch:
        batch.drop_column("accrual_entry_id")
