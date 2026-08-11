"""Magazzino interno: stock_movements, bill_of_materials, bom_components.

Sostituisce l'integrazione in sola lettura verso MasterLogistic-WMS con un
ledger di magazzino nativo di MasterLedger.

Revision ID: n2o3p4q5r6s7
Revises: m1n2o3p4q5r6
"""
from alembic import op
import sqlalchemy as sa
revision = 'n2o3p4q5r6s7'; down_revision = 'm1n2o3p4q5r6'; branch_labels = None; depends_on = None


def upgrade():
    op.create_table(
        'stock_movements',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('material_id', sa.Integer(), sa.ForeignKey('materials.id'), nullable=False),
        sa.Column('warehouse_area_id', sa.Integer(), sa.ForeignKey('warehouse_areas.id'), nullable=True),
        sa.Column('qty', sa.Numeric(14, 3), nullable=False),
        sa.Column('unit_cost', sa.Numeric(14, 4), nullable=True),
        sa.Column('movement_type', sa.String(20), nullable=False),
        sa.Column('source_type', sa.String(30), nullable=True),
        sa.Column('source_id', sa.Integer(), nullable=True),
        sa.Column('doc_date', sa.Date(), nullable=False),
        sa.Column('notes', sa.String(255)),
        sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime()),
    )
    op.create_index('ix_stock_movements_material_id', 'stock_movements', ['material_id'])

    op.create_table(
        'bill_of_materials',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('parent_material_id', sa.Integer(), sa.ForeignKey('materials.id'), nullable=False),
        sa.Column('version', sa.String(10), nullable=False, server_default='1'),
        sa.Column('active', sa.Boolean(), server_default=sa.true()),
        sa.Column('notes', sa.String(255)),
        sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime()),
        sa.UniqueConstraint('parent_material_id', 'version', name='uq_bom_parent_version'),
    )
    op.create_index('ix_bill_of_materials_parent_material_id', 'bill_of_materials', ['parent_material_id'])

    op.create_table(
        'bom_components',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('bom_id', sa.Integer(), sa.ForeignKey('bill_of_materials.id'), nullable=False),
        sa.Column('component_material_id', sa.Integer(), sa.ForeignKey('materials.id'), nullable=False),
        sa.Column('qty_per', sa.Numeric(14, 4), nullable=False),
        sa.Column('scrap_pct', sa.Numeric(5, 2), nullable=False, server_default='0'),
        sa.Column('notes', sa.String(255)),
        sa.UniqueConstraint('bom_id', 'component_material_id', name='uq_bom_component'),
    )


def downgrade():
    op.drop_table('bom_components')
    op.drop_table('bill_of_materials')
    op.drop_table('stock_movements')
