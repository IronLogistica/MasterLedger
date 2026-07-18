"""multi-riga e note di credito — tabella invoice_lines + fattura collegata

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-08 00:00:00.000000

Aggiunge:
  - invoice_lines: righe commerciali delle fatture/note di credito cliente
    (multi-riga, multi-aliquota) che alimentano <DettaglioLinee> e
    <DatiRiepilogo> dell'XML FatturaPA.
  - journal_entries.linked_invoice_id: per le note di credito (DG/TD04),
    riferimento alla fattura originale (blocco <DatiFattureCollegate>).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'invoice_lines',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entry_id', sa.Integer(), nullable=False),
        sa.Column('line_number', sa.Integer(), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=False),
        sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column('vat_rate', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('natura', sa.String(length=4), nullable=True),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['entry_id'], ['journal_entries.id']),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('journal_entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('linked_invoice_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_je_linked_invoice', 'journal_entries',
                                    ['linked_invoice_id'], ['id'])


def downgrade():
    with op.batch_alter_table('journal_entries', schema=None) as batch_op:
        batch_op.drop_constraint('fk_je_linked_invoice', type_='foreignkey')
        batch_op.drop_column('linked_invoice_id')
    op.drop_table('invoice_lines')
