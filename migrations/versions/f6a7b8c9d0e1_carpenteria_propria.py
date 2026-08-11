"""Flag carpenteria propria su materials.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-19
"""
from alembic import op
import sqlalchemy as sa

revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('materials', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_carpenteria_propria', sa.Boolean(),
                                       nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table('materials', schema=None) as batch_op:
        batch_op.drop_column('is_carpenteria_propria')
