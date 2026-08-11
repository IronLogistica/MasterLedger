"""Test di regressione — Turno 6 (moduli minori: operations/commesse)."""
from decimal import Decimal

from extensions import db
from models import Account, Material, ProductionOrder, StandardCost


def _seed_commessa_accounts():
    for code, name, typ in [
        ("157000", "WIP", "patrimoniale_attivo"),
        ("160000", "Magazzino Prodotti Finiti", "patrimoniale_attivo"),
        ("472000", "MOD assorbita", "patrimoniale_passivo"),
        ("473000", "Overhead assorbito", "patrimoniale_passivo"),
        ("464000", "Varianza produzione", "costo"),
        ("150000", "Materie prime", "patrimoniale_attivo"),
    ]:
        if not Account.query.filter_by(code=code).first():
            db.session.add(Account(code=code, name=name, account_type=typ))
    db.session.commit()


def test_double_receipt_on_same_order_is_blocked(login, app):
    """Bug: nessuna delle tre route (prelievo/assorbimento/versamento) blocca
    le operazioni su una commessa già completata. Chiamare due volte il
    versamento chiuderebbe il WIP due volte (già a zero), duplicando PF e
    varianza nella scrittura contabile."""
    with app.app_context():
        _seed_commessa_accounts()
        mat = Material(code="OP-DOPPIO", description="Prodotto", material_type="FERT",
                       standard_cost=Decimal("0"), qty_on_hand=Decimal("0"))
        db.session.add(mat); db.session.flush()
        db.session.add(StandardCost(material_id=mat.id, year=2026, month=1,
            standard_material_cost=Decimal("10"), standard_labor_cost=Decimal("0"),
            standard_overhead_cost=Decimal("0")))
        db.session.commit()
        mat_id = mat.id

    resp = login.post("/produzione-operativa/commesse", data={
        "material_id": mat_id, "qty_planned": "10", "order_date": "2026-01-10",
    })
    assert resp.status_code == 302
    with app.app_context():
        o = ProductionOrder.query.filter_by(material_id=mat_id).one()
        order_id = o.id
        order_number = o.order_number

    login.post(f"/produzione-operativa/commesse/{order_id}/assorbimento",
               data={"cost_type": "MOD", "amount": "100"})

    r1 = login.post(f"/produzione-operativa/commesse/{order_id}/versamento", data={"qty_completed": "10"})
    assert r1.status_code == 302
    with app.app_context():
        assert ProductionOrder.query.get(order_id).status == "completata"

    r2 = login.post(f"/produzione-operativa/commesse/{order_id}/versamento", data={"qty_completed": "10"})
    assert r2.status_code == 302
    with app.app_context():
        from models import JournalEntry
        chiusure = JournalEntry.query.filter_by(source_module="PRODUZIONE", reference=order_number,
                                                 doc_type="SA").all()
        # Solo 1 assorbimento + 1 versamento = 2 scritture, non 3
        assert len(chiusure) == 2


def test_issue_and_absorb_blocked_after_completion(login, app):
    """Stesso bug per prelievo e assorbimento dopo il versamento."""
    with app.app_context():
        _seed_commessa_accounts()
        mat = Material(code="OP-DOPPIO2", description="Prodotto", material_type="FERT",
                       standard_cost=Decimal("0"), qty_on_hand=Decimal("0"))
        db.session.add(mat); db.session.flush()
        db.session.add(StandardCost(material_id=mat.id, year=2026, month=1,
            standard_material_cost=Decimal("5"), standard_labor_cost=Decimal("0"),
            standard_overhead_cost=Decimal("0")))
        mat2 = Material(code="COMP-DOPPIO2", description="Componente", material_type="ROH")
        db.session.add(mat2)
        db.session.commit()
        mat_id, mat2_id = mat.id, mat2.id

    login.post("/produzione-operativa/commesse", data={"material_id": mat_id, "qty_planned": "5",
                                              "order_date": "2026-01-10"})
    with app.app_context():
        o = ProductionOrder.query.filter_by(material_id=mat_id).one()
        order_id = o.id
    login.post(f"/produzione-operativa/commesse/{order_id}/assorbimento", data={"cost_type": "MOD", "amount": "25"})
    login.post(f"/produzione-operativa/commesse/{order_id}/versamento", data={"qty_completed": "5"})

    r_issue = login.post(f"/produzione-operativa/commesse/{order_id}/prelievo",
                         data={"material_id": mat2_id, "qty": "1", "unit_cost": "1"})
    assert r_issue.status_code == 302
    r_absorb = login.post(f"/produzione-operativa/commesse/{order_id}/assorbimento",
                          data={"cost_type": "OVERHEAD", "amount": "10"})
    assert r_absorb.status_code == 302

    with app.app_context():
        from models import ProductionMaterialIssue, ProductionCostAbsorption
        assert ProductionMaterialIssue.query.filter_by(production_order_id=order_id).count() == 0
        # Solo l'assorbimento MOD originale, non quello OVERHEAD tentato dopo la chiusura
        assert ProductionCostAbsorption.query.filter_by(production_order_id=order_id).count() == 1
