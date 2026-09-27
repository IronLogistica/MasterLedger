"""Bug reale trovato in audit (gestione incassi/pagamenti): quando una
fattura riceve PRIMA un acconto parziale tracciato dallo Scadenzario
granulare (/gl/scadenzario/paga, services.payments.allocate_payment) e POI
viene chiusa a saldo dalla vista "compensazione semplificata" (Incasso
Cliente / Pagamento Fornitore), quella seconda scrittura azzerava
direttamente residual_amount SENZA registrare alcuna PaymentAllocation.

services.posting.reverse_journal_entry decide come ripristinare lo
Scadenzario guardando se il pagamento stornato ha PaymentAllocation
tracciate: se non ne trova (come nel caso della "compensazione"), fa un
reset PIENO di ogni rata al suo importo originario — cancellando anche
l'acconto granulare precedente, MAI stornato, il cui denaro resta
comunque, correttamente, sul conto banca della sua scrittura. Risultato:
dopo lo storno del solo pagamento a saldo, il debito/credito risultava per
l'INTERO importo originale invece che per la sola quota effettivamente
ripristinata da quello storno — un doppio incasso/pagamento in agguato.

Fix: anche la "compensazione semplificata" ora registra una
PaymentAllocation per la quota realmente chiusa da quel movimento, così lo
storno usa lo stesso percorso granulare di reverse_payment_allocations e
ripristina solo quella quota, mai gli acconti precedenti e indipendenti.
"""
from decimal import Decimal

from extensions import db
from models import EconomicSubject, JournalEntry, InvoiceInstallment, PaymentAllocation
from services.posting import reverse_journal_entry


def ap_form(vendor_id, expense_id, cost_center_id, number="F-1", net="1000.00"):
    return {
        "vendor_id": str(vendor_id), "invoice_number": number, "invoice_date": "2026-08-09",
        "line_description[]": ["Test"], "line_net[]": [net], "line_vat_rate[]": ["22"],
        "line_expense_account_id[]": [str(expense_id)], "line_cost_center_id[]": [str(cost_center_id)],
        "description": "Test",
    }


def test_supplier_payment_saldo_reversal_does_not_erase_earlier_granular_acconto(login, app, account, cost_center):
    with app.app_context():
        vendor = EconomicSubject.query.filter_by(code="F0001").one()
        expense = account("410000")
        vid, eid, cc = vendor.id, expense.id, cost_center().id

    login.post("/ap/supplier_invoice", data=ap_form(vid, eid, cc, number="F-MIX-AP", net="1000.00"))
    with app.app_context():
        invoice = JournalEntry.query.filter_by(reference="F-MIX-AP").one()
        inst = InvoiceInstallment.query.filter_by(entry_id=invoice.id).one()
        inst_id, invoice_id = inst.id, invoice.id

    # Acconto parziale granulare (tracciato)
    login.post("/gl/scadenzario/paga", data={
        "installment_id[]": [str(inst_id)], f"cash_{inst_id}": "400.00", f"abbuono_{inst_id}": "0",
    })
    with app.app_context():
        assert InvoiceInstallment.query.get(inst_id).residual_amount == Decimal("820.00")  # 1220 - 400

    # Saldo pieno tramite "Pagamento Fornitore" (compensazione semplificata)
    login.post("/ap/supplier_payment", data={"invoice_ids[]": [str(invoice_id)]}, follow_redirects=True)
    with app.app_context():
        invoice = JournalEntry.query.get(invoice_id)
        assert invoice.is_paid is True
        saldo_entry_id = invoice.paid_by_entry_id
        assert InvoiceInstallment.query.get(inst_id).residual_amount == Decimal("0.00")
        # La compensazione deve aver lasciato una traccia granulare, come
        # qualunque altro pagamento — non solo un azzeramento cieco.
        assert PaymentAllocation.query.filter_by(
            payment_entry_id=saldo_entry_id, installment_id=inst_id, reversed=False
        ).one().cash_amount == Decimal("820.00")

    # Storno del SOLO pagamento a saldo: l'acconto granulare da 400 (mai
    # stornato) deve restare fermo, deve tornare indietro solo il saldo.
    with app.app_context():
        reverse_journal_entry(saldo_entry_id, created_by_id=1)
        assert InvoiceInstallment.query.get(inst_id).residual_amount == Decimal("820.00")
        assert JournalEntry.query.get(invoice_id).is_paid is False


def test_customer_payment_saldo_reversal_does_not_erase_earlier_granular_acconto(login, app, account, cost_center):
    """Stesso bug, lato Incasso Cliente (ar/routes.py::customer_payment)."""
    from services.posting import post_journal_entry
    from services.payments import create_installments_for_invoice

    with app.app_context():
        customer = EconomicSubject.query.filter_by(code="C0001").one()
        revenue = account("310000")
        crediti = account("140000")
        invoice = post_journal_entry(
            "DR", "12", None, "Fattura test", [
                {"account_id": crediti.id, "dare": "1220.00", "avere": 0},
                {"account_id": revenue.id, "dare": 0, "avere": "1220.00"},
            ], economic_subject_id=customer.id, gross_amount="1220.00", commit=False,
        )
        create_installments_for_invoice(invoice)
        db.session.commit()
        inst = InvoiceInstallment.query.filter_by(entry_id=invoice.id).one()
        inst_id, invoice_id = inst.id, invoice.id

    login.post("/gl/scadenzario/paga", data={
        "installment_id[]": [str(inst_id)], f"cash_{inst_id}": "400.00", f"abbuono_{inst_id}": "0",
    })
    with app.app_context():
        assert InvoiceInstallment.query.get(inst_id).residual_amount == Decimal("820.00")

    login.post("/ar/customer_payment", data={"invoice_ids[]": [str(invoice_id)]}, follow_redirects=True)
    with app.app_context():
        invoice = JournalEntry.query.get(invoice_id)
        assert invoice.is_paid is True
        saldo_entry_id = invoice.paid_by_entry_id
        assert InvoiceInstallment.query.get(inst_id).residual_amount == Decimal("0.00")
        assert PaymentAllocation.query.filter_by(
            payment_entry_id=saldo_entry_id, installment_id=inst_id, reversed=False
        ).one().cash_amount == Decimal("820.00")

    with app.app_context():
        reverse_journal_entry(saldo_entry_id, created_by_id=1)
        assert InvoiceInstallment.query.get(inst_id).residual_amount == Decimal("820.00")
        assert JournalEntry.query.get(invoice_id).is_paid is False
