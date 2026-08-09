"""Correzione storia migrazioni #2 — le tabelle quotations, sales_orders,
deliveries, purchase_orders sono state create ad-hoc (vedi migrazione
j9k0l1m2n3o4) con colonne "customer_id"/"vendor_id" mai rinominate quando
il modello è stato refattorizzato su "economic_subject_id". Il modello e
il codice applicativo usano economic_subject_id da tempo, ma il database
di produzione (Postgres) ha ancora il nome storico — causa un
NotNullViolation su customer_id ogni volta che si crea un nuovo
Preventivo/Ordine/DDT, perché quella colonna non viene più valorizzata.

Rinomina IF NECESSARY: no-op se la colonna si chiama già
economic_subject_id (ambiente creato da zero con la migrazione
j9k0l1m2n3o4, che la crea già col nome giusto); rinomina se trova ancora
il nome storico.

Revision ID: k6l7m8n9o0p1
Revises: k5l6m7n8o9p0
"""
from alembic import op
from sqlalchemy import inspect

revision = 'k6l7m8n9o0p1'
down_revision = 'k5l6m7n8o9p0'
branch_labels = None
depends_on = None

# (tabella, nome storico da cercare, nome corretto atteso dal modello)
RENAMES = [
    ("quotations", "customer_id", "economic_subject_id"),
    ("sales_orders", "customer_id", "economic_subject_id"),
    ("deliveries", "customer_id", "economic_subject_id"),
    ("purchase_orders", "vendor_id", "economic_subject_id"),
]


def upgrade():
    b = op.get_bind()
    insp = inspect(b)
    existing_tables = set(insp.get_table_names())

    for table, old_name, new_name in RENAMES:
        if table not in existing_tables:
            continue  # tabella non ancora creata: la creerà j9k0l1m2n3o4 già col nome giusto
        cols = {c["name"] for c in insp.get_columns(table)}
        if new_name in cols:
            continue  # già corretto (ambiente creato da zero, o già sistemato)
        if old_name in cols:
            with op.batch_alter_table(table) as batch:
                batch.alter_column(old_name, new_column_name=new_name)


def downgrade():
    # Downgrade no-op deliberato: non vogliamo rischiare di rompere di
    # nuovo la produzione tornando al nome storico.
    pass
