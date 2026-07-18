"""
app.py — Punto di ingresso dell'applicazione MasterLedger.

Usa l'application factory pattern: create_app() costruisce e configura
l'app Flask, registra le estensioni (db, login, migrate) e i Blueprint
(uno per area funzionale, come richiesto: GL, AP, AR, Cespiti, Costi,
Setup Magazzini, Autenticazione).

Per avviare in locale:
    flask --app app run --debug

Per Railway: il Procfile lancia gunicorn app:app (vedi Procfile).
"""
import os
from flask import Flask, render_template
from flask_login import current_user

from config import Config
from extensions import db, migrate, login_manager


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # ── Estensioni ──────────────────────────────────────────────
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    # ── Modelli (import qui per evitare cicli di import) ───────
    from models import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # ── Blueprint — un modulo per area funzionale ───────────────
    from blueprints.auth.routes import auth_bp
    from blueprints.dashboard.routes import dashboard_bp
    from blueprints.gl.routes import gl_bp
    from blueprints.ap.routes import ap_bp
    from blueprints.ar.routes import ar_bp
    from blueprints.assets.routes import assets_bp
    from blueprints.warehouse.routes import warehouse_bp
    from blueprints.costs.routes import costs_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(dashboard_bp, url_prefix="/")
    app.register_blueprint(gl_bp, url_prefix="/gl")
    app.register_blueprint(ap_bp, url_prefix="/ap")
    app.register_blueprint(ar_bp, url_prefix="/ar")
    app.register_blueprint(assets_bp, url_prefix="/assets")
    app.register_blueprint(warehouse_bp, url_prefix="/warehouse")
    app.register_blueprint(costs_bp, url_prefix="/costs")

    # ── Variabili disponibili in ogni template ──────────────────
    @app.context_processor
    def inject_globals():
        return {
            "company_name": app.config["COMPANY_NAME"],
            "company_code": app.config["COMPANY_CODE"],
        }

    # ── Comando CLI per popolare il database con dati di partenza ──
    @app.cli.command("seed")
    def seed():
        """Popola il database con Piano dei Conti, utenti demo, OperatingSite di esempio.
        Uso: flask --app app seed
        """
        from seed import run_seed
        run_seed()
        print("Database popolato con dati di partenza.")

    # ── Bootstrap automatico (Railway): crea tabelle + utenti garantiti ──
    with app.app_context():
        try:
            db.create_all()
            for uname, pwd, role in (
                ("Angelo", "Angelo1234", "operatore"),
                ("Maurizio", "Maurizio1234", "commercialista"),
            ):
                u = User.query.filter(db.func.lower(User.username) == uname.lower()).first()
                if u is None:
                    u = User(username=uname, full_name=uname, role=role)
                    db.session.add(u)
                # Garantisce che la password sia sempre quella prevista
                u.set_password(pwd)
                u.is_active_flag = True
            db.session.commit()
        except Exception:
            db.session.rollback()

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
