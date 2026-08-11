"""Correzione storia migrazioni — le tabelle del ciclo SD/MM (preventivi,
ordini cliente/fornitore, DDT, entrate merci) esistono in produzione solo
perché create una tantum da un vecchio db.create_all() automatico, rimosso
il 19/07/2026 (vedi app.py). Non sono mai state tracciate da Alembic: un
ambiente NUOVO che segue la procedura documentata nel README (solo
`flask db upgrade`) le trova mancanti e va in errore alla prima Entrata
Merci/DDT. Creazione IF NOT EXISTS: no-op in produzione (le tabelle ci
sono già), le crea per la prima volta su un ambiente davvero da zero.

Revision ID: j9k0l1m2n3o4
Revises: j4k5l6m7n8o9
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'j9k0l1m2n3o4'
down_revision = '5f6a7b8c9d0e'
branch_labels = None
depends_on = None


def upgrade():
    b = op.get_bind()
    insp = inspect(b)
    existing = set(insp.get_table_names())

    if "quotations" not in existing:
        op.create_table(
            "quotations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("doc_number", sa.String(length=20), nullable=False, unique=True),
            sa.Column("doc_date", sa.Date(), nullable=False),
            sa.Column("economic_subject_id", sa.Integer(), sa.ForeignKey("economic_subjects.id"), nullable=True),
            sa.Column("status", sa.String(length=15)),
            sa.Column("note", sa.String(length=255)),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("created_at", sa.DateTime()),
        )
    if "quotation_lines" not in existing:
        op.create_table(
            "quotation_lines",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("quotation_id", sa.Integer(), sa.ForeignKey("quotations.id"), nullable=False),
            sa.Column("material_id", sa.Integer(), sa.ForeignKey("materials.id"), nullable=False),
            sa.Column("qty", sa.Numeric(14, 3), nullable=False),
            sa.Column("price", sa.Numeric(14, 4), nullable=False),
        )
    if "sales_orders" not in existing:
        op.create_table(
            "sales_orders",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("doc_number", sa.String(length=20), nullable=False, unique=True),
            sa.Column("doc_date", sa.Date(), nullable=False),
            sa.Column("economic_subject_id", sa.Integer(), sa.ForeignKey("economic_subjects.id"), nullable=True),
            sa.Column("quotation_id", sa.Integer(), sa.ForeignKey("quotations.id"), nullable=True),
            sa.Column("status", sa.String(length=15)),
            sa.Column("note", sa.String(length=255)),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("created_at", sa.DateTime()),
        )
    if "sales_order_lines" not in existing:
        op.create_table(
            "sales_order_lines",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("order_id", sa.Integer(), sa.ForeignKey("sales_orders.id"), nullable=False),
            sa.Column("material_id", sa.Integer(), sa.ForeignKey("materials.id"), nullable=False),
            sa.Column("qty", sa.Numeric(14, 3), nullable=False),
            sa.Column("qty_delivered", sa.Numeric(14, 3), nullable=False),
            sa.Column("price", sa.Numeric(14, 4), nullable=False),
        )
    if "deliveries" not in existing:
        op.create_table(
            "deliveries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("doc_number", sa.String(length=20), nullable=False, unique=True),
            sa.Column("doc_date", sa.Date(), nullable=False),
            sa.Column("order_id", sa.Integer(), sa.ForeignKey("sales_orders.id"), nullable=False),
            sa.Column("economic_subject_id", sa.Integer(), sa.ForeignKey("economic_subjects.id"), nullable=True),
            sa.Column("cogs_entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id"), nullable=True),
            sa.Column("billing_entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id"), nullable=True),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("created_at", sa.DateTime()),
            sa.Column("is_reversed", sa.Boolean(), server_default=sa.false()),
            sa.Column("reversal_reason", sa.String(length=255)),
            sa.Column("reversed_at", sa.DateTime()),
            sa.Column("reversed_by_id", sa.Integer(), sa.ForeignKey("users.id")),
        )
    if "delivery_lines" not in existing:
        op.create_table(
            "delivery_lines",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("delivery_id", sa.Integer(), sa.ForeignKey("deliveries.id"), nullable=False),
            sa.Column("material_id", sa.Integer(), sa.ForeignKey("materials.id"), nullable=False),
            sa.Column("qty", sa.Numeric(14, 3), nullable=False),
            sa.Column("price", sa.Numeric(14, 4), nullable=False),
            sa.Column("unit_cost", sa.Numeric(14, 4), nullable=False),
        )
    if "purchase_orders" not in existing:
        op.create_table(
            "purchase_orders",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("doc_number", sa.String(length=20), nullable=False, unique=True),
            sa.Column("doc_date", sa.Date(), nullable=False),
            sa.Column("economic_subject_id", sa.Integer(), sa.ForeignKey("economic_subjects.id"), nullable=True),
            sa.Column("note", sa.String(length=255)),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("created_at", sa.DateTime()),
        )
    if "purchase_order_lines" not in existing:
        op.create_table(
            "purchase_order_lines",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("po_id", sa.Integer(), sa.ForeignKey("purchase_orders.id"), nullable=False),
            sa.Column("material_id", sa.Integer(), sa.ForeignKey("materials.id"), nullable=False),
            sa.Column("qty", sa.Numeric(14, 3), nullable=False),
            sa.Column("price", sa.Numeric(14, 4), nullable=False),
            sa.Column("qty_received", sa.Numeric(14, 3), nullable=False),
            sa.Column("qty_invoiced", sa.Numeric(14, 3), nullable=False),
        )
    if "goods_receipts" not in existing:
        op.create_table(
            "goods_receipts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("doc_number", sa.String(length=20), nullable=False, unique=True),
            sa.Column("doc_date", sa.Date(), nullable=False),
            sa.Column("po_id", sa.Integer(), sa.ForeignKey("purchase_orders.id"), nullable=False),
            sa.Column("ddt_vendor_ref", sa.String(length=60)),
            sa.Column("journal_entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id"), nullable=True),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("created_at", sa.DateTime()),
            sa.Column("is_reversed", sa.Boolean(), server_default=sa.false()),
            sa.Column("reversal_reason", sa.String(length=255)),
            sa.Column("reversed_at", sa.DateTime()),
            sa.Column("reversed_by_id", sa.Integer(), sa.ForeignKey("users.id")),
        )
    if "goods_receipt_lines" not in existing:
        op.create_table(
            "goods_receipt_lines",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("receipt_id", sa.Integer(), sa.ForeignKey("goods_receipts.id"), nullable=False),
            sa.Column("po_line_id", sa.Integer(), sa.ForeignKey("purchase_order_lines.id"), nullable=False),
            sa.Column("qty", sa.Numeric(14, 3), nullable=False),
        )


def downgrade():
    # Downgrade volutamente no-op: queste tabelle esistevano già in
    # produzione prima di questa migrazione — un downgrade che le elimina
    # cancellerebbe dati reali di produzione, non solo lo stato di test.
    pass
