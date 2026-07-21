"""Indici per reporting CO actual-costing.

La release CO non crea un secondo ledger: sfrutta JournalLine, Account e
CostCenter già presenti. Gli indici supportano filtri per periodo, elemento e
centro di costo nel report derivato dalla prima nota.
"""
from alembic import op

revision = '5f6a7b8c9d0e'
down_revision = '4e5f6a7b8c9d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_journal_lines_cost_center_id', 'journal_lines', ['cost_center_id'], unique=False)
    op.create_index('ix_journal_lines_account_id', 'journal_lines', ['account_id'], unique=False)
    op.create_index('ix_journal_entries_doc_date', 'journal_entries', ['doc_date'], unique=False)


def downgrade():
    op.drop_index('ix_journal_entries_doc_date', table_name='journal_entries')
    op.drop_index('ix_journal_lines_account_id', table_name='journal_lines')
    op.drop_index('ix_journal_lines_cost_center_id', table_name='journal_lines')
