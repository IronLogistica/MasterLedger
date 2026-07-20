"""Multi-centre payroll allocations, deferred accruals and generic document splits.
Revision ID: 3d4e5f6a7b8c
Revises: 2c3d4e5f6a7b
"""
from alembic import op
import sqlalchemy as sa
revision='3d4e5f6a7b8c'; down_revision='2c3d4e5f6a7b'; branch_labels=None; depends_on=None
def upgrade():
    op.create_table('payroll_employee_allocations', sa.Column('id',sa.Integer(),primary_key=True), sa.Column('mapping_id',sa.Integer(),sa.ForeignKey('payroll_employee_mappings.id'),nullable=False), sa.Column('cost_center_id',sa.Integer(),sa.ForeignKey('cost_centers.id'),nullable=False), sa.Column('percentage',sa.Numeric(5,2),nullable=False), sa.UniqueConstraint('mapping_id','cost_center_id',name='uq_payroll_mapping_center'))
    # Preserve existing one-centre mappings as a deterministic 100% allocation.
    op.execute("INSERT INTO payroll_employee_allocations (mapping_id,cost_center_id,percentage) SELECT id,cost_center_id,100.00 FROM payroll_employee_mappings")
    op.create_table('allocation_splits', sa.Column('id',sa.Integer(),primary_key=True),sa.Column('document_type',sa.String(30),nullable=False),sa.Column('document_id',sa.Integer(),nullable=False),sa.Column('document_line_id',sa.Integer(),nullable=True),sa.Column('cost_center_id',sa.Integer(),sa.ForeignKey('cost_centers.id'),nullable=False),sa.Column('percentage',sa.Numeric(5,2),nullable=False),sa.UniqueConstraint('document_type','document_id','document_line_id','cost_center_id',name='uq_allocation_split_target_center'))
    for name in ('accrued_holiday_expense_account_id','accrued_permission_expense_account_id','accrued_thirteenth_expense_account_id','accrued_payable_account_id','tfr_expense_account_id','tfr_fund_account_id'):
        op.add_column('payroll_account_configs',sa.Column(name,sa.Integer(),sa.ForeignKey('accounts.id'),nullable=True))
def downgrade():
    for name in ('tfr_fund_account_id','tfr_expense_account_id','accrued_payable_account_id','accrued_thirteenth_expense_account_id','accrued_permission_expense_account_id','accrued_holiday_expense_account_id'):op.drop_column('payroll_account_configs',name)
    op.drop_table('allocation_splits');op.drop_table('payroll_employee_allocations')
