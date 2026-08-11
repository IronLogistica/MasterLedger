"""Fase 4 — riconciliazione bancaria (bank_statements/lines/allocations) e
campi di storno di dominio su goods_receipts e deliveries.

Revision ID: k5l6m7n8o9p0
Revises: j9k0l1m2n3o4
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'k5l6m7n8o9p0'
down_revision = 'j9k0l1m2n3o4'
branch_labels = None
depends_on = None


def upgrade():
    b = op.get_bind()
    insp = inspect(b)

    if "bank_statements" not in insp.get_table_names():
        op.create_table(
            "bank_statements",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("bank_account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column("period_from", sa.Date(), nullable=False),
            sa.Column("period_to", sa.Date(), nullable=False),
            sa.Column("opening_balance", sa.Numeric(14, 2), nullable=False),
            sa.Column("closing_balance", sa.Numeric(14, 2), nullable=False),
            sa.Column("import_filename", sa.String(length=255)),
            sa.Column("file_hash", sa.String(length=64)),
            sa.Column("imported_by_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("imported_at", sa.DateTime()),
            sa.UniqueConstraint("bank_account_id", "file_hash", name="uq_statement_file_hash"),
        )
    if "bank_statement_lines" not in insp.get_table_names():
        op.create_table(
            "bank_statement_lines",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("statement_id", sa.Integer(), sa.ForeignKey("bank_statements.id"), nullable=False),
            sa.Column("value_date", sa.Date(), nullable=False),
            sa.Column("description", sa.String(length=255)),
            sa.Column("amount", sa.Numeric(14, 2), nullable=False),
            sa.Column("bank_transaction_id", sa.String(length=120)),
            sa.Column("import_hash", sa.String(length=64), nullable=False),
            sa.UniqueConstraint("statement_id", "import_hash", name="uq_statement_line_hash"),
        )
    if "bank_reconciliation_allocations" not in insp.get_table_names():
        op.create_table(
            "bank_reconciliation_allocations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("statement_line_id", sa.Integer(), sa.ForeignKey("bank_statement_lines.id"), nullable=False),
            sa.Column("journal_line_id", sa.Integer(), sa.ForeignKey("journal_lines.id"), nullable=False),
            sa.Column("amount_allocated", sa.Numeric(14, 2), nullable=False),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("created_at", sa.DateTime()),
            sa.Column("reversed", sa.Boolean()),
        )

    gr_cols = {c["name"] for c in insp.get_columns("goods_receipts")} if "goods_receipts" in insp.get_table_names() else set()
    with op.batch_alter_table("goods_receipts") as batch:
        if "is_reversed" not in gr_cols:
            batch.add_column(sa.Column("is_reversed", sa.Boolean(), server_default=sa.false()))
        if "reversal_reason" not in gr_cols:
            batch.add_column(sa.Column("reversal_reason", sa.String(length=255)))
        if "reversed_at" not in gr_cols:
            batch.add_column(sa.Column("reversed_at", sa.DateTime()))
        if "reversed_by_id" not in gr_cols:
            batch.add_column(sa.Column("reversed_by_id", sa.Integer(), sa.ForeignKey("users.id")))

    dl_cols = {c["name"] for c in insp.get_columns("deliveries")} if "deliveries" in insp.get_table_names() else set()
    with op.batch_alter_table("deliveries") as batch:
        if "is_reversed" not in dl_cols:
            batch.add_column(sa.Column("is_reversed", sa.Boolean(), server_default=sa.false()))
        if "reversal_reason" not in dl_cols:
            batch.add_column(sa.Column("reversal_reason", sa.String(length=255)))
        if "reversed_at" not in dl_cols:
            batch.add_column(sa.Column("reversed_at", sa.DateTime()))
        if "reversed_by_id" not in dl_cols:
            batch.add_column(sa.Column("reversed_by_id", sa.Integer(), sa.ForeignKey("users.id")))


def downgrade():
    op.drop_table("bank_reconciliation_allocations")
    op.drop_table("bank_statement_lines")
    op.drop_table("bank_statements")
    with op.batch_alter_table("goods_receipts") as batch:
        batch.drop_column("is_reversed")
        batch.drop_column("reversal_reason")
        batch.drop_column("reversed_at")
        batch.drop_column("reversed_by_id")
    with op.batch_alter_table("deliveries") as batch:
        batch.drop_column("is_reversed")
        batch.drop_column("reversal_reason")
        batch.drop_column("reversed_at")
        batch.drop_column("reversed_by_id")
