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
    SQLALCHEMY_DATABASE_URI = _database_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Nome Codice azienda / Cliente — configurabile per riutilizzare l'app con più clienti
    COMPANY_CODE = os.environ.get("COMPANY_CODE", "1000")
    COMPANY_NAME = os.environ.get("COMPANY_NAME", "IRON SEGNALETICA")

    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    # Chiave API OpenAI per il suggerimento AI delle scritture di Prima Nota
    # (facoltativa: se assente, il pulsante "Suggerisci con AI" mostra un
    # errore chiaro invece di rompere il resto dell'applicazione).
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
