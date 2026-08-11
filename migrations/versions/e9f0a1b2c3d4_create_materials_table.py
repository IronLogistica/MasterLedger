"""Crea la tabella materials (mancante nella cronologia migrazioni).

BUG PREESISTENTE: nessuna migrazione della cronologia originale creava la
tabella 'materials', pur essendo:
  - alterata da f6a7b8c9d0e1_carpenteria_propria.py (ALTER TABLE materials
    ADD COLUMN is_carpenteria_propria) — fallisce con NoSuchTableError su
    qualunque DB creato da zero con `flask db upgrade`;
  - referenziata come Foreign Key da production_orders, production_material_issues,
    requests_for_quotation in 6g7h8i9j0k1l_commesse_wip_rfq.py.
Questa migrazione va eseguita PRIMA di f6a7b8c9d0e1 (schema coerente col
modello Material in models.py, senza la colonna is_carpenteria_propria che
resta di competenza della migrazione successiva, come da progetto originale).

Revision ID: e9f0a1b2c3d4
Revises: e5f6a7b8c9d0
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa

revision = 'e9f0a1b2c3d4'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'materials',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(30), nullable=False, unique=True),
        sa.Column('description', sa.String(200), nullable=False),
        sa.Column('material_type', sa.String(5), nullable=False, server_default='FERT'),
        sa.Column('uom', sa.String(10), server_default='PZ'),
        sa.Column('standard_cost', sa.Numeric(14, 4), nullable=False, server_default='0'),
        sa.Column('sales_price', sa.Numeric(14, 4), nullable=False, server_default='0'),
        sa.Column('vat_rate', sa.Numeric(5, 2), nullable=False, server_default='22'),
        sa.Column('qty_on_hand', sa.Numeric(14, 3), nullable=False, server_default='0'),
        sa.Column('active', sa.Boolean(), server_default=sa.true()),
    )


def downgrade():
    op.drop_table('materials')
