"""Anagrafica unificata Soggetti Economici.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-18
"""
from alembic import op
import sqlalchemy as sa

revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def _tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def _add_subject_reference(table):
    if table not in _tables():
        return
    cols = {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}
    if 'economic_subject_id' not in cols:
        op.add_column(table, sa.Column('economic_subject_id', sa.Integer(), nullable=True))


def upgrade():
    op.create_table(
        'economic_subjects',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(length=20), nullable=False, unique=True),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('subject_type', sa.String(length=12), nullable=False, server_default='azienda'),
        sa.Column('is_customer', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('is_supplier', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('piva', sa.String(length=20), nullable=True),
        sa.Column('codice_fiscale', sa.String(length=16), nullable=True),
        sa.Column('indirizzo', sa.String(length=120), nullable=True),
        sa.Column('cap', sa.String(length=10), nullable=True),
        sa.Column('comune', sa.String(length=80), nullable=True),
        sa.Column('provincia', sa.String(length=2), nullable=True),
        sa.Column('nazione', sa.String(length=2), nullable=True),
        sa.Column('email', sa.String(length=120), nullable=True),
        sa.Column('pec', sa.String(length=120), nullable=True),
        sa.Column('telefono', sa.String(length=40), nullable=True),
        sa.Column('codice_destinatario', sa.String(length=7), nullable=True),
        sa.Column('payment_terms', sa.String(length=40), nullable=True),
        sa.Column('iban', sa.String(length=34), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=True),
    )
    op.create_index('ix_economic_subjects_piva', 'economic_subjects', ['piva'])

    # Riferimento unico sui documenti: i vecchi campi restano nel DB per
    # preservare lo storico e consentire un rollback senza perdita dati.
    for table in ('journal_entries', 'quotations', 'sales_orders', 'deliveries', 'purchase_orders'):
        _add_subject_reference(table)

    bind = op.get_bind()
    tables = _tables()
    # Clienti: copia completa dei dati fiscali e assegna il ruolo Cliente.
    if 'customers' in tables:
        bind.execute(sa.text("""
            INSERT INTO economic_subjects
                (code, name, subject_type, is_customer, is_supplier, piva, codice_fiscale,
                 indirizzo, cap, comune, provincia, nazione, pec, codice_destinatario,
                 payment_terms, active)
            SELECT 'C-' || id, name, 'azienda', TRUE, FALSE, piva, codice_fiscale,
                   indirizzo, cap, comune, provincia, COALESCE(nazione, 'IT'), pec_destinatario,
                   COALESCE(codice_destinatario, '0000000'), payment_terms, COALESCE(active, TRUE)
            FROM customers
        """))
    # Fornitori con la stessa P.IVA diventano lo STESSO soggetto del cliente.
    if 'vendors' in tables:
        bind.execute(sa.text("""
            UPDATE economic_subjects SET is_supplier = TRUE
            WHERE piva IS NOT NULL AND piva <> ''
              AND piva IN (SELECT piva FROM vendors WHERE piva IS NOT NULL AND piva <> '')
        """))
        bind.execute(sa.text("""
            INSERT INTO economic_subjects
                (code, name, subject_type, is_customer, is_supplier, piva, payment_terms, active)
            SELECT 'F-' || v.id, v.name, 'azienda', FALSE, TRUE, v.piva, v.payment_terms, COALESCE(v.active, TRUE)
            FROM vendors v
            WHERE v.piva IS NULL OR v.piva = ''
               OR NOT EXISTS (SELECT 1 FROM economic_subjects s WHERE s.piva = v.piva)
        """))
    # Collega lo storico. Per omonimi senza P.IVA il matching è volutamente
    # conservativo: rimangono due soggetti finché l'utente non li unifica.
    if 'journal_entries' in tables:
        bind.execute(sa.text("""UPDATE journal_entries SET economic_subject_id =
            (SELECT id FROM economic_subjects WHERE code = 'C-' || journal_entries.customer_id)
            WHERE customer_id IS NOT NULL"""))
        bind.execute(sa.text("""UPDATE journal_entries SET economic_subject_id =
            (SELECT id FROM economic_subjects WHERE code = 'F-' || journal_entries.vendor_id)
            WHERE economic_subject_id IS NULL AND vendor_id IS NOT NULL"""))
        bind.execute(sa.text("""UPDATE journal_entries SET economic_subject_id =
            (SELECT id FROM economic_subjects WHERE piva = (SELECT piva FROM vendors WHERE id = journal_entries.vendor_id))
            WHERE economic_subject_id IS NULL AND vendor_id IS NOT NULL"""))
    for table in ('quotations', 'sales_orders', 'deliveries'):
        if table in tables:
            bind.execute(sa.text(f"""UPDATE {table} SET economic_subject_id =
                (SELECT id FROM economic_subjects WHERE code = 'C-' || {table}.customer_id)
                WHERE customer_id IS NOT NULL"""))
    if 'purchase_orders' in tables:
        bind.execute(sa.text("""UPDATE purchase_orders SET economic_subject_id =
            (SELECT id FROM economic_subjects WHERE code = 'F-' || purchase_orders.vendor_id)
            WHERE vendor_id IS NOT NULL"""))
        bind.execute(sa.text("""UPDATE purchase_orders SET economic_subject_id =
            (SELECT id FROM economic_subjects WHERE piva = (SELECT piva FROM vendors WHERE id = purchase_orders.vendor_id))
            WHERE economic_subject_id IS NULL AND vendor_id IS NOT NULL"""))


def downgrade():
    for table in ('purchase_orders', 'deliveries', 'sales_orders', 'quotations', 'journal_entries'):
        if table in _tables():
            op.drop_column(table, 'economic_subject_id')
    op.drop_index('ix_economic_subjects_piva', table_name='economic_subjects')
    op.drop_table('economic_subjects')
