"""Fase 1 (piano dei conti canonico) — tabella account_mappings.

Sostituisce i codici conto cablati a mano in AP/AR/GL/Cespiti con una
configurazione centralizzata, modificabile solo da ruolo commercialista.
Non cambia alcun saldo esistente: i valori di default puntano agli stessi
codici già in uso oggi (vedi seed.py).

Revision ID: h2i3j4k5l6m7
Revises: g1h2i3j4k5l6
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'h2i3j4k5l6m7'
down_revision = 'g1h2i3j4k5l6'
branch_labels = None
depends_on = None


def upgrade():
    b = op.get_bind()
    insp = inspect(b)
    if "account_mappings" not in insp.get_table_names():
        op.create_table(
            "account_mappings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("concept_key", sa.String(length=60), nullable=False, unique=True),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column("label", sa.String(length=120), nullable=False),
            sa.Column("category", sa.String(length=40)),
            sa.Column("updated_by_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("updated_at", sa.DateTime()),
        )


def downgrade():
    op.drop_table("account_mappings")
