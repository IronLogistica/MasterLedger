"""Bug reale trovato in audit (contabilità/SAP, Produzione Completata a
costo standard): con una quantità prodotta a più di 2 decimali (kg, litri —
caso legittimo in produzione), la capitalizzazione a costo standard poteva
sbilanciare la scrittura contabile di 1 centesimo e bloccare la
registrazione con "Documento non bilanciato".

Causa: standard.standard_X_cost * qty_produced (costo unitario a 4 decimali
per quantità a 4 decimali) genera importi con più di 2 decimali. La riga
"dare" usava la somma ESATTA (std_totale) mentre le tre varianze sotto
venivano arrotondate ciascuna per conto proprio — la somma di tre
arrotondamenti indipendenti può differire di 1 centesimo dall'arrotondamento
di un'unica somma (lo stesso problema, in negativo, che i moduli payroll/F24
risolvono arrotondando ogni componente PRIMA di sommare).

Verificato con una ricerca casuale di 200.000 casi (trovato un caso concreto
che sbilanciava di 1 centesimo) e la correzione verificata su 500.000 casi
casuali (0 sbilanciamenti) prima di essere applicata a
blueprints/production/routes.py::completata().
"""
from decimal import Decimal

from extensions import db
from models import Account, JournalEntry, Material, ProductionEntry, StandardCost


def _seed_produzione_accounts():
    for code, name, typ in [
        ("150000", "Magazzino Materie Prime", "patrimoniale_attivo"),
        ("160000", "Magazzino Prodotti Finiti", "patrimoniale_attivo"),
        ("430000", "Variazione Rimanenze PF", "ricavo"),
        ("461000", "Varianza Materiali", "costo"),
        ("462000", "Varianza Manodopera", "costo"),
        ("463000", "Varianza Overhead", "costo"),
    ]:
        if not Account.query.filter_by(code=code).first():
            db.session.add(Account(code=code, name=name, account_type=typ))
    db.session.commit()


def test_completata_with_standard_cost_and_fractional_quantity_balances(login, app):
    """Caso concreto trovato dalla ricerca casuale (qty a 4 decimali, kg):
    prima della correzione questa POST falliva con 'Documento non
    bilanciato' — dare 66948.87 contro avere 66948.86."""
    with app.app_context():
        _seed_produzione_accounts()
        material = Material(code="PROD-KG-1", description="Prodotto a peso", material_type="FERT")
        db.session.add(material); db.session.flush()
        standard = StandardCost(
            material_id=material.id, year=2026, month=1,
            standard_material_cost=Decimal("283.62"),
            standard_labor_cost=Decimal("398.10"),
            standard_overhead_cost=Decimal("499.57"),
        )
        db.session.add(standard); db.session.commit()
        material_id = material.id

    resp = login.post("/produzione/completata", data={
        "material_id": material_id,
        "qty_produced": "25.547",
        "raw_material_cost": "176.67",
        "direct_labor_cost": "37361.79",
        "overhead_cost": "22341.43",
        "mese_produzione": "2026-01",
        "period_label": "Gennaio 2026",
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert "non bilanciato".encode() not in resp.data
    assert "Produzione registrata".encode() in resp.data

    with app.app_context():
        pe = ProductionEntry.query.filter_by(material_id=material_id).one()
        entry = JournalEntry.query.get(pe.journal_entry_id)
        assert entry.total_dare == entry.total_avere


def test_completata_quantizes_form_money_inputs_to_cents(login, app):
    """Un importo incollato con più di 2 decimali (es. da un foglio di
    calcolo) deve essere arrotondato al centesimo subito, non lasciato a
    precisione arbitraria fino alla scrittura contabile — altrimenti anche
    il ramo SENZA costo standard può sbilanciarsi (dare = somma esatta dei
    tre importi, avere = somma di due arrotondamenti indipendenti)."""
    with app.app_context():
        _seed_produzione_accounts()
        material = Material(code="PROD-DEC3", description="Prodotto", material_type="FERT")
        db.session.add(material); db.session.commit()
        material_id = material.id

    resp = login.post("/produzione/completata", data={
        "material_id": material_id,
        "qty_produced": "10",
        "raw_material_cost": "100.005",
        "direct_labor_cost": "50.003",
        "overhead_cost": "25.002",
        "period_label": "Test",
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert "non bilanciato".encode() not in resp.data

    with app.app_context():
        pe = ProductionEntry.query.filter_by(material_id=material_id).one()
        # Ogni importo arrotondato al centesimo (ROUND_HALF_UP) subito all'ingresso.
        assert pe.raw_material_cost == Decimal("100.01")
        assert pe.direct_labor_cost == Decimal("50.00")
        assert pe.overhead_cost == Decimal("25.00")
        entry = JournalEntry.query.get(pe.journal_entry_id)
        assert entry.total_dare == entry.total_avere
