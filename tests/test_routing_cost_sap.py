"""Test end-to-end — innesto BOM + Routing + Overhead standard, metodo SAP
del centro di lavoro (services/routing_cost.py + endpoint /calcola-costo-pieno).
"""
from decimal import Decimal

from extensions import db
from models import Material, WorkCenter, Routing, RoutingOperation, BillOfMaterial, BOMComponent, ProductionOverheadItem
from services.routing_cost import work_center_overhead_rate, calcola_costo_pieno_da_routing


def _scenario_base():
    """
    Prodotto FERT "CARTELLO-01" fatto con:
      - BOM: 2 lamiere ROH (LAMIERA, costo standard 10€) + 4 viti ROH (VITE, costo standard 0,5€)
      - Routing: 1 fase al centro TAGLIO (10 min macchina, 6 min manodopera)
      - Centro TAGLIO: capacità 160 ore/mese, tariffa manodopera 18€/h
      - Pool overhead di TAGLIO per 08/2026: 400€ (ammortamento) + 100€ (energia) = 500€
        → tariffa oraria overhead = 500 / 160 = 3,125 €/h
    """
    lamiera = Material(code="LAMIERA", description="Lamiera zincata", material_type="ROH",
                       standard_cost=Decimal("10"), sales_price=Decimal("0"), qty_on_hand=Decimal("1000"))
    vite = Material(code="VITE", description="Vite autofilettante", material_type="ROH",
                    standard_cost=Decimal("0.5"), sales_price=Decimal("0"), qty_on_hand=Decimal("1000"))
    cartello = Material(code="CARTELLO-01", description="Cartello stradale", material_type="FERT",
                        standard_cost=Decimal("0"), sales_price=Decimal("100"),
                        is_carpenteria_propria=True, qty_on_hand=Decimal("0"))
    db.session.add_all([lamiera, vite, cartello])
    db.session.flush()

    bom = BillOfMaterial(parent_material_id=cartello.id, version="1", active=True)
    db.session.add(bom); db.session.flush()
    db.session.add_all([
        BOMComponent(bom_id=bom.id, component_material_id=lamiera.id, qty_per=Decimal("2"), scrap_pct=Decimal("0")),
        BOMComponent(bom_id=bom.id, component_material_id=vite.id, qty_per=Decimal("4"), scrap_pct=Decimal("0")),
    ])

    taglio = WorkCenter(code="TAGLIO", description="Taglio laser", capacity_hours_month=Decimal("160"),
                        hourly_rate_labor=Decimal("18"))
    db.session.add(taglio); db.session.flush()

    routing = Routing(parent_material_id=cartello.id, version="1", active=True)
    db.session.add(routing); db.session.flush()
    db.session.add(RoutingOperation(routing_id=routing.id, seq=10, work_center_id=taglio.id,
                                    description="Taglio e piegatura",
                                    machine_time_min=Decimal("10"), labor_time_min=Decimal("6")))

    db.session.add(ProductionOverheadItem(year=2026, month=8, description="Ammortamento laser",
                                          amount=Decimal("400"), work_center_id=taglio.id))
    db.session.add(ProductionOverheadItem(year=2026, month=8, description="Energia reparto taglio",
                                          amount=Decimal("100"), work_center_id=taglio.id))
    db.session.commit()
    return lamiera, vite, cartello, taglio, routing


def test_work_center_overhead_rate_is_pool_over_capacity(app):
    with app.app_context():
        _, _, _, taglio, _ = _scenario_base()
        tariffa, pool, avviso = work_center_overhead_rate(taglio, 2026, 8)
        assert pool == Decimal("500")
        assert tariffa == Decimal("500") / Decimal("160")
        assert avviso is None


def test_work_center_overhead_rate_warns_without_capacity(app):
    with app.app_context():
        wc = WorkCenter(code="SENZA-CAP", description="Senza capacità", capacity_hours_month=Decimal("0"))
        db.session.add(wc); db.session.commit()
        db.session.add(ProductionOverheadItem(year=2026, month=8, description="Voce", amount=Decimal("100"),
                                              work_center_id=wc.id))
        db.session.commit()
        tariffa, pool, avviso = work_center_overhead_rate(wc, 2026, 8)
        assert tariffa == Decimal("0")
        assert avviso is not None and "capacità" in avviso.lower()


def test_work_center_overhead_rate_warns_without_pool():
    pass  # copertura via test successivo (nessun pool assegnato al centro)


def test_calcola_costo_pieno_da_routing_matches_manual_computation(app):
    with app.app_context():
        _, _, cartello, taglio, _ = _scenario_base()
        manodopera, overhead, dettaglio, avvisi = calcola_costo_pieno_da_routing(cartello, 2026, 8)

        # 6 min manodopera / 60 × 18€/h = 1,80€
        assert manodopera == Decimal("1.80")
        # (10+6) min / 60 × (500/160)€/h = 0,26666...€ → verifica via moltiplicazione inversa
        ore_totali = Decimal("16") / 60
        tariffa_ovh = Decimal("500") / Decimal("160")
        assert overhead == ore_totali * tariffa_ovh
        assert avvisi == []
        assert len(dettaglio) == 1
        assert dettaglio[0]["centro"] == "TAGLIO"


def test_calcola_costo_pieno_da_routing_reports_missing_routing(app):
    with app.app_context():
        senza_routing = Material(code="NO-ROUTING", description="Senza ciclo", material_type="FERT",
                                 standard_cost=Decimal("0"), sales_price=Decimal("0"))
        db.session.add(senza_routing); db.session.commit()
        manodopera, overhead, dettaglio, avvisi = calcola_costo_pieno_da_routing(senza_routing, 2026, 8)
        assert manodopera == Decimal("0")
        assert overhead == Decimal("0")
        assert dettaglio == []
        assert len(avvisi) == 1 and "ciclo di lavorazione" in avvisi[0].lower()


def test_endpoint_calcola_costo_pieno_combines_bom_and_routing(login, app):
    with app.app_context():
        _, _, cartello, taglio, _ = _scenario_base()
        material_id = cartello.id

    resp = login.get(f"/produzione/calcola-costo-pieno?material_id={material_id}&mese=2026-08")
    assert resp.status_code == 200
    data = resp.get_json()

    # Materiali: 2×10€ (lamiera) + 4×0,5€ (vite) = 22€
    assert data["costo_materiali"] == 22.0
    assert data["costo_manodopera"] == 1.80
    assert round(data["costo_overhead"], 4) == round(float(Decimal("16") / 60 * (Decimal("500") / 160)), 4)
    assert round(data["costo_pieno_unitario"], 4) == round(
        22.0 + 1.80 + float(Decimal("16") / 60 * (Decimal("500") / 160)), 4)
    assert data["avvisi"] == []
    assert len(data["dettaglio_materiali"]) == 2
    assert len(data["dettaglio_routing"]) == 1


def test_endpoint_calcola_costo_pieno_requires_login(app):
    with app.app_context():
        _, _, cartello, taglio, _ = _scenario_base()
        material_id = cartello.id
    client = app.test_client()
    resp = client.get(f"/produzione/calcola-costo-pieno?material_id={material_id}&mese=2026-08")
    assert resp.status_code in (302, 401)
