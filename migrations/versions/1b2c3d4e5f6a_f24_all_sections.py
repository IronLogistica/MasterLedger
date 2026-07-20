"""Keep and configure every F24 section, including IMU.
Revision ID: 1b2c3d4e5f6a
Revises: 0a1b2c3d4e5f
"""
from alembic import op
import sqlalchemy as sa
revision='1b2c3d4e5f6a'; down_revision='0a1b2c3d4e5f'; branch_labels=None; depends_on=None
def upgrade():
    op.add_column('payroll_account_configs',sa.Column('imu_expense_account_id',sa.Integer(),sa.ForeignKey('accounts.id'),nullable=True))
    op.create_table('f24_imu_mappings',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('municipality_code',sa.String(8),nullable=False),sa.Column('tribute_code',sa.String(12),nullable=False),sa.Column('expense_account_id',sa.Integer(),sa.ForeignKey('accounts.id'),nullable=False),sa.Column('cost_center_id',sa.Integer(),sa.ForeignKey('cost_centers.id'),nullable=False),sa.Column('updated_at',sa.DateTime()),sa.UniqueConstraint('municipality_code','tribute_code',name='uq_f24_imu_mapping'))
def downgrade():
    op.drop_table('f24_imu_mappings');op.drop_column('payroll_account_configs','imu_expense_account_id')
