"""fatturapa fields — dati fiscali cliente + aliquota IVA fattura

Revision ID: a1b2c3d4e5f6
Revises: f7ed3b5853f8
Create Date: 2026-07-06 00:00:00.000000

Aggiunge i campi anagrafici/fiscali sul Customer necessari a costruire il
blocco CessionarioCommittente dell'XML FatturaPA (services/fatturapa.py),
e la colonna vat_rate su JournalEntry per salvare l'aliquota IVA della
fattura senza doverla ricalcolare per differenza.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'f7ed3b5853f8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('codice_fiscale', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('indirizzo', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('cap', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('comune', sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column('provincia', sa.String(length=2), nullable=True))
        batch_op.add_column(sa.Column('nazione', sa.String(length=2), nullable=True))
        batch_op.add_column(sa.Column('codice_destinatario', sa.String(length=7), nullable=True))
        batch_op.add_column(sa.Column('pec_destinatario', sa.String(length=120), nullable=True))

    with op.batch_alter_table('journal_entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('vat_rate', sa.Numeric(precision=5, scale=2), nullable=True))

    # Default sensato per i clienti già esistenti: "0000000" = recapito
    # tramite PEC (richiede comunque la PEC compilata a mano per generare
    # l'XML — vedi validazione in services/fatturapa.py).
    op.execute("UPDATE customers SET codice_destinatario = '0000000' WHERE codice_destinatario IS NULL")
    op.execute("UPDATE customers SET nazione = 'IT' WHERE nazione IS NULL")


def downgrade():
    with op.batch_alter_table('journal_entries', schema=None) as batch_op:
        batch_op.drop_column('vat_rate')

    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_column('pec_destinatario')
        batch_op.drop_column('codice_destinatario')
        batch_op.drop_column('nazione')
        batch_op.drop_column('provincia')
        batch_op.drop_column('comune')
        batch_op.drop_column('cap')
        batch_op.drop_column('indirizzo')
        batch_op.drop_column('codice_fiscale')
