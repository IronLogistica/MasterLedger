"""Produzione Completata (COGM) — soluzione ponte finché non c'è MasterProduction.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-19
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'production_entries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('doc_number', sa.String(length=20), nullable=False, unique=True),
        sa.Column('doc_date', sa.Date(), nullable=False),
        sa.Column('period_label', sa.String(length=30), nullable=True),
        sa.Column('material_id', sa.Integer(), sa.ForeignKey('materials.id'), nullable=False),
        sa.Column('qty_produced', sa.Numeric(14, 3), nullable=False),
        sa.Column('raw_material_cost', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('direct_labor_cost', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('overhead_cost', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('notes', sa.String(length=300), nullable=True),
        sa.Column('journal_entry_id', sa.Integer(), sa.ForeignKey('journal_entries.id'), nullable=True),
        sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    # Conto "Variazione Rimanenze Prodotti Finiti" — se il seed non è già
    # stato rilanciato, lo creiamo qui così la registrazione non fallisce
    # per conto mancante anche su chi non rilancia `flask seed`.
    conn = op.get_bind()
    esiste = conn.execute(sa.text("SELECT 1 FROM accounts WHERE code = '430000'")).fetchone()
    if not esiste:
        conn.execute(sa.text("""
            INSERT INTO accounts (code, name, account_type, cost_relevant, cost_relevant_type, active)
            VALUES ('430000', 'Variazione Rimanenze Prodotti Finiti', 'ricavo', true, 'REVENUE', true)
        """))

    # Numerazione documenti per "Produzione Completata" (tipo PR, prefisso 40)
    esiste_seq = conn.execute(sa.text("SELECT 1 FROM document_sequences WHERE doc_type = 'PR'")).fetchone()
    if not esiste_seq:
        conn.execute(sa.text("""
            INSERT INTO document_sequences (doc_type, prefix, current_number)
            VALUES ('PR', '40', 0)
        """))


def downgrade():
    op.drop_table('production_entries')
