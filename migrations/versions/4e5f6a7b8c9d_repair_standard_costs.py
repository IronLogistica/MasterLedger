"""Ripristino sicuro della tabella Costi Standard su database già distribuiti.

Alcuni ambienti creati prima dell'adozione delle migrazioni possono risultare
allineati nell'applicazione ma senza la tabella standard_costs. Questa revisione
è idempotente: crea solo gli elementi assenti e non modifica i dati esistenti.

Revision ID: 4e5f6a7b8c9d
Revises: 3d4e5f6a7b8c
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = '4e5f6a7b8c9d'
down_revision = '3d4e5f6a7b8c'
branch_labels = None
depends_on = None


def _columns(bind, table_name):
    return {column['name'] for column in inspect(bind).get_columns(table_name)}


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if 'standard_costs' not in inspector.get_table_names():
        op.create_table(
            'standard_costs',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('material_id', sa.Integer(), sa.ForeignKey('materials.id'), nullable=False),
            sa.Column('year', sa.Integer(), nullable=False),
            sa.Column('month', sa.Integer(), nullable=False),
            sa.Column('standard_material_cost', sa.Numeric(14, 4), nullable=False, server_default='0'),
            sa.Column('standard_labor_cost', sa.Numeric(14, 4), nullable=False, server_default='0'),
            sa.Column('standard_overhead_cost', sa.Numeric(14, 4), nullable=False, server_default='0'),
            sa.Column('notes', sa.String(length=300), nullable=True),
            sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
        )

    # Le colonne seguenti sono necessarie per collegare la produzione al costo
    # standard; vengono aggiunte solo se un vecchio database non le possiede.
    inspector = inspect(bind)
    if 'production_entries' in inspector.get_table_names():
        existing = _columns(bind, 'production_entries')
        if 'standard_cost_id' not in existing:
            op.add_column('production_entries', sa.Column('standard_cost_id', sa.Integer(), nullable=True))
            if bind.dialect.name != 'sqlite':
                op.create_foreign_key(
                    'fk_production_entries_standard_costs',
                    'production_entries', 'standard_costs', ['standard_cost_id'], ['id']
                )
        existing = _columns(bind, 'production_entries')
        for name in ('variance_materiali', 'variance_manodopera', 'variance_overhead'):
            if name not in existing:
                op.add_column(
                    'production_entries',
                    sa.Column(name, sa.Numeric(14, 2), nullable=False, server_default='0')
                )


def downgrade():
    # Non eseguiamo cancellazioni automatiche: questa è una migrazione di
    # ripristino destinata a database già in esercizio.
    pass
