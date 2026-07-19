"""Conto Varianza Prezzo Materiali (Purchase Price Variance).

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-19
"""
from alembic import op
import sqlalchemy as sa

revision = 'b8c9d0e1f2a3'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    esiste = conn.execute(sa.text("SELECT 1 FROM accounts WHERE code = '460000'")).fetchone()
    if not esiste:
        conn.execute(sa.text("""
            INSERT INTO accounts (code, name, account_type, cost_relevant, cost_relevant_type, active)
            VALUES ('460000', 'Varianza Prezzo Materiali', 'costo', true, 'COST', true)
        """))


def downgrade():
    op.execute("DELETE FROM accounts WHERE code = '460000'")
