"""Costo Standard e varianze di produzione (Materiali/Manodopera/Overhead).

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-19
"""
from alembic import op
import sqlalchemy as sa

revision = 'c9d0e1f2a3b4'
down_revision = 'b8c9d0e1f2a3'
branch_labels = None
depends_on = None


def upgrade():
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

    with op.batch_alter_table('production_entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('standard_cost_id', sa.Integer(),
                                       sa.ForeignKey('standard_costs.id'), nullable=True))
        batch_op.add_column(sa.Column('variance_materiali', sa.Numeric(14, 2), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('variance_manodopera', sa.Numeric(14, 2), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('variance_overhead', sa.Numeric(14, 2), nullable=False, server_default='0'))

    conn = op.get_bind()
    for code, name in (
        ("461000", "Varianza Materiali (Produzione)"),
        ("462000", "Varianza Manodopera (Produzione)"),
        ("463000", "Varianza Overhead (Produzione)"),
    ):
        esiste = conn.execute(sa.text("SELECT 1 FROM accounts WHERE code = :c"), {"c": code}).fetchone()
        if not esiste:
            conn.execute(sa.text("""
                INSERT INTO accounts (code, name, account_type, cost_relevant, cost_relevant_type, active)
                VALUES (:code, :name, 'costo', true, 'COST', true)
            """), {"code": code, "name": name})


def downgrade():
    with op.batch_alter_table('production_entries', schema=None) as batch_op:
        batch_op.drop_column('variance_overhead')
        batch_op.drop_column('variance_manodopera')
        batch_op.drop_column('variance_materiali')
        batch_op.drop_column('standard_cost_id')
    op.drop_table('standard_costs')
