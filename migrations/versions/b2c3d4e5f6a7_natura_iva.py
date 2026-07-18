"""natura IVA — codice Natura per fatture con aliquota zero

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-08 00:00:00.000000

Aggiunge la colonna natura su JournalEntry: le specifiche tecniche SdI
(Allegato A vers. 1.9) impongono l'elemento <Natura> sia nella linea
(controllo 00400) sia nei DatiRiepilogo (controllo 00429) quando
l'aliquota IVA è pari a zero. Senza questo dato, l'XML di una fattura
"esente" viene scartato dal Sistema di Interscambio.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('journal_entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('natura', sa.String(length=4), nullable=True))


def downgrade():
    with op.batch_alter_table('journal_entries', schema=None) as batch_op:
        batch_op.drop_column('natura')
