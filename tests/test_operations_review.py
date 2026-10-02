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


def test_partial_receipt_keeps_residual_wip_and_final_receipt_closes_it(login, app):
    """Un versamento parziale non può chiudere l'intera commessa né mandare
    tutto il WIP a varianza: quantità, WIP e stock restano riconciliati."""
    with app.app_context():
        _seed_commessa_accounts()
        mat = Material(code="OP-PARZ", description="Prodotto parziale", material_type="FERT",
                       standard_cost=Decimal("0"), qty_on_hand=Decimal("0"))
        db.session.add(mat); db.session.flush()
        db.session.add(StandardCost(material_id=mat.id, year=2026, month=1,
            standard_material_cost=Decimal("10"), standard_labor_cost=Decimal("0"),
            standard_overhead_cost=Decimal("0")))
        db.session.commit(); mat_id = mat.id

    login.post('/produzione-operativa/commesse', data={
        'material_id': mat_id, 'qty_planned': '10', 'order_date': '2026-01-10'})
    with app.app_context():
        order = ProductionOrder.query.filter_by(material_id=mat_id).one(); order_id = order.id
    login.post(f'/produzione-operativa/commesse/{order_id}/assorbimento', data={
        'cost_type': 'MOD', 'amount': '100', 'posting_date': '2026-01-15'})
    login.post(f'/produzione-operativa/commesse/{order_id}/versamento', data={
        'qty_completed': '4', 'posting_date': '2026-01-20'})

    with app.app_context():
        from models import ProductionReceipt, StockMovement
        order = ProductionOrder.query.get(order_id)
        assert order.status == 'in_lavorazione'
        assert order.qty_completed == Decimal('4.000')
        assert order.actual_wip == Decimal('60.00')
        receipt = ProductionReceipt.query.filter_by(production_order_id=order_id).one()
        assert receipt.wip_relieved == Decimal('40.00')
        assert receipt.standard_value == Decimal('40.00')
        assert Material.query.get(mat_id).qty_on_hand == Decimal('4.000')
        assert StockMovement.query.get(receipt.stock_movement_id).posting_value == Decimal('40.00')

    login.post(f'/produzione-operativa/commesse/{order_id}/versamento', data={
        'qty_completed': '6', 'posting_date': '2026-01-25'})
    with app.app_context():
        order = ProductionOrder.query.get(order_id)
        assert order.status == 'completata'
        assert order.qty_completed == Decimal('10.000')
        assert order.actual_wip == Decimal('0.00')
        assert Material.query.get(mat_id).qty_on_hand == Decimal('10.000')


def test_issue_is_atomic_with_stock_and_fi(login, app):
    """Se la giacenza non basta, il fallimento stock annulla anche la scrittura FI."""
    with app.app_context():
        _seed_commessa_accounts()
        fert = Material(code='OP-ATOMIC', description='PF', material_type='FERT', qty_on_hand=0)
        comp = Material(code='COMP-ATOMIC', description='MP', material_type='ROH',
                        qty_on_hand=0, standard_cost=Decimal('5'))
        db.session.add_all([fert, comp]); db.session.commit(); fert_id, comp_id = fert.id, comp.id
    login.post('/produzione-operativa/commesse', data={'material_id': fert_id, 'qty_planned': '1'})
    with app.app_context():
        order = ProductionOrder.query.filter_by(material_id=fert_id).one()
        order_id, order_number = order.id, order.order_number
    login.post(f'/produzione-operativa/commesse/{order_id}/prelievo', data={
        'material_id': comp_id, 'qty': '1', 'unit_cost': '5', 'posting_date': '2026-01-10'})
    with app.app_context():
        from models import JournalEntry, ProductionMaterialIssue, StockMovement
        assert ProductionMaterialIssue.query.filter_by(production_order_id=order_id).count() == 0
        assert StockMovement.query.filter_by(source_type='production_order', source_id=order_id).count() == 0
        assert JournalEntry.query.filter_by(source_module='PRODUZIONE', reference=order_number).count() == 0


def test_issue_uses_three_decimals_and_negative_stock_value(login, app):
    with app.app_context():
        _seed_commessa_accounts()
        fert = Material(code='OP-SIGN', description='PF', material_type='FERT', qty_on_hand=0)
        comp = Material(code='COMP-SIGN', description='MP', material_type='ROH',
                        qty_on_hand=Decimal('10'), standard_cost=Decimal('0.335'))
        db.session.add_all([fert, comp]); db.session.flush()
        # La cache iniziale deve avere il corrispondente ledger di apertura.
        from models import StockMovement
        db.session.add(StockMovement(material_id=comp.id, qty=Decimal('10'), unit_cost=Decimal('0.335'),
                                     movement_type='adjustment'))
        db.session.commit(); fert_id, comp_id = fert.id, comp.id
    login.post('/produzione-operativa/commesse', data={'material_id': fert_id, 'qty_planned': '1'})
    with app.app_context():
        order_id = ProductionOrder.query.filter_by(material_id=fert_id).one().id
    login.post(f'/produzione-operativa/commesse/{order_id}/prelievo', data={
        'material_id': comp_id, 'qty': '3', 'unit_cost': '0.335', 'posting_date': '2026-01-10'})
    with app.app_context():
        from models import ProductionMaterialIssue, StockMovement
        issue = ProductionMaterialIssue.query.filter_by(production_order_id=order_id).one()
        movement = StockMovement.query.get(issue.stock_movement_id)
        assert issue.total_cost == Decimal('1.01')
        assert movement.posting_value == Decimal('-1.01')
        assert movement.total_value == Decimal('-1.01')
        assert ProductionOrder.query.get(order_id).actual_wip == Decimal('1.01')

    # Precisione oltre i tre decimali: nessuna registrazione aggiuntiva.
    login.post(f'/produzione-operativa/commesse/{order_id}/prelievo', data={
        'material_id': comp_id, 'qty': '1.2345', 'unit_cost': '1', 'posting_date': '2026-01-10'})
    with app.app_context():
        from models import ProductionMaterialIssue
        assert ProductionMaterialIssue.query.filter_by(production_order_id=order_id).count() == 1


def test_legacy_completed_order_does_not_reopen_wip(app):
    with app.app_context():
        _seed_commessa_accounts()
        fert = Material(code='OP-LEGACY', description='PF storico', material_type='FERT', qty_on_hand=0)
        comp = Material(code='COMP-LEGACY', description='MP', material_type='ROH', qty_on_hand=0)
        db.session.add_all([fert, comp]); db.session.flush()
        order = ProductionOrder(order_number='OP-LEGACY-1', material_id=fert.id,
                                qty_planned=Decimal('10'), status='completata')
        from models import ProductionMaterialIssue
        order.issues.append(ProductionMaterialIssue(material_id=comp.id,
                                                    qty=Decimal('10'), unit_cost=Decimal('2'),
                                                    journal_entry_id=999))
        assert order.qty_completed == Decimal('10.000')
        assert order.actual_wip == Decimal('0.00')
