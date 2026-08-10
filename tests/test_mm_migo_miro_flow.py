from datetime import date
from decimal import Decimal

from extensions import db
from models import (
    Account, EconomicSubject, GoodsReceipt, InvoiceVerificationLine,
    JournalEntry, Material, PurchaseOrder, PurchaseOrderLine,
)


def _prepare_mm_order():
    for code, name, account_type in [
        ("150000", "Magazzino materie prime", "patrimoniale_attivo"),
        ("165000", "Ricevimenti da fatturare", "patrimoniale_passivo"),
        ("460000", "Varianze prezzo", "costo"),
    ]:
        if not Account.query.filter_by(code=code).first():
            db.session.add(Account(code=code, name=name, account_type=account_type))
    supplier = EconomicSubject.query.filter_by(code="F0001").one()
    material = Material(code="MP-TEST", description="Materia prima test", material_type="ROH")
    po = PurchaseOrder(doc_number="4500000001", economic_subject_id=supplier.id)
    db.session.add_all([material, po])
    db.session.flush()
    line = PurchaseOrderLine(po_id=po.id, material_id=material.id,
                             qty=Decimal("10"), price=Decimal("5"))
    db.session.add(line)
    db.session.commit()
    return po.id, line.id


def _sides(entry):
    return {line.account.code: (line.dare, line.avere) for line in entry.lines}


def test_migo_then_later_miro_posts_expected_accounts_and_dates(login, app):
    with app.app_context():
        po_id, line_id = _prepare_mm_order()

    response = login.post("/mm/goods-receipts", data={
        "po_id": po_id,
        "ddt_vendor_ref": "DDT-100",
        "ddt_date": "2026-07-29",
        "posting_date": "2026-07-31",
        f"recv_qty_{line_id}": "6.000",
    })
    assert response.status_code == 302

    with app.app_context():
        gr = GoodsReceipt.query.one()
        assert gr.doc_date == date(2026, 7, 29)
        assert gr.journal_entry.posting_date == date(2026, 7, 31)
        assert _sides(gr.journal_entry) == {
            "150000": (Decimal("30.00"), Decimal("0.00")),
            "165000": (Decimal("0.00"), Decimal("30.00")),
        }
        assert PurchaseOrderLine.query.get(line_id).qty_received == Decimal("6.000")

    response = login.post("/mm/invoice-verification", data={
        "po_id": po_id,
        "invoice_ref": "FT-2026-123",
        "invoice_date": "2026-07-30",
        "posting_date": "2026-08-31",
        "vat_rate": "22",
        f"inv_qty_{line_id}": "6.000",
        f"inv_price_{line_id}": "5.0000",
    })
    assert response.status_code == 302

    with app.app_context():
        miro = JournalEntry.query.filter_by(doc_type="KR", source_module="ACQUISTI").one()
        assert miro.doc_date == date(2026, 7, 30)
        assert miro.posting_date == date(2026, 8, 31)
        assert _sides(miro) == {
            "165000": (Decimal("30.00"), Decimal("0.00")),
            "154000": (Decimal("6.60"), Decimal("0.00")),
            "210000": (Decimal("0.00"), Decimal("36.60")),
        }
        assert InvoiceVerificationLine.query.one().qty == Decimal("6.000")
        assert PurchaseOrderLine.query.get(line_id).qty_invoiced == Decimal("6.000")


def test_miro_requires_valid_dates_and_reference(login, app):
    with app.app_context():
        po_id, line_id = _prepare_mm_order()
        line = PurchaseOrderLine.query.get(line_id)
        line.qty_received = Decimal("1")
        db.session.commit()

    response = login.post("/mm/invoice-verification", data={
        "po_id": po_id,
        "invoice_ref": "",
        "invoice_date": "not-a-date",
        "posting_date": "2026-08-31",
        "vat_rate": "22",
        f"inv_qty_{line_id}": "1",
        f"inv_price_{line_id}": "5",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Riferimento fattura fornitore obbligatorio" in response.data
    with app.app_context():
        assert JournalEntry.query.filter_by(doc_type="KR", source_module="ACQUISTI").count() == 0
