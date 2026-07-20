"""Payroll PDF imports, remembered employee cost centers and account configuration.
Revision ID: 0a1b2c3d4e5f
Revises: f6a7b8c9d0e1
"""
from alembic import op
import sqlalchemy as sa
revision='0a1b2c3d4e5f'; down_revision='f6a7b8c9d0e1'; branch_labels=None; depends_on=None
def upgrade():
    op.create_table('payroll_employee_mappings',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('employee_key',sa.String(80),nullable=False,unique=True),sa.Column('employee_name',sa.String(160),nullable=False),sa.Column('cost_center_id',sa.Integer(),sa.ForeignKey('cost_centers.id'),nullable=False),sa.Column('updated_at',sa.DateTime()))
    op.create_table('payroll_account_configs',sa.Column('id',sa.Integer(),primary_key=True),*[sa.Column(n,sa.Integer(),sa.ForeignKey('accounts.id')) for n in ('wage_expense_account_id','employer_burden_account_id','net_salary_payable_account_id','inps_payable_account_id','withholding_payable_account_id','bank_account_id')])
    op.create_table('payroll_imports',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('document_kind',sa.String(12),nullable=False),sa.Column('filename',sa.String(255),nullable=False),sa.Column('fingerprint',sa.String(64),nullable=False,unique=True),sa.Column('document_reference',sa.String(120)),sa.Column('document_date',sa.Date()),sa.Column('parsed_data',sa.Text(),nullable=False),sa.Column('status',sa.String(20),nullable=False,server_default='review'),sa.Column('journal_entry_id',sa.Integer(),sa.ForeignKey('journal_entries.id')),sa.Column('created_by_id',sa.Integer(),sa.ForeignKey('users.id')),sa.Column('created_at',sa.DateTime()),sa.Column('posted_at',sa.DateTime()))
def downgrade():
    op.drop_table('payroll_imports');op.drop_table('payroll_account_configs');op.drop_table('payroll_employee_mappings')
