"""Correzione storia migrazioni #3 — la migrazione precedente
(k6l7m8n9o0p1) saltava la tabella se trovava già una colonna
"economic_subject_id", assumendo che significasse "già a posto". Sbagliato:
in produzione ENTRAMBE le colonne coesistono — "customer_id"/"vendor_id"
(legacy, ancora NOT NULL, mai più scritta da nessun codice) accanto a
"economic_subject_id" (quella vera, correttamente scritta dall'app). La
colonna legacy orfana continua a bloccare ogni INSERT con NotNullViolation.

Qui si toglie il vincolo NOT NULL dalla colonna legacy, SE esiste, a
prescindere dal fatto che economic_subject_id esista già o no — copre
in un colpo solo sia l'ambiente dove la k6l7m8n9o0p1 ha rinominato
correttamente (nessuna colonna legacy rimasta, questa migrazione è un
no-op) sia l'ambiente reale di produzione dove è rimasta orfana.

Si toglie SOLO il vincolo NOT NULL (non si elimina la colonna): è la
correzione minima che ferma subito l'errore, senza cancellare una colonna
che potrebbe ancora contenere dati storici da valutare con calma.

Revision ID: k7l8m9n0o1p2
Revises: k6l7m8n9o0p1
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'k7l8m9n0o1p2'
down_revision = 'k6l7m8n9o0p1'
branch_labels = None
depends_on = None

# (tabella, nome colonna legacy da liberare dal NOT NULL)
LEGACY_COLUMNS = [
    ("quotations", "customer_id"),
    ("sales_orders", "customer_id"),
    ("deliveries", "customer_id"),
    ("purchase_orders", "vendor_id"),
]


def upgrade():
    b = op.get_bind()
    insp = inspect(b)
    existing_tables = set(insp.get_table_names())

    for table, legacy_col in LEGACY_COLUMNS:
        if table not in existing_tables:
            continue
        cols = {c["name"]: c for c in insp.get_columns(table)}
        if legacy_col not in cols:
            continue  # non c'è (già rinominata correttamente, o mai esistita) — no-op
        if cols[legacy_col]["nullable"]:
            continue  # già senza vincolo — no-op
        with op.batch_alter_table(table) as batch:
            batch.alter_column(legacy_col, existing_type=sa.Integer(), nullable=True)


def downgrade():
    # No-op deliberato, come le correzioni precedenti sullo stesso tema:
    # non vogliamo reintrodurre un vincolo che ha già causato un incidente.
    pass
