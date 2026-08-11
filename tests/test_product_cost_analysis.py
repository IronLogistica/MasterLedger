from datetime import date
from decimal import Decimal

from extensions import db
from models import Material, ProductionEntry, StandardCost, ProductCostTarget
from services.product_cost import (classification, efficiency_variance,
                                   material_price_variance, material_quantity_variance,
                                   rate_variance)


def _entry(number, material, doc_date, qty, mat, labor, overhead, standard=None):
    entry = ProductionEntry(doc_number=number, material_id=material.id, doc_date=doc_date,
        qty_produced=Decimal(str(qty)), raw_material_cost=Decimal(str(mat)),
        direct_labor_cost=Decimal(str(labor)), overhead_cost=Decimal(str(overhead)),
        standard_cost_id=standard.id if standard else None)
    db.session.add(entry)
    return entry


def test_variance_formulae_use_unfavourable_positive_sign():
    assert material_quantity_variance(10, 12, 5) == Decimal("10")
    assert material_price_variance(5, 6, 12) == Decimal("12")
    assert efficiency_variance(12, 10, 8) == Decimal("16")
    assert rate_variance(10, 8, 12) == Decimal("24")
    assert classification(Decimal("0.004")) == "nulla"
    assert classification(Decimal("1")) == "sfavorevole"
    assert classification(Decimal("-1")) == "favorevole"


def test_product_cost_analysis_uses_entry_standard_and_effective_target(login, app):
    with app.app_context():
        material = Material(code="COST-1", description="Prodotto costo", material_type="FERT")
        db.session.add(material); db.session.flush()
        standard = StandardCost(material_id=material.id, year=2026, month=1,
            standard_material_cost=Decimal("10"), standard_labor_cost=Decimal("5"), standard_overhead_cost=Decimal("2"))
        target = ProductCostTarget(material_id=material.id, effective_date=date(2026, 1, 1),
            target_material_cost=Decimal("9"), target_labor_cost=Decimal("4"), target_overhead_cost=Decimal("2"))
        db.session.add_all([standard, target]); db.session.flush()
        _entry("PRD-CA-1", material, date(2026, 2, 10), 10, 120, 60, 30, standard)
        db.session.commit(); material_id = material.id

    result = login.get(f"/produzione/api/analisi-costo-prodotto?material_id={material_id}&da=2026-02-01&a=2026-02-28")
    assert result.status_code == 200
    body = result.get_json()
    assert body["ok"] is True
    assert body["quantita_prodotta"] == 10.0
    total = body["righe"][-1]
    assert total["effettivo"] == 210.0
    assert total["standard"] == 170.0
    assert total["target"] == 150.0
    assert total["varianza_standard"] == 40.0
    assert total["varianza_target"] == 60.0
    assert total["classificazione_standard"] == "sfavorevole"
    assert body["costo_unitario"] == {"effettivo": 21.0, "standard": 17.0, "target": 15.0}


def test_product_cost_analysis_reports_missing_standard_instead_of_inventing_one(login, app):
    with app.app_context():
        material = Material(code="COST-2", description="Senza standard", material_type="FERT")
        db.session.add(material); db.session.flush()
        _entry("PRD-CA-2", material, date(2026, 3, 10), 2, 20, 4, 2)
        db.session.commit(); material_id = material.id
    body = login.get(f"/produzione/api/analisi-costo-prodotto?material_id={material_id}&a=2026-03-31").get_json()
    # Nessuna registrazione ha uno standard agganciabile: lo standard resta
    # esplicitamente NON DISPONIBILE (None), mai un falso zero che genererebbe
    # una varianza "sfavorevole" fittizia confrontando il costo reale con niente.
    assert body["righe"][-1]["standard"] is None
    assert body["righe"][-1]["varianza_standard"] is None
    assert body["righe"][-1]["target"] is None
    assert any("senza costo standard" in warning for warning in body["limiti"])
    assert any("Nessun costo target" in warning for warning in body["limiti"])


