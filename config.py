import os
from datetime import timedelta

basedir = os.path.abspath(os.path.dirname(__file__))


class Config:
    """
    Configurazione dell'applicazione.

    In locale, senza nessuna variabile d'ambiente, gira su SQLite (file
    masterledger.db nella cartella del progetto) — zero setup per provarla subito.

    Su Railway, basta collegare un plugin Postgres: Railway espone
    automaticamente la variabile DATABASE_URL, che questa configurazione
    legge da sola (con la piccola correzione "postgres://" -> "postgresql://"
    richiesta da SQLAlchemy 1.4+).
    """
    SECRET_KEY = os.environ.get("SECRET_KEY", "cambia-questa-chiave-in-produzione")

    _database_url = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(basedir, 'masterledger.db')}")
    if _database_url.startswith("postgres://"):
        _database_url = _database_url.replace("postgres://", "postgresql://", 1)
    # Il progetto installa psycopg2-binary (non psycopg 3). SQLAlchemy 2.x,
    # con un URL "postgresql://" generico senza driver esplicito, può
    # risolvere il dialetto sul driver psycopg (v3) se lo trova prima nella
    # ricerca degli entry point, anche quando non è affatto installato:
    # il risultato è un ModuleNotFoundError: No module named 'psycopg' in
    # avvio/migrazioni, non un errore di connessione. Per non dipendere da
    # quale driver risulti installato in un dato ambiente, l'URL viene reso
    # esplicito qui una volta per tutte: postgresql+psycopg2://.
    if _database_url.startswith("postgresql://"):
        _database_url = _database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    SQLALCHEMY_DATABASE_URI = _database_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Nome Codice azienda / Cliente — configurabile per riutilizzare l'app con più clienti
    COMPANY_CODE = os.environ.get("COMPANY_CODE", "1000")
    COMPANY_NAME = os.environ.get("COMPANY_NAME", "IRON APPALTI")

    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    REMEMBER_COOKIE_DURATION = timedelta(hours=8)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
    # Un form di Prima Nota/Preventivo con molte righe può restare aperto a
    # lungo (interruzioni, dati da recuperare altrove) prima dell'invio. Il
    # default di Flask-WTF è 1 ora — troppo poco per un uso reale, causa
    # "CSRF token has expired" su form legittimi. Allineato alla durata
    # della sessione stessa (8 ore): il token resta valido finché resta
    # valida la sessione dell'utente, non prima.
    WTF_CSRF_TIME_LIMIT = 8 * 60 * 60
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    BOOTSTRAP_DEMO_USERS = os.environ.get("BOOTSTRAP_DEMO_USERS", "false").lower() in ("1", "true", "yes")

    # Chiave API OpenAI per il suggerimento AI delle scritture di Prima Nota
    # (facoltativa: se assente, il pulsante "Suggerisci con AI" mostra un
    # errore chiaro invece di rompere il resto dell'applicazione).
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

    # URL base del servizio MasterLogistic-WMS (fonte di verità per le giacenze).
    # Es. https://masterlogistic-wms-production.up.railway.app — SENZA slash finale.
    # Se assente, tutte le operazioni che leggono/scrivono giacenza restano bloccate
    # con un errore chiaro invece di proseguire silenziosamente con dati sbagliati.
    MASTERLOGISTIC_URL = os.environ.get("MASTERLOGISTIC_URL", "").rstrip("/")

    # Canale di inoltro RFQ: configura questi valori nell'ambiente di produzione.
    RFQ_SMTP_HOST = os.environ.get("RFQ_SMTP_HOST", "")
    RFQ_SMTP_PORT = int(os.environ.get("RFQ_SMTP_PORT", "587"))
    RFQ_SMTP_USERNAME = os.environ.get("RFQ_SMTP_USERNAME", "")
    RFQ_SMTP_PASSWORD = os.environ.get("RFQ_SMTP_PASSWORD", "")
    RFQ_SMTP_USE_TLS = os.environ.get("RFQ_SMTP_USE_TLS", "true").lower() in ("1", "true", "yes")
    RFQ_FROM_EMAIL = os.environ.get("RFQ_FROM_EMAIL", "")
    RFQ_FROM_NAME = os.environ.get("RFQ_FROM_NAME", COMPANY_NAME)
