"""Test PostgreSQL dedicato.

La suite principale (tests/conftest.py) gira su SQLite in memoria con
db.create_all(): verifica la logica applicativa, ma non prova mai le
migrazioni Alembic reali né lo schema PostgreSQL effettivo — un audit ha
mostrato che "95 test verdi" convive con un errore di avvio (driver
psycopg mancante), un comando pytest che si arresta senza PYTHONPATH, e
una divergenza reale fra schema migrato e modelli ORM.

Questo file prova, su un vero PostgreSQL, con lo stesso meccanismo che usa
"flask db upgrade" in produzione (migrations/env.py legge l'URL dalla app
Flask corrente, non da un Config alembic passato a mano):

  1) le migrazioni Alembic applicano da database vuoto alla head;
  2) la catena di migrazioni ha una sola head;
  3) applicare la head due volte è idempotente;
  4) lo schema migrato NON diverge dai modelli ORM (equivalente
     automatizzato di "flask db check");
  5) l'app si avvia realmente su quel database e la pagina di login
     risponde.

Opt-in: richiede un PostgreSQL raggiungibile. Si attiva impostando
POSTGRES_TEST_DATABASE_URL (o, in mancanza, DATABASE_URL se punta già a
postgres) nell'ambiente; altrimenti l'intero modulo viene saltato con un
motivo esplicito, per non rompere l'esecuzione locale di chi non ha
Postgres installato. In CI va sempre eseguito (vedi
.github/workflows/tests.yml, che fornisce un servizio postgres e imposta
la variabile).

Ogni test crea un database Postgres dedicato e lo distrugge alla fine:
non tocca mai il database puntato dalla variabile d'ambiente, che serve
solo per sapere a quale SERVER Postgres connettersi (host/porta/credenziali).
"""
import os
import uuid

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.postgres

_RAW_URL = os.environ.get("POSTGRES_TEST_DATABASE_URL") or ""
if not _RAW_URL:
    _fallback = os.environ.get("DATABASE_URL", "")
    if _fallback.startswith("postgres"):
        _RAW_URL = _fallback

if not _RAW_URL:
    pytest.skip(
        "Nessun PostgreSQL disponibile per questo test: imposta "
        "POSTGRES_TEST_DATABASE_URL (es. postgresql://postgres:postgres@localhost:5432/postgres) "
        "per eseguirlo. Saltato di proposito, non fallito: la suite principale "
        "(SQLite) resta eseguibile senza Postgres installato.",
        allow_module_level=True,
    )

if _RAW_URL.startswith("postgres://"):
    _RAW_URL = _RAW_URL.replace("postgres://", "postgresql://", 1)

_SERVER_URL = sa.engine.make_url(_RAW_URL)


def _admin_engine():
    # Connessione al database di servizio, solo per creare/distruggere il
    # database di test usa-e-getta di ciascun test.
    return sa.create_engine(_SERVER_URL.set(database="postgres"), isolation_level="AUTOCOMMIT")


@pytest.fixture()
def pg_database_url():
    """Crea un database Postgres vuoto e dedicato per la durata del test,
    lo distrugge alla fine (anche se il test fallisce)."""
    db_name = f"masterledger_test_{uuid.uuid4().hex[:12]}"
    admin = _admin_engine()
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
    try:
        # NB: str(url) di SQLAlchemy nasconde la password ("***"). Va reso
        # esplicito con render_as_string(hide_password=False), altrimenti
        # ogni connessione successiva fallisce con "password authentication
        # failed" invece di usare davvero la password fornita.
        yield _SERVER_URL.set(database=db_name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f"""
                SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                WHERE datname = '{db_name}' AND pid <> pg_backend_pid()
            """))
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        admin.dispose()


def _make_pg_app(database_url):
    """App Flask configurata sul Postgres di test — stesso create_app()
    usato in produzione, non una app di comodo per i test."""
    import app as app_module

    class PgTestConfig:
        SQLALCHEMY_DATABASE_URI = database_url
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        TESTING = True
        WTF_CSRF_ENABLED = False
        SECRET_KEY = "test-secret"
        BOOTSTRAP_DEMO_USERS = False
        COMPANY_CODE = "1000"
        COMPANY_NAME = "TEST"
        MASTERLOGISTIC_URL = ""

    return app_module.create_app(PgTestConfig)


def _upgrade_to_head(flask_app):
    from flask_migrate import upgrade
    with flask_app.app_context():
        upgrade(directory="migrations")


def test_migrations_apply_cleanly_from_empty_database(pg_database_url):
    flask_app = _make_pg_app(pg_database_url)
    _upgrade_to_head(flask_app)

    engine = sa.create_engine(pg_database_url)
    insp = sa.inspect(engine)
    tables = set(insp.get_table_names())
    assert "journal_entries" in tables
    assert "economic_subjects" in tables
    # Le tabelle legacy, se presenti in un vecchio ambiente, vengono
    # eliminate solo dopo verifica di quadratura: su un database vuoto la
    # verifica passa banalmente e le tabelle legacy non devono sopravvivere.
    assert "customers" not in tables
    assert "vendors" not in tables
    engine.dispose()


def test_migration_chain_has_a_single_head():
    from flask_migrate import Migrate
    from alembic.script import ScriptDirectory
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig()
    cfg.set_main_option("script_location", "migrations")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, (
        f"La catena di migrazioni ha {len(heads)} head invece di 1: {heads}. "
        f"Serve una migrazione di merge prima di procedere."
    )


def test_upgrade_head_is_idempotent(pg_database_url):
    flask_app = _make_pg_app(pg_database_url)
    _upgrade_to_head(flask_app)

    engine = sa.create_engine(pg_database_url)
    with engine.connect() as conn:
        before = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()

    _upgrade_to_head(flask_app)  # seconda esecuzione: non deve fare nulla

    with engine.connect() as conn:
        after = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    assert before == after
    engine.dispose()


def test_migrated_schema_matches_orm_models(pg_database_url):
    """Equivalente automatizzato di "flask --app app db check": nessuna
    differenza fra ciò che i modelli SQLAlchemy dichiarano e ciò che le
    migrazioni hanno effettivamente creato nel database."""
    flask_app = _make_pg_app(pg_database_url)
    _upgrade_to_head(flask_app)

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from extensions import db

    with flask_app.app_context():
        engine = db.engine
        with engine.connect() as conn:
            mc = MigrationContext.configure(conn)
            diffs = compare_metadata(mc, db.metadata)

    assert diffs == [], (
        "Lo schema migrato diverge dai modelli ORM (drift rilevato da "
        f"compare_metadata, equivalente di 'flask db check'): {diffs}"
    )


def test_app_starts_and_login_page_responds_on_postgresql(pg_database_url):
    flask_app = _make_pg_app(pg_database_url)
    _upgrade_to_head(flask_app)

    client = flask_app.test_client()

    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (301, 302)

    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert b"form" in resp.data.lower()
