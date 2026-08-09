from datetime import date
from decimal import Decimal

from extensions import db
from models import Account, Asset, EconomicSubject, InvoiceLine, JournalEntry
from services.posting import post_journal_entry, reverse_journal_entry


def ar_form(customer_id, revenue_id, number="INV-1", amount="100.00"):
    return {
        "customer_id": str(customer_id), "invoice_number": number, "invoice_date": "2026-01-10",
        "description": "Prestazione", "line_description[]": "Servizio", "line_net[]": amount,
        "line_vat_rate[]": "22", "line_natura[]": "", "line_account_id[]": str(revenue_id),
    }


def create_open(app, account, party, doc_type="KR", gross="122.00"):
    if doc_type == "KR":
        lines = [{"account_id": account("410000").id, "dare": gross, "avere": 0},
                 {"account_id": account("210000").id, "dare": 0, "avere": gross}]
    else:
        lines = [{"account_id": account("140000").id, "dare": gross, "avere": 0},
                 {"account_id": account("310000").id, "dare": 0, "avere": gross}]
    return post_journal_entry(doc_type, "19" if doc_type == "KR" else "18", None, "invoice", lines,
                              economic_subject_id=party.id, gross_amount=gross)


def test_ar_invoice_and_credit_note_have_correct_sides_and_vat(login, app, account):
    with app.app_context():
        customer = EconomicSubject.query.filter_by(code="C0001").one()
        revenue = account("310000")
        cid, rid = customer.id, revenue.id
    response = login.post("/ar/customer_invoice", data=ar_form(cid, rid), follow_redirects=False)
    assert response.status_code == 302
    with app.app_context():
        invoice = JournalEntry.query.filter_by(doc_type="DR").one()
        assert invoice.gross_amount == Decimal("122.00")
        assert invoice.total_dare == invoice.total_avere == Decimal("122.00")
        sides = {line.account.code: (line.dare, line.avere) for line in invoice.lines}
        assert sides["140000"] == (Decimal("122.00"), Decimal("0.00"))
        assert sides["310000"] == (Decimal("0.00"), Decimal("100.00"))
        assert sides["170000"] == (Decimal("0.00"), Decimal("22.00"))
        assert InvoiceLine.query.filter_by(entry_id=invoice.id).count() == 1
        invoice_id = invoice.id
    form = ar_form(cid, rid, "NC-1", "20.00"); form["linked_invoice_id"] = str(invoice_id)
    response = login.post("/ar/customer_credit_note", data=form)
    assert response.status_code == 302
    with app.app_context():
        credit = JournalEntry.query.filter_by(doc_type="DG").one()
        assert credit.linked_invoice_id == invoice_id
        assert credit.total_dare == credit.total_avere == Decimal("24.40")
        sides = {line.account.code: (line.dare, line.avere) for line in credit.lines}
        assert sides["310000"] == (Decimal("20.00"), Decimal("0.00"))
        assert sides["170000"] == (Decimal("4.40"), Decimal("0.00"))
        assert sides["140000"] == (Decimal("0.00"), Decimal("24.40"))


def test_supplier_payment_is_atomic_distinct_and_reversal_reopens(login, app, account):
    with app.app_context():
        supplier = EconomicSubject.query.filter_by(code="F0001").one()
        inv = create_open(app, account, supplier)
        iid = inv.id
    # Duplicate client fields do not duplicate the payment amount.
    response = login.post("/ap/supplier_payment", data={"invoice_ids[]": [str(iid), str(iid)]})
    assert response.status_code == 302
    with app.app_context():
        inv = db.session.get(JournalEntry, iid)
        payment = JournalEntry.query.filter_by(doc_type="KZ").one()
        assert inv.is_paid and inv.paid_by_entry_id == payment.id
        assert payment.gross_amount == Decimal("122.00")
        assert payment.total_dare == payment.total_avere == Decimal("122.00")
        reverse_journal_entry(payment.id)
        db.session.refresh(inv)
        assert not inv.is_paid and inv.paid_by_entry_id is None


