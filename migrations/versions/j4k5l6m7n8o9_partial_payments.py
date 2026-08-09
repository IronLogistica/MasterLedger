"""Fase 3 (pagamenti parziali) — tabelle invoice_installments e payment_allocations.

Revision ID: j4k5l6m7n8o9
Revises: i3j4k5l6m7n8
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'j4k5l6m7n8o9'
down_revision = 'i3j4k5l6m7n8'
branch_labels = None
depends_on = None


def upgrade():
    b = op.get_bind()
    insp = inspect(b)
    if "invoice_installments" not in insp.get_table_names():
        op.create_table(
            "invoice_installments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id"), nullable=False),
            sa.Column("numero_rata", sa.Integer(), nullable=False),
            sa.Column("due_date", sa.Date(), nullable=False),
            sa.Column("amount", sa.Numeric(14, 2), nullable=False),
            sa.Column("residual_amount", sa.Numeric(14, 2), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime()),
        )
    if "payment_allocations" not in insp.get_table_names():
        op.create_table(
            "payment_allocations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("payment_entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id"), nullable=False),
            sa.Column("installment_id", sa.Integer(), sa.ForeignKey("invoice_installments.id"), nullable=False),
            sa.Column("cash_amount", sa.Numeric(14, 2), nullable=False),
            sa.Column("abbuono_amount", sa.Numeric(14, 2), nullable=False),
            sa.Column("created_at", sa.DateTime()),
            sa.Column("reversed", sa.Boolean()),
            sa.Column("reversed_at", sa.DateTime()),
            sa.Column("reversed_by_id", sa.Integer(), sa.ForeignKey("users.id")),
        )


def downgrade():
    op.drop_table("payment_allocations")
    op.drop_table("invoice_installments")
