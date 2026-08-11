"""Test di regressione per i bug trovati nella rilettura riga-per-riga del
motore contabile core (services/posting.py, services/reversals.py,
services/payments.py, blueprints/gl/routes.py)."""
from decimal import Decimal
import pytest

from extensions import db
from models import (Account, CostCenter, EconomicSubject, Material, SalesOrder,
                     SalesOrderLine, Delivery, DeliveryLine, JournalEntry, InvoiceInstallment)
from services.posting import post_journal_entry
from services.payments import create_installments_for_invoice, allocate_payment
from services.reversals import reverse_delivery
from services.warehouse import post_stock_movement


def _seed_sd_accounts():
    for code, name, typ in [
        ("150000", "Magazzino Materie Prime", "patrimoniale_attivo"),
        ("160000", "Magazzino Prodotti Finiti", "patrimoniale_attivo"),
        ("450000", "Costo del Venduto", "costo"),
    ]:
        if not Account.query.filter_by(code=code).first():
            db.session.add(Account(code=code, name=name, account_type=typ))
    db.session.flush()


def test_invoice_with_multiple_installments_is_not_marked_paid_until_all_are_settled(app, account):
    """Bug: allocate_payment segnava l'intera fattura pagata (is_paid=True)
    appena UNA rata su più si azzerava, invece di aspettare che TUTTE le
    rate fossero saldate."""
    with app.app_context():
        supplier = EconomicSubject.query.filter_by(code="F0001").one()
        a, b = account("410000"), account("210000")
        invoice = post_journal_entry(
            "KR", "19", None, "Fattura a due rate",
            [{"account_id": a.id, "dare": "1000.00", "avere": 0},
             {"account_id": b.id, "dare": 0, "avere": "1000.00"}],
            economic_subject_id=supplier.id, gross_amount="1000.00",
        )
        rate = create_installments_for_invoice(invoice, schedule=[(15, Decimal("400.00")), (45, Decimal("600.00"))])
        db.session.commit()
        assert len(rate) == 2
        rata1_id, rata2_id, invoice_id = rate[0].id, rate[1].id, invoice.id

        bank = account("180000")
        payment1 = post_journal_entry(
            "KZ", "15", None, "Pagamento rata 1",
            [{"account_id": b.id, "dare": "400.00", "avere": 0},
             {"account_id": bank.id, "dare": 0, "avere": "400.00"}],
            economic_subject_id=supplier.id, gross_amount="400.00",
        )
        allocate_payment(payment1, [{"installment_id": rata1_id, "cash_amount": Decimal("400.00")}])
        db.session.commit()

        # La rata 1 è saldata, ma la rata 2 (600€) resta aperta: la fattura
        # NON deve risultare pagata.
        inv = JournalEntry.query.get(invoice_id)
        assert inv.is_paid is False
        assert InvoiceInstallment.query.get(rata1_id).residual_amount == Decimal("0.00")
        assert InvoiceInstallment.query.get(rata2_id).residual_amount == Decimal("600.00")

        payment2 = post_journal_entry(
            "KZ", "15", None, "Pagamento rata 2",
            [{"account_id": b.id, "dare": "600.00", "avere": 0},
             {"account_id": bank.id, "dare": 0, "avere": "600.00"}],
            economic_subject_id=supplier.id, gross_amount="600.00",
        )
        allocate_payment(payment2, [{"installment_id": rata2_id, "cash_amount": Decimal("600.00")}])
        db.session.commit()

        # Ora ENTRAMBE le rate sono saldate: la fattura risulta pagata, e
        # dal secondo pagamento (quello che ha chiuso l'ultima rata aperta).
        inv = JournalEntry.query.get(invoice_id)
        assert inv.is_paid is True
        assert inv.paid_by_entry_id == payment2.id


def test_reverse_delivery_restores_correct_line_when_order_has_duplicate_material(login, app):
    """Bug: reverse_delivery cercava la SalesOrderLine da ripristinare per
    material_id — ambiguo se l'ordine ha lo stesso articolo su due righe
    (es. stesso SKU a prezzi diversi). Ora usa il riferimento diretto
    salvato su DeliveryLine.sales_order_line_id."""
    with app.app_context():
        _seed_sd_accounts()
        customer = EconomicSubject.query.filter_by(code="C0001").one()
        mat = Material(code="SD-DUP-1", description="Prodotto", material_type="FERT",
                       standard_cost=Decimal("5"), qty_on_hand=Decimal("100"))
        db.session.add(mat); db.session.flush()

        so = SalesOrder(doc_number="SO-DUP-1", economic_subject_id=customer.id)
        db.session.add(so); db.session.flush()
        # Stesso articolo su DUE righe a prezzi diversi (nessun vincolo lo impedisce)
        line_a = SalesOrderLine(order_id=so.id, material_id=mat.id, qty=Decimal("10"), price=Decimal("20"))
        line_b = SalesOrderLine(order_id=so.id, material_id=mat.id, qty=Decimal("5"), price=Decimal("30"))
        db.session.add_all([line_a, line_b]); db.session.commit()
        so_id, mat_id, line_a_id, line_b_id = so.id, mat.id, line_a.id, line_b.id

    # Consegna riga B (5 pezzi) via API reale, cosi' DeliveryLine.sales_order_line_id
    # viene popolato dal codice reale, non impostato a mano nel test.
    resp = login.post("/sd/deliveries", data={"order_id": so_id})
    assert resp.status_code == 302

    with app.app_context():
        so_line_a = SalesOrderLine.query.get(line_a_id)
        so_line_b = SalesOrderLine.query.get(line_b_id)
        # Entrambe le righe vengono spedite nella stessa chiamata (nessun residuo parziale)
        assert so_line_a.qty_delivered == Decimal("10.000")
        assert so_line_b.qty_delivered == Decimal("5.000")
        dl_b = DeliveryLine.query.filter_by(sales_order_line_id=line_b_id).one()
        assert dl_b.qty == Decimal("5.000")
        delivery_id = dl_b.delivery_id

        reverse_delivery(delivery_id, reason="Test storno", created_by_id=None)
        db.session.commit()

        so_line_a = SalesOrderLine.query.get(line_a_id)
        so_line_b = SalesOrderLine.query.get(line_b_id)
        # Lo storno deve azzerare ESATTAMENTE le righe consegnate, non una a
        # caso: prima del fix, .first() poteva decrementare la riga sbagliata.
        assert so_line_a.qty_delivered == Decimal("0.000")
        assert so_line_b.qty_delivered == Decimal("0.000")