def test_mixed_supplier_payment_is_rejected_without_side_effect(login, app, account):
    with app.app_context():
        s1 = EconomicSubject.query.filter_by(code="F0001").one()
        s2 = EconomicSubject.query.filter_by(code="F0002").one()
        ids = [create_open(app, account, s1, gross="10").id, create_open(app, account, s2, gross="20").id]
    response = login.post("/ap/supplier_payment", data={"invoice_ids[]": [str(x) for x in ids]}, follow_redirects=True)
    assert response.status_code == 200
    assert b"stesso fornitore" in response.data
    with app.app_context():
        assert JournalEntry.query.filter_by(doc_type="KZ").count() == 0
        assert not any(db.session.get(JournalEntry, x).is_paid for x in ids)


def test_tampered_payment_cannot_close_unrelated_document(login, app, account):
    with app.app_context():
        e = post_journal_entry("SA", "10", None, "manual", [
            {"account_id": account("180000").id, "dare": 9, "avere": 0},
            {"account_id": account("310000").id, "dare": 0, "avere": 9}])
        eid = e.id
    response = login.post("/ap/supplier_payment", data={"invoice_ids[]": str(eid)}, follow_redirects=True)
    assert b"non validi" in response.data
    with app.app_context():
        assert not db.session.get(JournalEntry, eid).is_paid
        assert JournalEntry.query.filter_by(doc_type="KZ").count() == 0


def test_asset_capitalization_and_monthly_depreciation_cap(login, app, account):
    response = login.post("/assets/asset_create", data={
        "description": "Macchinario", "category": "Impianti", "value": "120.00", "vat_rate": "22",
        "useful_life": "1", "acquisition_date": "2026-01-01",
    })
    assert response.status_code == 302
    with app.app_context():
        asset = Asset.query.one()
        assert JournalEntry.query.filter_by(doc_type="Cespiti").one().total_dare == Decimal("146.40")
    response = login.post("/assets/depreciation", data={"period": "1", "year": "2026"})
    assert response.status_code == 302
    with app.app_context():
        asset = Asset.query.one()
        assert asset.accumulated_depreciation == Decimal("10.00")
        dep = JournalEntry.query.filter_by(doc_type="AF").one()
        assert dep.total_dare == dep.total_avere == Decimal("10.00")
    # Duplicate period cannot post or increment again.
    login.post("/assets/depreciation", data={"period": "1", "year": "2026"})
    with app.app_context():
        assert JournalEntry.query.filter_by(doc_type="AF").count() == 1
        assert Asset.query.one().accumulated_depreciation == Decimal("10.00")


def test_mastrino_filtered_balance_includes_opening(login, app, account):
    with app.app_context():
        bank = account("180000"); revenue = account("310000")
        post_journal_entry("SA", "10", date(2026, 1, 1), "opening", [
            {"account_id": bank.id, "dare": 100, "avere": 0}, {"account_id": revenue.id, "dare": 0, "avere": 100}])
        post_journal_entry("SA", "10", date(2026, 2, 1), "period", [
            {"account_id": bank.id, "dare": 25, "avere": 0}, {"account_id": revenue.id, "dare": 0, "avere": 25}])
        bank_id = bank.id
    page = login.get(f"/gl/mastrino/{bank_id}?date_from=2026-02-01&date_to=2026-02-28")
    assert page.status_code == 200
    assert b"Saldo iniziale" in page.data and b"100.00" in page.data and b"125.00" in page.data


def test_customer_credit_note_is_compensated_in_collection(login, app, account):
    with app.app_context():
        customer = EconomicSubject.query.filter_by(code="C0001").one()
        invoice = create_open(app, account, customer, doc_type="DR", gross="122.00")
        credit = post_journal_entry("DG", "17", None, "credit", [
            {"account_id": account("310000").id, "dare": 20, "avere": 0},
            {"account_id": account("140000").id, "dare": 0, "avere": 20},
        ], economic_subject_id=customer.id, gross_amount="20.00")
        ids = (invoice.id, credit.id)
    response = login.post("/ar/customer_payment", data={"invoice_ids[]": [str(i) for i in ids]})
    assert response.status_code == 302
    with app.app_context():
        receipt = JournalEntry.query.filter_by(doc_type="DZ").one()
        assert receipt.total_dare == receipt.total_avere == Decimal("102.00")
        assert all(db.session.get(JournalEntry, i).is_paid for i in ids)
