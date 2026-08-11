"""Test di regressione — Turno 2 (AP/AR + pagamenti + riconciliazione bancaria)."""
from decimal import Decimal

from extensions import db
from models import Account, EconomicSubject, JournalEntry, InvoiceInstallment
from services.posting import post_journal_entry
from services.payments import create_installments_for_invoice, allocate_payment


def ap_form(vendor_id, expense_id, cost_center_id, number="F-1", net="1000.00", rate="22"):
    return {
        "vendor_id": str(vendor_id), "invoice_number": number, "invoice_date": "2026-08-09",
        "line_description[]": ["Test"], "line_net[]": [net], "line_vat_rate[]": [rate],
        "line_expense_account_id[]": [str(expense_id)], "line_cost_center_id[]": [str(cost_center_id)],
        "description": "Test",
    }


def test_supplier_invoice_rounds_vat_half_up_not_bankers_rounding(login, app, account, cost_center):
    """Bug: la Fattura Fornitore calcolava l'IVA con float + round() nativo
    (banker's rounding su binario) invece di Decimal + ROUND_HALF_UP, come
    richiesto dalle specifiche SdI e già fatto correttamente ovunque nel
    resto dell'app (vedi ar/routes.py _round_half_up_2). 267.50 x 1% deve
    fare 2.68 €, non 2.67 € (che è quello che dava round() nativo su float)."""
    with app.app_context():
        vendor = EconomicSubject.query.filter_by(code="F0001").one()
        expense = account("410000")
        vid, eid, cc = vendor.id, expense.id, cost_center().id

    resp = login.post("/ap/supplier_invoice", data=ap_form(vid, eid, cc, net="267.50", rate="1"))
    assert resp.status_code == 302
    with app.app_context():
        invoice = JournalEntry.query.filter_by(doc_type="KR").one()
        assert invoice.gross_amount == Decimal("270.18")  # 267.50 + 2.68


def test_supplier_payment_does_not_double_pay_after_partial_scadenzario_payment(login, app, account, cost_center):
    """Bug: supplier_payment usava sempre il lordo originale della fattura,
    ignorando un pagamento parziale già fatto via Scadenzario granulare —
    pagava di nuovo la parte già saldata invece del solo residuo."""
    with app.app_context():
        vendor = EconomicSubject.query.filter_by(code="F0001").one()
        a, b = account("410000"), account("210000")
        invoice = post_journal_entry(
            "KR", "19", None, "Fattura test doppio pagamento",
            [{"account_id": a.id, "dare": "1000.00", "avere": 0},
             {"account_id": b.id, "dare": 0, "avere": "1000.00"}],
            economic_subject_id=vendor.id, gross_amount="1000.00",
        )
        rate = create_installments_for_invoice(invoice)
        db.session.commit()
        rata_id, invoice_id, vendor_id = rate[0].id, invoice.id, vendor.id

        bank = account("180000")
        payment1 = post_journal_entry(
            "KZ", "15", None, "Acconto parziale",
            [{"account_id": b.id, "dare": "400.00", "avere": 0},
             {"account_id": bank.id, "dare": 0, "avere": "400.00"}],
            economic_subject_id=vendor.id, gross_amount="400.00",
        )
        allocate_payment(payment1, [{"installment_id": rata_id, "cash_amount": Decimal("400.00")}])
        db.session.commit()
        assert InvoiceInstallment.query.get(rata_id).residual_amount == Decimal("600.00")
        assert JournalEntry.query.get(invoice_id).is_paid is False
        payment1_id = payment1.id

    resp = login.post("/ap/supplier_payment", data={"invoice_ids[]": [str(invoice_id)]})
    assert resp.status_code == 302

    with app.app_context():
        payment2 = (JournalEntry.query.filter_by(doc_type="KZ")
                    .filter(JournalEntry.id != payment1_id).order_by(JournalEntry.id.desc()).first())
        assert payment2.gross_amount == Decimal("600.00")
        assert JournalEntry.query.get(invoice_id).is_paid is True
