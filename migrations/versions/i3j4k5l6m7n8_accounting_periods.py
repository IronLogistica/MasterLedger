"""Fase 2 (chiusura periodi) — tabelle accounting_periods e accounting_period_logs.

Revision ID: i3j4k5l6m7n8
Revises: h2i3j4k5l6m7
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'i3j4k5l6m7n8'
down_revision = 'h2i3j4k5l6m7'
branch_labels = None
depends_on = None


def upgrade():
    b = op.get_bind()
    insp = inspect(b)
    if "accounting_periods" not in insp.get_table_names():
        op.create_table(
            "accounting_periods",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company", sa.String(length=80), nullable=False),
            sa.Column("year", sa.Integer(), nullable=False),
            sa.Column("month", sa.Integer(), nullable=False),
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("end_date", sa.Date(), nullable=False),
            sa.Column("period_type", sa.String(length=20)),
            sa.Column("status", sa.String(length=24)),
            sa.Column("closed_by_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("closed_at", sa.DateTime()),
            sa.Column("reopen_reason", sa.String(length=255)),
            sa.UniqueConstraint("company", "year", "month", name="uq_period_company_year_month"),
        )
    if "accounting_period_logs" not in insp.get_table_names():
        op.create_table(
            "accounting_period_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("period_id", sa.Integer(), sa.ForeignKey("accounting_periods.id"), nullable=False),
            sa.Column("action", sa.String(length=20), nullable=False),
            sa.Column("performed_by_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("performed_at", sa.DateTime()),
            sa.Column("reason", sa.String(length=255)),
        )


def downgrade():
    op.drop_table("accounting_period_logs")
    op.drop_table("accounting_periods")
