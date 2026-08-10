"""Aggiunge invoice_verification_lines — traccia quali righe ordine e
quali quantità ha fatturato una Verifica Fattura (KR generato da MM).
Senza questa tabella non c'era modo sicuro di ripristinare qty_invoiced
se il documento veniva eliminato (vedi blueprints/mm/routes.py).

Revision ID: k9l0m1n2o3p4
Revises: k8l9m0n1o2p3
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'k9l0m1n2o3p4'
down_revision = 'k8l9m0n1o2p3'
branch_labels = None
depends_on = None


def upgrade():
    b = op.get_bind()
    insp = inspect(b)
    if "invoice_verification_lines" not in insp.get_table_names():
        op.create_table(
            "invoice_verification_lines",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id"), nullable=False),
            sa.Column("po_line_id", sa.Integer(), sa.ForeignKey("purchase_order_lines.id"), nullable=False),
            sa.Column("qty", sa.Numeric(14, 3), nullable=False),
        )


def downgrade():
    op.drop_table("invoice_verification_lines")
