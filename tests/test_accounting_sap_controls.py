"""Invarianti contabili SAP-style: residui, storni e atomicità FI/COGM/DDT."""
from datetime import date
from decimal import Decimal

import pytest

from extensions import db
from models import (
    Account, BillOfMaterial, BOMComponent, Delivery, DeliveryLine,
    EconomicSubject, InvoiceInstallment, JournalEntry, Material,
    PaymentAllocation, ProductionEntry, SalesOrder, SalesOrderLine,
    StockMovement,
)
from services.payments import allocate_payment, create_installments_for_invoice
from services.posting import post_journal_entry, reverse_journal_entry
from services.reversals import reverse_delivery


def _account(code, name, account_type):
    obj = Account.query.filter_by(code=code).first()
    if obj is None:
        obj = Account(code=code, name=name, account_type=account_type)
        db.session.add(obj)
        db.session.flush()
    return obj


def _seed_production_accounts():
    for code, name, typ in (
        ("150000", "Magazzino Materie Prime", "patrimoniale_attivo"),
        ("160000", "Magazzino Prodotti Finiti", "patrimoniale_attivo"),
        ("430000", "Variazione Rimanenze PF", "ricavo"),
        ("461000", "Varianza Materiali", "costo"),
        ("462000", "Varianza Manodopera", "costo"),
        ("463000", "Varianza Overhead", "costo"),
    ):
        _account(code, name, typ)
    db.session.commit()


def test_partitario_uses_installment_residual_after_partial_payment(login, app, account):
    with app.app_context():
        supplier = EconomicSubject.query.filter_by(code="F0001").one()
        expense, payable, bank = account("410000"), account("210000"), account("180000")
        invoice = post_journal_entry(
            "KR", "19", None, "Fattura 1.000",
            [{"account_id": expense.id, "dare": "1000.00", "avere": 0},
             {"account_id": payable.id, "dare": 0, "avere": "1000.00"}],
            economic_subject_id=supplier.id, gross_amount="1000.00", commit=False,
        )
        installment = create_installments_for_invoice(invoice)[0]
        payment = post_journal_entry(
            "KZ", "15", None, "Acconto 300",
            [{"account_id": payable.id, "dare": "300.00", "avere": 0},
             {"account_id": bank.id, "dare": 0, "avere": "300.00"}],
            economic_subject_id=supplier.id, gross_amount="300.00", commit=False,
        )
        allocate_payment(payment, [{"installment_id": installment.id, "cash_amount": Decimal("300.00")}])
        db.session.commit()
        supplier_id = supplier.id

    response = login.get(f"/gl/partitario/{supplier_id}")
    assert response.status_code == 200
    assert b"700.00" in response.data
    assert b"Parziale" in response.data


def test_generic_invoice_reversal_rejects_partial_settlement(app, account):
    with app.app_context():
        supplier = EconomicSubject.query.filter_by(code="F0001").one()
        expense, payable, bank = account("410000"), account("210000"), account("180000")
        invoice = post_journal_entry(
            "KR", "19", None, "Fattura parzialmente pagata",
            [{"account_id": expense.id, "dare": "1000.00", "avere": 0},
             {"account_id": payable.id, "dare": 0, "avere": "1000.00"}],
            economic_subject_id=supplier.id, gross_amount="1000.00", commit=False,
        )
        installment = create_installments_for_invoice(invoice)[0]
        payment = post_journal_entry(
            "KZ", "15", None, "Acconto",
            [{"account_id": payable.id, "dare": "300.00", "avere": 0},
             {"account_id": bank.id, "dare": 0, "avere": "300.00"}],
            economic_subject_id=supplier.id, gross_amount="300.00", commit=False,
        )
        allocate_payment(payment, [{"installment_id": installment.id, "cash_amount": Decimal("300.00")}])
        db.session.commit()
        invoice_id = invoice.id

        with pytest.raises(ValueError, match="stornare tutti i pagamenti"):
            reverse_journal_entry(invoice_id, created_by_id=1)
        db.session.rollback()

        assert JournalEntry.query.get(invoice_id).is_reversed is False
        assert InvoiceInstallment.query.filter_by(entry_id=invoice_id).one().residual_amount == Decimal("700.00")
        assert PaymentAllocation.query.filter_by(installment_id=installment.id, reversed=False).count() == 1


