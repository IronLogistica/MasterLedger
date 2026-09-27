"""Correzione storia migrazioni #4 — FK mancante su economic_subject_id e
pulizia conservativa di customer_id/vendor_id/customers/vendors.

Contesto (verificato su Postgres, non solo dedotto dal codice):
"flask db check" contro uno schema realmente migrato segnala che il
modello JournalEntry.economic_subject_id non ha mai avuto un vero vincolo
di chiave esterna nel database: la migrazione d4e5f6a7b8c9 aveva aggiunto
la colonna con "op.add_column(...)" senza mai dichiarare la
ForeignKeyConstraint verso economic_subjects. Nel frattempo le vecchie
colonne "customer_id"/"vendor_id" (e le tabelle "customers"/"vendors" da
cui derivano) restano nello schema ma non hanno più alcun modello né
alcun codice applicativo che le scriva o le legga: sono la sola vera
divergenza residua fra ORM e schema migrato per questa parte (le tabelle
quotations/sales_orders/deliveries/purchase_orders erano già state
corrette dalle migrazioni k6l7m8n9o0p1/k7l8m9n0o1p2).

Questa migrazione:

 1. Aggiunge il vincolo di chiave esterna mancante
    journal_entries.economic_subject_id -> economic_subjects.id.

 2. PRIMA di toccare qualunque colonna/tabella legacy, verifica riga per
    riga che ogni journal_entries con customer_id o vendor_id valorizzato
    abbia GIA' anche economic_subject_id valorizzato (cioè che il backfill
    della d4e5f6a7b8c9 sia stato completo). Se anche una sola riga non è
    stata migrata, la migrazione si ferma con un errore esplicito e non
    tocca nulla: è una scelta deliberata, per non rischiare di perdere il
    collegamento storico di una scrittura contabile reale. In quel caso va
    prima sistemato il dato (a mano o con uno script dedicato), poi
    rilanciata questa migrazione.

 3. Solo se la verifica passa, toglie le colonne "customer_id"/"vendor_id"
    (ormai ridondanti: il collegamento vero è economic_subject_id) da
    journal_entries.

 4. Solo se, dopo il punto 3, nessun'altra tabella referenzia più
    "customers"/"vendors" (verificato via information_schema, non
    assunto), ed ogni riga di "customers"/"vendors" risulta già presente
    in economic_subjects (stesso controllo di quadratura), elimina le due
    tabelle legacy. Se anche un solo soggetto non risultasse riconciliato,
    le tabelle vengono lasciate al loro posto e va indagato a mano.

Il downgrade non ricrea customer_id/vendor_id/customers/vendors: come le
correzioni precedenti sullo stesso tema, non si vuole reintrodurre uno
stato che ha già causato incidenti. Toglie solo la FK aggiunta al punto 1.

Revision ID: w1x2y3z4a5b6
Revises: v0w1x2y3z4a5
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'w1x2y3z4a5b6'
down_revision = 'v0w1x2y3z4a5'
branch_labels = None
depends_on = None

FK_NAME = "fk_journal_entries_economic_subject_id"


def _fk_exists(insp, table, name):
    return any(fk["name"] == name for fk in insp.get_foreign_keys(table))


def _referencing_tables(bind, target_table):
    """Tabelle che hanno ancora una FK verso target_table, lette dal
    database reale (information_schema), non dal modello ORM."""
    rows = bind.execute(sa.text("""
        SELECT tc.table_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name
         AND tc.table_schema = ccu.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND ccu.table_name = :target
    """), {"target": target_table}).fetchall()
    return {r[0] for r in rows}


def upgrade():
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())

    if "journal_entries" not in tables:
        return  # ambiente senza questa tabella (non dovrebbe succedere): no-op sicuro

    je_cols = {c["name"] for c in insp.get_columns("journal_entries")}

    # --- 1. FK mancante su economic_subject_id ---------------------------
    if "economic_subject_id" in je_cols and "economic_subjects" in tables:
        if not _fk_exists(insp, "journal_entries", FK_NAME):
            with op.batch_alter_table("journal_entries") as batch:
                batch.create_foreign_key(
                    FK_NAME, "economic_subjects",
                    ["economic_subject_id"], ["id"],
                )

    # --- 2/3. Pulizia legacy customer_id/vendor_id su journal_entries ----
    legacy_cols = [c for c in ("customer_id", "vendor_id") if c in je_cols]
    if legacy_cols and "economic_subject_id" in je_cols:
        unmigrated = bind.execute(sa.text(f"""
            SELECT COUNT(*) FROM journal_entries
            WHERE economic_subject_id IS NULL
              AND ({' OR '.join(f"{c} IS NOT NULL" for c in legacy_cols)})
        """)).scalar()
        if unmigrated:
            raise RuntimeError(
                f"Migrazione w1x2y3z4a5b6 interrotta: {unmigrated} riga/e di "
                f"journal_entries hanno ancora customer_id/vendor_id valorizzato "
                f"ma economic_subject_id NULL. Il backfill verso economic_subjects "
                f"non risulta completo: va sistemato il dato prima di eliminare le "
                f"colonne legacy. Nessuna modifica è stata applicata."
            )
        with op.batch_alter_table("journal_entries") as batch:
            for c in legacy_cols:
                batch.drop_column(c)

        # --- 4. Tabelle customers/vendors: eliminale solo se innocue ----
        tables = set(inspect(bind).get_table_names())  # ricalcola dopo il drop colonne
        for legacy_table, prefix in (("customers", "C-"), ("vendors", "F-")):
            if legacy_table not in tables:
                continue
            still_referenced = _referencing_tables(bind, legacy_table)
            if still_referenced:
                continue  # qualcosa la referenzia ancora: non si tocca, va indagato a mano
            if "economic_subjects" not in tables:
                continue
            orphans = bind.execute(sa.text(f"""
                SELECT COUNT(*) FROM {legacy_table} t
                WHERE NOT EXISTS (
                    SELECT 1 FROM economic_subjects s WHERE s.code = :prefix || t.id
                )
                AND (t.piva IS NULL OR t.piva = '' OR NOT EXISTS (
                    SELECT 1 FROM economic_subjects s2 WHERE s2.piva = t.piva
                ))
            """), {"prefix": prefix}).scalar()
            if orphans:
                continue  # soggetti non riconciliati in economic_subjects: si lascia la tabella
            op.drop_table(legacy_table)


def downgrade():
    bind = op.get_bind()
    insp = inspect(bind)
    if "journal_entries" in insp.get_table_names() and _fk_exists(insp, "journal_entries", FK_NAME):
        with op.batch_alter_table("journal_entries") as batch:
            batch.drop_constraint(FK_NAME, type_="foreignkey")
    # Deliberatamente no-op per il resto: non si ricreano colonne/tabelle
    # legacy già eliminate, per lo stesso motivo delle correzioni precedenti.
