from datetime import date
from decimal import Decimal

from extensions import db
from models import (
    EconomicSubject, GoodsReceipt, GoodsReceiptLine, InvoiceVerificationLine,
    JournalEntry, Material, PurchaseOrder, PurchaseOrderLine,
)
from services.mm_invoice_quantities import actual_invoiced_qty


def _mm_case(qty_invoiced="10"):
    supplier = EconomicSubject.query.filter_by(code="F0001").one()
    material = Material(code="MM-TEST", description="Materiale test", material_type="ROH")
    po = PurchaseOrder(doc_number="3300000099", doc_date=date(2026, 8, 10),
                       economic_subject_id=supplier.id)
    db.session.add_all([material, po])
    db.session.flush()
    po_line = PurchaseOrderLine(po_id=po.id, material_id=material.id, qty=Decimal("10"),
                                price=Decimal("5"), qty_received=Decimal("10"),
                                qty_invoiced=Decimal(qty_invoiced))
    db.session.add(po_line)
    db.session.flush()
    receipt = GoodsReceipt(doc_number="5000000099", doc_date=date(2026, 8, 10), po_id=po.id)
    db.session.add(receipt)
    db.session.flush()
    db.session.add(GoodsReceiptLine(receipt_id=receipt.id, po_line_id=po_line.id,
                                    qty=Decimal("10")))
    db.session.commit()
    return receipt.id, po_line.id, po.id


def _kr(po_number, *, reversed=False):
    entry = JournalEntry(
        doc_number="1900000099", doc_type="KR", doc_date=date(2026, 8, 10),
        source_module="ACQUISTI", description=f"Verifica fattura TEST su OA {po_number} (three-way match OK)",
        is_reversed=reversed,
    )
    db.session.add(entry)
    db.session.flush()
    return entry


def test_stale_qty_without_real_invoice_does_not_block_goods_receipt_delete(login, app):
    """Riproduzione esatta: cache=10, ma nessuna fattura reale dietro."""
    with app.app_context():
        receipt_id, po_line_id, _ = _mm_case(qty_invoiced="10")

    response = login.post(f"/mm/goods-receipts/{receipt_id}/elimina", follow_redirects=False)
    assert response.status_code == 302

    with app.app_context():
        assert db.session.get(GoodsReceipt, receipt_id) is None
        line = db.session.get(PurchaseOrderLine, po_line_id)
        assert line.qty_received == Decimal("0.000")
        assert line.qty_invoiced == Decimal("0.000")


def test_tracked_active_invoice_still_blocks_delete(login, app):
    with app.app_context():
        receipt_id, po_line_id, po_id = _mm_case(qty_invoiced="0")
        po = db.session.get(PurchaseOrder, po_id)
        entry = _kr(po.doc_number)
        db.session.add(InvoiceVerificationLine(entry_id=entry.id, po_line_id=po_line_id,
                                               qty=Decimal("10")))
        db.session.commit()
        assert actual_invoiced_qty(po_line_id) == Decimal("10")

    response = login.post(f"/mm/goods-receipts/{receipt_id}/elimina", follow_redirects=False)
    assert response.status_code == 302

    with app.app_context():
        assert db.session.get(GoodsReceipt, receipt_id) is not None
        assert db.session.get(PurchaseOrderLine, po_line_id).qty_received == Decimal("10.000")


def test_untracked_active_legacy_invoice_blocks_conservatively(login, app):
    """Una fattura reale senza dettaglio non viene scambiata per quantità zero."""
    with app.app_context():
        receipt_id, po_line_id, po_id = _mm_case(qty_invoiced="10")
        po = db.session.get(PurchaseOrder, po_id)
        _kr(po.doc_number)
        db.session.commit()

    response = login.post(f"/mm/goods-receipts/{receipt_id}/elimina", follow_redirects=False)
    assert response.status_code == 302

    with app.app_context():
        assert db.session.get(GoodsReceipt, receipt_id) is not None
        assert db.session.get(PurchaseOrderLine, po_line_id).qty_received == Decimal("10.000")


def test_reversed_tracked_invoice_is_not_counted(app):
    with app.app_context():
        _, po_line_id, po_id = _mm_case(qty_invoiced="10")
        po = db.session.get(PurchaseOrder, po_id)
        entry = _kr(po.doc_number, reversed=True)
        db.session.add(InvoiceVerificationLine(entry_id=entry.id, po_line_id=po_line_id,
                                               qty=Decimal("10")))
        db.session.commit()
        assert actual_invoiced_qty(po_line_id) == Decimal("0")
