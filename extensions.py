"""
Istanze condivise delle estensioni Flask.

Vengono create qui (senza essere legate a nessuna app) e "agganciate"
dentro app.py con extension.init_app(app) — il pattern standard per
evitare import circolari tra i blueprint e il modulo dei modelli.
"""
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Effettua l'accesso per continuare."
login_manager.login_message_category = "warning"
