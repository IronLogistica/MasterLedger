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
import click
from flask import Flask, render_template
from flask_login import current_user

from config import Config
from extensions import db, migrate, login_manager, csrf


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # ── Estensioni ──────────────────────────────────────────────
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

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
    from blueprints.sd.routes import sd_bp
    from blueprints.mm.routes import mm_bp
    from blueprints.production.routes import production_bp
    from blueprints.materials.routes import materials_bp
    from blueprints.logistics.routes import logistics_bp
    from blueprints.parties.routes import parties_bp
    from blueprints.payroll.routes import payroll_bp
    from blueprints.operations.routes import operations_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(dashboard_bp, url_prefix="/")
    app.register_blueprint(gl_bp, url_prefix="/gl")
    app.register_blueprint(ap_bp, url_prefix="/ap")
    app.register_blueprint(ar_bp, url_prefix="/ar")
    app.register_blueprint(assets_bp, url_prefix="/assets")
    app.register_blueprint(warehouse_bp, url_prefix="/warehouse")
    app.register_blueprint(costs_bp, url_prefix="/costs")
    app.register_blueprint(sd_bp, url_prefix="/sd")
    app.register_blueprint(mm_bp, url_prefix="/mm")
    app.register_blueprint(production_bp, url_prefix="/produzione")
    app.register_blueprint(materials_bp, url_prefix="/materials")
    app.register_blueprint(logistics_bp, url_prefix="/logistics")
    app.register_blueprint(parties_bp, url_prefix="/soggetti-economici")
    app.register_blueprint(payroll_bp, url_prefix="/paghe")
    app.register_blueprint(operations_bp, url_prefix="/produzione-operativa")

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

    # ── Comando CLI per svuotare COMPLETAMENTE il database ──
    # Pensato per ripartire da zero con le prime registrazioni di prova,
    # senza dover distruggere/ricreare il database Postgres su Railway (più
    # lento e rischia di disallineare le variabili d'ambiente di connessione).
    # SVUOTA OGNI TABELLA — irreversibile, richiede conferma esplicita.
    @app.cli.command("reset-db")
    @click.option("--yes-i-am-sure", is_flag=True,
                 help="Conferma esplicita: senza questo flag il comando non fa nulla.")
    def reset_db(yes_i_am_sure):
        """SVUOTA TUTTE LE TABELLE del database (irreversibile).
        Uso: flask --app app reset-db --yes-i-am-sure
        Dopo, rilancia 'flask --app app seed' per ripopolare piano dei conti,
        utenti e dati di partenza — il reset da solo lascia tutto vuoto.
        """
        if not yes_i_am_sure:
            print("ATTENZIONE: questo comando CANCELLA TUTTI I DATI dal database — ")
            print("fatture, pagamenti, scritture, anagrafiche, TUTTO. Irreversibile.")
            print("Se sei sicuro, rilancia con: flask --app app reset-db --yes-i-am-sure")
            return

        with app.app_context():
            engine = db.engine
            table_names = [t.name for t in db.metadata.sorted_tables]
            with engine.begin() as conn:
                if engine.dialect.name == "postgresql":
                    if table_names:
                        conn.execute(db.text(
                            f"TRUNCATE TABLE {', '.join(table_names)} RESTART IDENTITY CASCADE;"
                        ))
                else:
                    # SQLite (solo per test in locale) — ordine inverso per rispettare le FK.
                    for name in reversed(table_names):
                        conn.execute(db.text(f"DELETE FROM {name};"))
                    has_sequence = conn.execute(db.text(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence';"
                    )).fetchone()
                    if has_sequence:
                        conn.execute(db.text(
                            "DELETE FROM sqlite_sequence WHERE name IN "
                            f"({','.join(repr(n) for n in table_names)});"
                        ))
        print(f"Database svuotato: {len(table_names)} tabelle azzerate.")
        print("Ora rilancia: flask --app app seed")

    # ── Comando CLI: reset MANTENENDO anagrafica partner + piano dei conti ──
    # Svuota tutto il resto (articoli, BOM, cicli, magazzino, scritture,
    # documenti SD/MM/produzione/paghe/cespiti/banca, parametri fiscali,
    # sedi/aree magazzino, sequenze documento, periodi contabili) ma NON
    # tocca economic_subjects (clienti/fornitori), accounts (piano dei
    # conti), account_mappings, users (altrimenti nessuno potrebbe più fare
    # login), cost_centers (centri di costo) né work_centers (centri di
    # lavoro/produzione). Pensato per ripartire puliti sui dati operativi
    # tenendo anagrafica, piano dei conti e struttura organizzativa già
    # validati.
    TABELLE_DA_MANTENERE = {"economic_subjects", "accounts", "users", "account_mappings",
                            "cost_centers", "work_centers"}

    @app.cli.command("reset-mantieni-anagrafica")
    @click.option("--yes-i-am-sure", is_flag=True,
                 help="Conferma esplicita: senza questo flag il comando non fa nulla.")
    def reset_mantieni_anagrafica(yes_i_am_sure):
        """Svuota TUTTO tranne anagrafica partner, piano dei conti, mappatura
        conti, centri di costo, centri di lavoro e utenti (irreversibile).
        Uso: flask --app app reset-mantieni-anagrafica --yes-i-am-sure
        """
        if not yes_i_am_sure:
            print("ATTENZIONE: questo comando CANCELLA articoli, BOM, cicli di lavorazione, magazzino,")
            print("scritture contabili, documenti SD/MM/produzione/paghe/cespiti/banca, parametri")
            print("fiscali, sedi/aree magazzino, sequenze documento e periodi contabili. RESTANO:")
            print("anagrafica clienti/fornitori, piano dei conti, mappatura conti, centri di costo,")
            print("centri di lavoro e utenti. Irreversibile.")
            print("Se sei sicuro, rilancia con: flask --app app reset-mantieni-anagrafica --yes-i-am-sure")
            return

        with app.app_context():
            engine = db.engine
            table_names = [t.name for t in db.metadata.sorted_tables if t.name not in TABELLE_DA_MANTENERE]
            with engine.begin() as conn:
                if engine.dialect.name == "postgresql":
                    if table_names:
                        conn.execute(db.text(
                            f"TRUNCATE TABLE {', '.join(table_names)} RESTART IDENTITY CASCADE;"
                        ))
                else:
                    for name in reversed(table_names):
                        conn.execute(db.text(f"DELETE FROM {name};"))
                    has_sequence = conn.execute(db.text(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence';"
                    )).fetchone()
                    if has_sequence:
                        conn.execute(db.text(
                            "DELETE FROM sqlite_sequence WHERE name IN "
                            f"({','.join(repr(n) for n in table_names)});"
                        ))
        print(f"Database svuotato: {len(table_names)} tabelle azzerate, "
              f"{len(TABELLE_DA_MANTENERE)} mantenute ({', '.join(sorted(TABELLE_DA_MANTENERE))}).")
        print("Ora rilancia: flask --app app seed-esempio-costo-pieno per creare 2 prodotti di prova con BOM+Routing.")

    # ── Comando CLI: 2 prodotti di esempio con BOM + Routing + centro di ──
    # lavoro, per testare subito il costo pieno dopo un reset-mantieni-
    # anagrafica (o su un database nuovo). Usa i codici conto magazzino del
    # piano dei conti standard (150000/155000/160000) — presuppone quindi che
    # il piano dei conti sia già presente (seed oppure reset-mantieni-anagrafica).
    @app.cli.command("seed-esempio-costo-pieno")
    def seed_esempio_costo_pieno():
        """Crea 2 prodotti finiti di esempio con BOM + Ciclo di Lavorazione +
        Centro di Lavoro, per testare subito il costo pieno (metodo SAP).
        Uso: flask --app app seed-esempio-costo-pieno
        """
        from decimal import Decimal
        from models import Material, BillOfMaterial, BOMComponent, WorkCenter, Routing, RoutingOperation, CostCenter

        with app.app_context():
            cc = CostCenter.query.filter_by(code="CC-PROD-01").first()
            if cc is None:
                cc = CostCenter(code="CC-PROD-01", name="Produzione")
                db.session.add(cc)
                db.session.flush()

            taglio = WorkCenter.query.filter_by(code="TAGLIO-01").first()
            if taglio is None:
                taglio = WorkCenter(code="TAGLIO-01", description="Taglio e piegatura",
                                    cost_center_id=cc.id, capacity_hours_month=Decimal("160"),
                                    hourly_rate_labor=Decimal("18"))
                db.session.add(taglio)
            assemblaggio = WorkCenter.query.filter_by(code="ASSEMBLAGGIO-01").first()
            if assemblaggio is None:
                assemblaggio = WorkCenter(code="ASSEMBLAGGIO-01", description="Assemblaggio e confezionamento",
                                          cost_center_id=cc.id, capacity_hours_month=Decimal("160"),
                                          hourly_rate_labor=Decimal("16"))
                db.session.add(assemblaggio)
            db.session.flush()

            def _articolo(code, description, material_type, standard_cost, sales_price, is_carp=False):
                m = Material.query.filter_by(code=code).first()
                if m is None:
                    m = Material(code=code, description=description, material_type=material_type,
                                uom="PZ", standard_cost=standard_cost, sales_price=sales_price,
                                vat_rate=Decimal("22"), qty_on_hand=Decimal("0"),
                                is_carpenteria_propria=is_carp)
                    db.session.add(m)
                    db.session.flush()
                return m

            # Materie prime dei due prodotti di esempio
            lamiera = _articolo("ES-LAMIERA", "Lamiera zincata 2mm", "ROH", Decimal("12"), Decimal("0"))
            vite = _articolo("ES-VITE", "Vite autofilettante 6x40", "ROH", Decimal("0.35"), Decimal("0"))
            palo = _articolo("ES-PALO", "Palo zincato 3m", "ROH", Decimal("28"), Decimal("0"))
            staffa = _articolo("ES-STAFFA", "Staffa di fissaggio", "ROH", Decimal("4.50"), Decimal("0"))

            # Prodotto 1: cartello stradale
            cartello = _articolo("ES-CARTELLO-01", "Cartello stradale triangolare 90cm", "FERT",
                                 Decimal("0"), Decimal("95"), is_carp=True)
            bom1 = BillOfMaterial.query.filter_by(parent_material_id=cartello.id, active=True).first()
            if bom1 is None:
                bom1 = BillOfMaterial(parent_material_id=cartello.id, version="1", active=True,
                                      notes="Esempio seed-esempio-costo-pieno")
                db.session.add(bom1); db.session.flush()
                db.session.add_all([
                    BOMComponent(bom_id=bom1.id, component_material_id=lamiera.id, qty_per=Decimal("1"), scrap_pct=Decimal("5")),
                    BOMComponent(bom_id=bom1.id, component_material_id=vite.id, qty_per=Decimal("6"), scrap_pct=Decimal("0")),
                ])
            routing1 = Routing.query.filter_by(parent_material_id=cartello.id, active=True).first()
            if routing1 is None:
                routing1 = Routing(parent_material_id=cartello.id, version="1", active=True,
                                   notes="Esempio seed-esempio-costo-pieno")
                db.session.add(routing1); db.session.flush()
                db.session.add_all([
                    RoutingOperation(routing_id=routing1.id, seq=10, work_center_id=taglio.id,
                                     description="Taglio e piegatura lamiera",
                                     machine_time_min=Decimal("8"), labor_time_min=Decimal("4")),
                    RoutingOperation(routing_id=routing1.id, seq=20, work_center_id=assemblaggio.id,
                                     description="Assemblaggio e confezionamento",
                                     machine_time_min=Decimal("0"), labor_time_min=Decimal("5")),
                ])

            # Prodotto 2: segnale su palo
            segnale = _articolo("ES-SEGNALE-01", "Segnale su palo con staffa", "FERT",
                                Decimal("0"), Decimal("140"), is_carp=True)
            bom2 = BillOfMaterial.query.filter_by(parent_material_id=segnale.id, active=True).first()
            if bom2 is None:
                bom2 = BillOfMaterial(parent_material_id=segnale.id, version="1", active=True,
                                      notes="Esempio seed-esempio-costo-pieno")
                db.session.add(bom2); db.session.flush()
                db.session.add_all([
                    BOMComponent(bom_id=bom2.id, component_material_id=cartello.id, qty_per=Decimal("1"), scrap_pct=Decimal("0")),
                    BOMComponent(bom_id=bom2.id, component_material_id=palo.id, qty_per=Decimal("1"), scrap_pct=Decimal("0")),
                    BOMComponent(bom_id=bom2.id, component_material_id=staffa.id, qty_per=Decimal("2"), scrap_pct=Decimal("0")),
                ])
            routing2 = Routing.query.filter_by(parent_material_id=segnale.id, active=True).first()
            if routing2 is None:
                routing2 = Routing(parent_material_id=segnale.id, version="1", active=True,
                                   notes="Esempio seed-esempio-costo-pieno")
                db.session.add(routing2); db.session.flush()
                db.session.add(
                    RoutingOperation(routing_id=routing2.id, seq=10, work_center_id=assemblaggio.id,
                                     description="Fissaggio segnale su palo",
                                     machine_time_min=Decimal("0"), labor_time_min=Decimal("12")),
                )

            db.session.commit()
        print("Creati: 1 centro di costo, 2 centri di lavoro (TAGLIO-01, ASSEMBLAGGIO-01),")
        print("4 materie prime, 2 prodotti finiti con BOM+Routing attivi (ES-CARTELLO-01, ES-SEGNALE-01).")
        print("Manca ancora il pool overhead del mese, assegnato ai centri, in 'Pool Overhead Reparto' —")
        print("senza quello la tariffa oraria di overhead resta 0 e 'Costo Standard' lo segnala come avviso.")

    # ── Bootstrap automatico (Railway): SOLO conti/utenti garantiti ──
    # FIX (19/07/2026): qui c'era anche un db.create_all() automatico ad ogni
    # avvio. Il problema: create_all() crea le tabelle NUOVE che mancano, ma
    # non tocca MAI le tabelle già esistenti per aggiungere colonne — quindi
    # ogni volta che una migrazione aggiungeva un campo a una tabella già
    # esistente (es. materials.is_carpenteria_propria), create_all() la
    # "nascondeva" creando nel frattempo le tabelle nuove senza che
    # alembic_version avanzasse mai, lasciando lo schema in uno stato
    # incoerente e imprevedibile. Da ora lo schema lo gestisce SOLO
    # `flask db upgrade` (già eseguito automaticamente da Railway nel passo
    # "release" del Procfile) — niente più scorciatoie qui.
    with app.app_context():
        try:
            from models import Account
            for code, name, atype, co_rel, co_type in (
                ("450000", "Costo del Venduto", "costo", True, "COST"),
                ("165000", "Ricevimenti da fatturare (EM/RF)", "patrimoniale_passivo", False, None),
            ):
                if not Account.query.filter_by(code=code).first():
                    db.session.add(Account(code=code, name=name, account_type=atype,
                                           cost_relevant=co_rel, cost_relevant_type=co_type))
            # Gli utenti demo sono opt-in e vengono creati una sola volta: non si
            # sovrascrivono mai password o stato di utenze già presenti.
            if app.config.get("BOOTSTRAP_DEMO_USERS"):
                for uname, pwd, role in (
                    ("Angelo", "Angelo1234", "operatore"),
                    ("Maurizio", "Maurizio1234", "commercialista"),
                ):
                    if User.query.filter(db.func.lower(User.username) == uname.lower()).first() is None:
                        u = User(username=uname, full_name=uname, role=role)
                        u.set_password(pwd)
                        db.session.add(u)
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
