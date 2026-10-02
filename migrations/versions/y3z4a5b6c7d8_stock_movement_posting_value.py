"""Valore contabile esteso dei movimenti stock, riconciliato con FI.

Revision ID: y3z4a5b6c7d8
Revises: x2y3z4a5b6c7
"""
from alembic import op
import sqlalchemy as sa

revision = 'y3z4a5b6c7d8'
down_revision = 'x2y3z4a5b6c7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('stock_movements', sa.Column('posting_value', sa.Numeric(precision=14, scale=2), nullable=True))
    # Backfill prudente dei movimenti storici dal valore unitario disponibile.
    # Resta NULL soltanto dove il costo unitario non era stato registrato.
    op.execute('''
        UPDATE stock_movements
           SET posting_value = ROUND(qty * unit_cost, 2)
         WHERE unit_cost IS NOT NULL AND posting_value IS NULL
    ''')


def downgrade():
    op.drop_column('stock_movements', 'posting_value')
