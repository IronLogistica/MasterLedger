"""WIP parziale, legami stock e unicita competenza paghe.

Revision ID: z4a5b6c7d8e9
Revises: y3z4a5b6c7d8
"""
from alembic import op
import sqlalchemy as sa

revision = 'z4a5b6c7d8e9'
down_revision = 'y3z4a5b6c7d8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('payroll_imports', sa.Column('accounting_period', sa.String(length=7), nullable=True))
    # Recupera la competenza degli import storici prima di imporre l'unicità.
    # parsed_data è lo snapshot autorevole e contiene payroll_period nei file
    # creati dalle versioni precedenti.
    op.execute(r'''
        UPDATE payroll_imports
           SET accounting_period = COALESCE(
               substring(parsed_data from '"payroll_period"[[:space:]]*:[[:space:]]*"(20[0-9]{2}-[0-9]{2})"'),
               substring(document_reference from '(20[0-9]{2}-[0-9]{2})')
           )
         WHERE document_kind IN ('PAYSLIP', 'RATEI')
           AND accounting_period IS NULL
    ''')
    # Non scegliere arbitrariamente quale malloppone storico tenere: in caso
    # di duplicati la migrazione si ferma e richiede riconciliazione umana.
    op.execute(r'''
        DO $$
        BEGIN
          IF EXISTS (
              SELECT 1 FROM payroll_imports
               WHERE accounting_period IS NOT NULL
               GROUP BY document_kind, accounting_period HAVING COUNT(*) > 1
          ) THEN
              RAISE EXCEPTION 'Import paghe/ratei duplicati per tipo e periodo: riconciliare prima della migrazione';
          END IF;
        END $$
    ''')
    op.create_unique_constraint('uq_payroll_import_kind_period', 'payroll_imports',
                                ['document_kind', 'accounting_period'])

    op.add_column('production_material_issues',
                  sa.Column('stock_movement_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_prod_issue_stock_movement', 'production_material_issues',
                          'stock_movements', ['stock_movement_id'], ['id'])
    op.create_unique_constraint('uq_prod_issue_stock_movement', 'production_material_issues',
                                ['stock_movement_id'])

    op.create_table(
        'production_receipts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('production_order_id', sa.Integer(), nullable=False),
        sa.Column('qty', sa.Numeric(14, 3), nullable=False),
        sa.Column('standard_value', sa.Numeric(14, 2), nullable=False),
        sa.Column('wip_relieved', sa.Numeric(14, 2), nullable=False),
        sa.Column('variance', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('journal_entry_id', sa.Integer(), nullable=False),
        sa.Column('stock_movement_id', sa.Integer(), nullable=False),
        sa.Column('posting_date', sa.Date(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['production_order_id'], ['production_orders.id']),
        sa.ForeignKeyConstraint(['journal_entry_id'], ['journal_entries.id']),
        sa.ForeignKeyConstraint(['stock_movement_id'], ['stock_movements.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.UniqueConstraint('journal_entry_id', name='uq_prod_receipt_journal_entry'),
        sa.UniqueConstraint('stock_movement_id', name='uq_prod_receipt_stock_movement'),
    )
    op.create_index('ix_production_receipts_production_order_id', 'production_receipts',
                    ['production_order_id'])


def downgrade():
    op.drop_index('ix_production_receipts_production_order_id', table_name='production_receipts')
    op.drop_table('production_receipts')
    op.drop_constraint('uq_prod_issue_stock_movement', 'production_material_issues', type_='unique')
    op.drop_constraint('fk_prod_issue_stock_movement', 'production_material_issues', type_='foreignkey')
    op.drop_column('production_material_issues', 'stock_movement_id')
    op.drop_constraint('uq_payroll_import_kind_period', 'payroll_imports', type_='unique')
    op.drop_column('payroll_imports', 'accounting_period')
