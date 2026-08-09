from pathlib import Path
import pytest

from app import create_app
from config import Config
from extensions import db
from models import Account, AccountMapping, EconomicSubject, User


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "test-secret"
    BOOTSTRAP_DEMO_USERS = False


@pytest.fixture()
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        accounts = [
            ("018000", "Fondo ammortamento", "patrimoniale_passivo"),
            ("140000", "Crediti clienti", "patrimoniale_attivo"),
            ("154000", "IVA credito", "patrimoniale_attivo"),
            ("170000", "IVA debito", "patrimoniale_passivo"),
            ("180000", "Banca", "patrimoniale_attivo"),
            ("200000", "Cespiti", "patrimoniale_attivo"),
            ("210000", "Debiti fornitori", "patrimoniale_passivo"),
            ("310000", "Ricavi", "ricavo"),
            ("410000", "Acquisti", "costo"),
            ("520000", "Ammortamenti", "costo"),
            ("452000", "Abbuoni Passivi", "ricavo"),
            ("652000", "Abbuoni Attivi", "costo"),
        ]
        for code, name, typ in accounts:
            if not Account.query.filter_by(code=code).first():
                db.session.add(Account(code=code, name=name, account_type=typ))
        db.session.flush()

        # Piano dei conti canonico (Fase 1) — stessa mappatura di seed.py,
        # sui conti di test sopra. Senza questo, AccountMapping.get_or_error()
        # blocca ogni flusso AP/AR/Cespiti perché il concetto non è configurato.
        mapping_defaults = [
            ("banca_principale", "180000", "Banca", "banca"),
            ("iva_credito", "154000", "IVA credito", "iva"),
            ("iva_debito", "170000", "IVA debito", "iva"),
            ("crediti_clienti", "140000", "Crediti clienti", "clienti_fornitori"),
            ("debiti_fornitori", "210000", "Debiti fornitori", "clienti_fornitori"),
            ("cespiti_impianti", "200000", "Cespiti", "cespiti"),
            ("ammortamenti_costo", "520000", "Ammortamenti", "cespiti"),
            ("fondo_ammortamento", "018000", "Fondo ammortamento", "cespiti"),
            ("abbuoni_attivi", "652000", "Abbuoni attivi", "pagamenti"),
            ("abbuoni_passivi", "452000", "Abbuoni passivi", "pagamenti"),
        ]
        for concept_key, code, label, category in mapping_defaults:
            if not AccountMapping.query.filter_by(concept_key=concept_key).first():
                acc = Account.query.filter_by(code=code).first()
                db.session.add(AccountMapping(concept_key=concept_key, account_id=acc.id,
                                              label=label, category=category))
        user = User(username="tester", full_name="Tester", role="commercialista")
        user.set_password("secret")
        op = User(username="operator", full_name="Operator", role="operatore")
        op.set_password("secret")
        customer = EconomicSubject(code="C0001", name="Cliente", is_customer=True,
                                   piva="12345678901", codice_destinatario="0000000",
                                   indirizzo="Via Test 1", comune="Roma", cap="00100", provincia="RM", nazione="IT")
        supplier = EconomicSubject(code="F0001", name="Fornitore", is_supplier=True, piva="10987654321")
        supplier2 = EconomicSubject(code="F0002", name="Fornitore 2", is_supplier=True, piva="10987654322")
        db.session.add_all([user, op, customer, supplier, supplier2])
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def login(client, app):
    with app.app_context():
        uid = User.query.filter_by(username="tester").one().id
    with client.session_transaction() as session:
        session["_user_id"] = str(uid)
        session["_fresh"] = True
    return client


@pytest.fixture()
def account(app):
    def get(code):
        return Account.query.filter_by(code=code).one()
    return get
