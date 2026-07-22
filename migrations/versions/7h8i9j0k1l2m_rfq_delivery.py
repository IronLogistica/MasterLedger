"""tracciamento inoltri RFQ a fornitori

Revision ID: 7h8i9j0k1l2m
Revises: 6g7h8i9j0k1l
"""
from alembic import op
import sqlalchemy as sa

revision = '7h8i9j0k1l2m'
down_revision = '6g7h8i9j0k1l'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        'rfq_deliveries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('rfq_id', sa.Integer(), sa.ForeignKey('requests_for_quotation.id'), nullable=False),
        sa.Column('economic_subject_id', sa.Integer(), sa.ForeignKey('economic_subjects.id'), nullable=False),
        sa.Column('recipient_email', sa.String(length=120), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='inviata'),
        sa.Column('error_message', sa.String(length=500)),
        sa.Column('sent_at', sa.DateTime()),
        sa.Column('sent_by_id', sa.Integer(), sa.ForeignKey('users.id')),
    )
    op.create_index('ix_rfq_deliveries_rfq_id', 'rfq_deliveries', ['rfq_id'])

def downgrade():
    op.drop_index('ix_rfq_deliveries_rfq_id', table_name='rfq_deliveries')
    op.drop_table('rfq_deliveries')
