"""Test di regressione — Turno 4, parte produzione."""
from datetime import date
from decimal import Decimal

from extensions import db
from models import Material, ProductionOverheadItem, EconomicSubject, SalesOrder, SalesOrderLine, Account
from blueprints.production.routes import _calcola_overhead_da_fatturato


def _material_carpenteria(code, sales_price="100"):
    m = Material(code=code, description="Prodotto carpenteria", material_type="FERT",
                standard_cost=Decimal("0"), sales_price=Decimal(sales_price),
                is_carpenteria_propria=True, qty_on_hand=Decimal("1000"))
    db.session.add(m); db.session.flush()
    return m


def _consegna(material, qty, price, giorno):
    for code, name, typ in [
        ("160000", "Magazzino Prodotti Finiti", "patrimoniale_attivo"),
        ("450000", "Costo del Venduto", "costo"),
    ]:
        if not Account.query.filter_by(code=code).first():
            db.session.add(Account(code=code, name=name, account_type=typ))
    customer = EconomicSubject.query.filter_by(code="C0001").one()
    so = SalesOrder(doc_number=f"SO-OH-{material.code}", economic_subject_id=customer.id, doc_date=giorno)
    db.session.add(so); db.session.flush()
    db.session.add(SalesOrderLine(order_id=so.id, material_id=material.id, qty=qty, price=price))
    db.session.commit()
    from models import Delivery, DeliveryLine
    d = Delivery(doc_number=f"DL-OH-{material.code}", order_id=so.id,
                economic_subject_id=customer.id, doc_date=giorno)
    db.session.add(d); db.session.flush()
    db.session.add(DeliveryLine(delivery_id=d.id, material_id=material.id, qty=qty, price=price,
                                unit_cost=Decimal("0")))
    db.session.commit()


def test_overhead_mix_renormalizes_when_cost_primo_basis_is_zero(app):
    """Bug: con un mix fatturato/costo-primo (es. 50/50) e nessun costo primo
    calcolabile per il mese (nessuna produzione, standard_cost=0), metà del
    pool restava silenziosamente non assegnata a nessun prodotto — la somma
    delle quote non arrivava mai al 100% del pool."""
    with app.app_context():
        mat = _material_carpenteria("OH-TEST-1")
        db.session.add(ProductionOverheadItem(year=2026, month=6, description="Test pool", amount=Decimal("1000")))
        db.session.commit()
        _consegna(mat, Decimal("10"), Decimal("100"), date(2026, 6, 15))

        quota, dettaglio, pool_totale, avviso = _calcola_overhead_da_fatturato(
            mat, date(2026, 6, 1), peso_fatturato_pct=50
        )
        assert pool_totale == Decimal("1000")
        assert quota == Decimal("1000")
        assert avviso is not None
        assert "rinormalizzat" in avviso.lower()


def test_overhead_mix_no_renormalization_when_both_bases_present(app):
    """Controprova: se entrambe le basi hanno dati, nessuna rinormalizzazione
    e nessun avviso — comportamento invariato."""
    with app.app_context():
        mat = _material_carpenteria("OH-TEST-2")
        db.session.add(ProductionOverheadItem(year=2026, month=7, description="Test pool", amount=Decimal("500")))
        db.session.commit()
        mat.standard_cost = Decimal("10")
        db.session.commit()
        _consegna(mat, Decimal("5"), Decimal("50"), date(2026, 7, 10))

        quota, dettaglio, pool_totale, avviso = _calcola_overhead_da_fatturato(
            mat, date(2026, 7, 1), peso_fatturato_pct=50
        )
        assert avviso is None
        assert quota == Decimal("500")
