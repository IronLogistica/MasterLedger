"""Test di regressione — Turno 5 (servizi AI/fiscali)."""
from decimal import Decimal

from extensions import db
from models import Account, EconomicSubject
from services.posting import post_journal_entry
from services.fatturapa import _derive_invoice_amounts


def test_derive_invoice_amounts_rounds_half_up_not_bankers_rounding(app, account):
    """Bug: _derive_invoice_amounts sommava le righe con float invece di
    Decimal — stesso rischio già corretto in AP supplier_invoice(): un
    accumulo in binario può cadere sul lato sbagliato della soglia di
    arrotondamento HALF_UP richiesta dalle specifiche SdI."""
    with app.app_context():
        customer = EconomicSubject.query.filter_by(code="C0001").one()

        def _acc(code, name, typ):
            a = Account.query.filter_by(code=code).first()
            if a is None:
                a = Account(code=code, name=name, account_type=typ)
                db.session.add(a); db.session.flush()
            return a

        ricavi = _acc("400000", "Ricavi", "ricavo")
        iva = _acc("170000", "IVA a debito", "patrimoniale_passivo")
        crediti = _acc("150000", "Crediti clienti", "patrimoniale_attivo")

        # 267.50 di imponibile x 1% di IVA = 2.675 esatto -> deve arrotondare
        # a 2.68 (HALF_UP), non 2.67 (quello che dava il binario in float).
        entry = post_journal_entry(
            "DR", "14", None, "Test precisione IVA",
            [{"account_id": crediti.id, "dare": "270.18", "avere": 0},
             {"account_id": ricavi.id, "dare": 0, "avere": "267.50"},
             {"account_id": iva.id, "dare": 0, "avere": "2.68"}],
            economic_subject_id=customer.id, gross_amount="270.18",
        )
        db.session.commit()

        net, vat, vat_rate, gross = _derive_invoice_amounts(entry)
        assert isinstance(net, Decimal)
        assert isinstance(vat, Decimal)
        assert net == Decimal("267.50")
        assert vat == Decimal("2.68")
        assert gross == Decimal("270.18")