def test_production_failure_rolls_back_fi_receipt_and_component_issue(login, app):
    """L'errore sullo scarico BOM avviene dopo il posting FI e il carico PF:
    il rollback deve annullare tutti e tre, senza documento orfano."""
    with app.app_context():
        _seed_production_accounts()
        parent = Material(code="FERT-ROLLBACK", description="PF", material_type="FERT", qty_on_hand=0)
        component = Material(code="ROH-EMPTY", description="MP", material_type="ROH",
                             standard_cost=Decimal("10.00"), qty_on_hand=0)
        db.session.add_all([parent, component]); db.session.flush()
        bom = BillOfMaterial(parent_material_id=parent.id, version="1", active=True)
        db.session.add(bom); db.session.flush()
        db.session.add(BOMComponent(bom_id=bom.id, component_material_id=component.id,
                                    qty_per=Decimal("2.0000"), scrap_pct=0))
        db.session.commit()
        parent_id, component_id = parent.id, component.id

    response = login.post("/produzione/completata", data={
        "material_id": parent_id,
        "qty_produced": "5",
        "raw_material_cost": "100.00",
        "direct_labor_cost": "50.00",
        "overhead_cost": "25.00",
        "bom_usata": "1",
        "posting_date": "2026-09-29",
        "period_label": "Settembre 2026",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Giacenza insufficiente" in response.data

    with app.app_context():
        assert ProductionEntry.query.filter_by(material_id=parent_id).count() == 0
        assert JournalEntry.query.filter_by(source_module="PRODUZIONE").count() == 0
        assert StockMovement.query.filter_by(source_type="production_entry").count() == 0
        assert Material.query.get(parent_id).qty_on_hand == Decimal("0.000")
        assert Material.query.get(component_id).qty_on_hand == Decimal("0.000")


def test_reverse_delivery_also_reverses_accrual_and_clears_link(app):
    with app.app_context():
        inventory = _account("160000", "Magazzino PF", "patrimoniale_attivo")
        cogs = _account("450000", "Costo del Venduto", "costo")
        accrued = _account("149000", "Fatture da emettere", "patrimoniale_attivo")
        revenue = _account("4000", "Ricavi", "ricavo")
        customer = EconomicSubject.query.filter_by(code="C0001").one()
        material = Material(code="FERT-ACCRUAL", description="PF", material_type="FERT",
                            qty_on_hand=Decimal("0"), standard_cost=Decimal("10"))
        db.session.add(material); db.session.flush()
        order = SalesOrder(doc_number="SO-ACCRUAL", economic_subject_id=customer.id)
        db.session.add(order); db.session.flush()
        order_line = SalesOrderLine(order_id=order.id, material_id=material.id,
                                    qty=Decimal("1"), qty_delivered=Decimal("1"), price=Decimal("100"))
        db.session.add(order_line); db.session.flush()
        cogs_entry = post_journal_entry(
            "SA", "10", date(2026, 9, 29), "PGI",
            [{"account_id": cogs.id, "dare": "10.00", "avere": 0},
             {"account_id": inventory.id, "dare": 0, "avere": "10.00"}],
            source_module="VENDITE", commit=False,
        )
        accrual_entry = post_journal_entry(
            "SA", "10", date(2026, 9, 29), "Fatture da emettere",
            [{"account_id": accrued.id, "dare": "100.00", "avere": 0},
             {"account_id": revenue.id, "dare": 0, "avere": "100.00"}],
            source_module="VENDITE", economic_subject_id=customer.id,
            gross_amount="100.00", commit=False,
        )
        delivery = Delivery(doc_number="DDT-ACCRUAL", order_id=order.id,
                            economic_subject_id=customer.id, cogs_entry_id=cogs_entry.id,
                            accrual_entry_id=accrual_entry.id)
        db.session.add(delivery); db.session.flush()
        db.session.add(DeliveryLine(delivery_id=delivery.id, material_id=material.id,
                                    sales_order_line_id=order_line.id, qty=Decimal("1"),
                                    price=Decimal("100"), unit_cost=Decimal("10")))
        db.session.commit()
        delivery_id, accrual_id, cogs_id = delivery.id, accrual_entry.id, cogs_entry.id

        reverse_delivery(delivery_id, "Reso totale", created_by_id=1)

        reversed_delivery = Delivery.query.get(delivery_id)
        assert reversed_delivery.is_reversed is True
        assert reversed_delivery.accrual_entry_id is None
        assert JournalEntry.query.get(accrual_id).is_reversed is True
        assert JournalEntry.query.get(cogs_id).is_reversed is True
        assert SalesOrderLine.query.get(order_line.id).qty_delivered == Decimal("0.000")
