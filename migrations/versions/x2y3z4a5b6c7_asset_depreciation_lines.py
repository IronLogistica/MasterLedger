"""Dettaglio per cespite degli ammortamenti (AssetDepreciationLine) — serve
per poter stornare correttamente un ammortamento contabilizzato con
periodo/importo sbagliato (vedi services/reversals.reverse_depreciation).
Prima di questa tabella non esisteva alcun modo di correggere un
ammortamento già registrato.

Revision ID: x2y3z4a5b6c7
Revises: w1x2y3z4a5b6
"""
from alembic import op
import sqlalchemy as sa

revision = 'x2y3z4a5b6c7'
down_revision = 'w1x2y3z4a5b6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'asset_depreciation_lines',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('entry_id', sa.Integer(), sa.ForeignKey('journal_entries.id'), nullable=False),
        sa.Column('asset_id', sa.Integer(), sa.ForeignKey('assets.id'), nullable=False),
        sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_asset_depreciation_lines_entry_id', 'asset_depreciation_lines', ['entry_id'])
    op.create_index('ix_asset_depreciation_lines_asset_id', 'asset_depreciation_lines', ['asset_id'])


def downgrade():
    op.drop_index('ix_asset_depreciation_lines_asset_id', table_name='asset_depreciation_lines')
    op.drop_index('ix_asset_depreciation_lines_entry_id', table_name='asset_depreciation_lines')
    op.drop_table('asset_depreciation_lines')