def test_duplicate_target_same_material_and_date_is_rejected_with_friendly_message(login, app):
    with app.app_context():
        material = Material(code="COST-DUP", description="Duplicato", material_type="FERT")
        db.session.add(material); db.session.commit(); material_id = material.id
    data = {"material_id": material_id, "effective_date": "2026-06-01", "target_material_cost": "1",
            "target_labor_cost": "1", "target_overhead_cost": "1", "csrf_token": ""}
    r1 = login.post("/produzione/analisi-costo-prodotto", data=data, follow_redirects=True)
    r2 = login.post("/produzione/analisi-costo-prodotto", data=data, follow_redirects=True)
    assert r2.status_code == 200
    assert "Esiste già un costo target".encode() in r2.data
    # Nessun traceback/SQL grezzo esposto all'utente
    assert b"IntegrityError" not in r2.data
    assert b"UNIQUE constraint" not in r2.data
    with app.app_context():
        assert ProductCostTarget.query.filter_by(material_id=material_id).count() == 1


def test_target_cost_is_saved_as_effective_dated_version(login, app):
    with app.app_context():
        material = Material(code="COST-3", description="Target", material_type="FERT")
        db.session.add(material); db.session.commit(); material_id = material.id
    response = login.post("/produzione/analisi-costo-prodotto", data={
        "material_id": material_id, "effective_date": "2026-04-01", "target_material_cost": "1.5",
        "target_labor_cost": "2.5", "target_overhead_cost": "3", "notes": "Budget", "csrf_token": ""})
    assert response.status_code == 302
    with app.app_context():
        target = ProductCostTarget.query.one()
        assert target.effective_date == date(2026, 4, 1)
        assert target.target_total_unitario == Decimal("7.0000")


def test_product_cost_analysis_excludes_unbenchmarked_entries_from_standard_variance_on_both_sides(login, app):
    """Il bug che questo test blocca: prima, il costo reale di una registrazione
    SENZA standard restava nel totale effettivo mentre lo standard corrispondente
    restava a zero — la varianza risultava gonfiata artificialmente. Ora quella
    registrazione è esclusa dal confronto standard su ENTRAMBI i lati."""
    with app.app_context():
        material = Material(code="COST-MIX", description="Standard parziale", material_type="FERT")
        db.session.add(material); db.session.flush()
        standard = StandardCost(material_id=material.id, year=2026, month=5,
            standard_material_cost=Decimal("10"), standard_labor_cost=Decimal("0"), standard_overhead_cost=Decimal("0"))
        db.session.add(standard); db.session.flush()
        # Con standard: 10 pz, 100€ materiali reali vs 100€ standard -> varianza 0
        _entry("PRD-MIX-1", material, date(2026, 5, 5), 10, 100, 0, 0, standard)
        # SENZA standard: 5 pz, 500€ materiali reali, PRIMA che lo standard di
        # maggio diventi applicabile (_trova_standard_applicabile guarda solo
        # avanti nel tempo) -> nessuno standard agganciabile per questa riga
        _entry("PRD-MIX-2", material, date(2026, 4, 15), 5, 500, 0, 0)
        db.session.commit(); material_id = material.id

    body = login.get(f"/produzione/api/analisi-costo-prodotto?material_id={material_id}&da=2026-04-01&a=2026-05-31").get_json()
    riga_materiali = body["righe"][0]
    # Effettivo totale = 100 + 500 = 600 (include TUTTA la spesa reale)
    assert riga_materiali["effettivo"] == 600.0
    # Ma la varianza standard confronta SOLO i 100€ comparabili coi 100€ di standard -> 0, non 500 di scostamento fittizio
    assert riga_materiali["varianza_standard"] == 0.0
    assert riga_materiali["classificazione_standard"] == "nulla"
    assert any("1 registrazione" in w and "500.00" in w for w in body["limiti"])


def test_target_save_flash_uses_existing_danger_css_class_on_error(login, app):
    """Le categorie flash dell'app usano 'danger' (vedi static/css/style.css,
    .alert-danger); 'error' non ha alcuna regola CSS e renderebbe il messaggio
    senza lo stile di avviso previsto."""
    response = login.post("/produzione/analisi-costo-prodotto", data={
        "material_id": "999999", "effective_date": "2026-04-01",
        "target_material_cost": "1", "target_labor_cost": "1", "target_overhead_cost": "1",
        "csrf_token": "",
    }, follow_redirects=True)
    assert b"alert-danger" in response.data
    assert b"alert-error" not in response.data


def test_product_cost_page_is_available_from_masterledger_navigation(login):
    response = login.get("/produzione/analisi-costo-prodotto")
    assert response.status_code == 200
    assert b"Analisi costo prodotto, target e varianze" in response.data
    assert b"Nuova versione costo target" in response.data
