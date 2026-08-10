"""Add employee_inps_rate and employer_contribution_rate to payroll_account_configs.

These are convenience prefill rates only — the amount actually posted always
comes from the editable, human-reviewed field on each payslip line (never
applied blindly at posting time). They let the accountant configure a default
once instead of retyping it every month.

Revision ID: m1n2o3p4q5r6
Revises: l0m1n2o3p4q5
"""
from alembic import op
import sqlalchemy as sa
revision = 'm1n2o3p4q5r6'; down_revision = 'l0m1n2o3p4q5'; branch_labels = None; depends_on = None


def upgrade():
    with op.batch_alter_table('payroll_account_configs') as batch_op:
        batch_op.add_column(sa.Column('employee_inps_rate', sa.Numeric(5, 2), nullable=True))
        batch_op.add_column(sa.Column('employer_contribution_rate', sa.Numeric(5, 2), nullable=True))


def downgrade():
    with op.batch_alter_table('payroll_account_configs') as batch_op:
        batch_op.drop_column('employer_contribution_rate')
        batch_op.drop_column('employee_inps_rate')
