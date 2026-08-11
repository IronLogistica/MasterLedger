"""Verifica che il magazzino interno (StockMovement) sostituisca correttamente
MasterLogistic-WMS: nessuna chiamata esterna, ledger sempre coerente con la
cache Material.qty_on_hand, e i tre punti di innesto (DDT, Entrata Merci,
Produzione Completata) scaricano/caricano davvero la giacenza."""
from decimal import Decimal
import pytest

from extensions import db
from models import (
    Account, EconomicSubject, Material, SalesOrder, SalesOrderLine, PurchaseOrder,
    PurchaseOrderLine, GoodsReceipt, BillOfMaterial, BOMComponent, StockMovement,
)
from services.warehouse import (
    post_stock_movement, current_stock, bom_components, explode_bom,
    fabbisogni_acquisto, WarehouseError,
)


def _acc(code, name, typ):
    a = Account.query.filter_by(code=code).first()
    if not a:
        a = Account(code=code, name=name, account_type=typ)
        db.session.add(a); db.session.flush()
    return a


def _seed_sd_mm_accounts():
    for code, name, typ in [
        ("150000", "Magazzino Materie Prime", "patrimoniale_attivo"),
        ("160000", "Magazzino Prodotti Finiti", "patrimoniale_attivo"),
        ("165000", "Ricevimenti da fatturare", "patrimoniale_passivo"),
        ("450000", "Costo del Venduto", "costo"),
    ]:
        _acc(code, name, typ)


def test_post_stock_movement_updates_ledger_and_cache(app):
    with app.app_context():
        m = Material(code="ML-TEST-1", description="Test", material_type="ROH",
                    standard_cost=Decimal("10"), qty_on_hand=0)
        db.session.add(m); db.session.commit()

        post_stock_movement(material_id=m.id, qty=Decimal("50"), movement_type="adjustment",
                            notes="carico iniziale")
        assert Material.query.get(m.id).qty_on_hand == Decimal("50")
        assert current_stock(m.id) == Decimal("50")

        post_stock_movement(material_id=m.id, qty=Decimal("-20"), movement_type="adjustment")
        assert Material.query.get(m.id).qty_on_hand == Decimal("30")
        assert current_stock(m.id) == Decimal("30")
        assert StockMovement.query.filter_by(material_id=m.id).count() == 2


def test_post_stock_movement_blocks_negative_stock(app):
    with app.app_context():
        m = Material(code="ML-TEST-2", description="Test", material_type="ROH", qty_on_hand=0)
        db.session.add(m); db.session.commit()
        with pytest.raises(WarehouseError, match="insufficiente"):
            post_stock_movement(material_id=m.id, qty=Decimal("-5"), movement_type="adjustment")
        assert Material.query.get(m.id).qty_on_hand == Decimal("0")


def test_post_stock_movement_allow_negative_bypasses_check(app):
    with app.app_context():
        m = Material(code="ML-TEST-3", description="Test", material_type="ROH", qty_on_hand=0)
        db.session.add(m); db.session.commit()
        post_stock_movement(material_id=m.id, qty=Decimal("-5"), movement_type="adjustment", allow_negative=True)
        assert Material.query.get(m.id).qty_on_hand == Decimal("-5")


def test_explode_bom_multilevel(app):
    with app.app_context():
        vite = Material(code="VITE", description="Vite", material_type="ROH", standard_cost=Decimal("0.1"))
        piastra = Material(code="PIASTRA", description="Piastra", material_type="ROH", standard_cost=Decimal("2"))
        gamba = Material(code="GAMBA", description="Gamba (semilavorato)", material_type="HALB", standard_cost=Decimal("5"))
        tavolo = Material(code="TAVOLO", description="Tavolo finito", material_type="FERT", standard_cost=Decimal("0"))
        db.session.add_all([vite, piastra, gamba, tavolo]); db.session.flush()

        # Gamba = 4 viti (BOM di 1° livello, semilavorato)
        bom_gamba = BillOfMaterial(parent_material_id=gamba.id, version="1", active=True)
        db.session.add(bom_gamba); db.session.flush()
        db.session.add(BOMComponent(bom_id=bom_gamba.id, component_material_id=vite.id, qty_per=Decimal("4")))

        # Tavolo = 1 piastra + 4 gambe (che a loro volta esplodono in viti)
        bom_tavolo = BillOfMaterial(parent_material_id=tavolo.id, version="1", active=True)
        db.session.add(bom_tavolo); db.session.flush()
        db.session.add(BOMComponent(bom_id=bom_tavolo.id, component_material_id=piastra.id, qty_per=Decimal("1")))
        db.session.add(BOMComponent(bom_id=bom_tavolo.id, component_material_id=gamba.id, qty_per=Decimal("4")))
        db.session.commit()

        fabbisogno = explode_bom(tavolo.id, Decimal("2"))  # 2 tavoli
        # 2 tavoli -> 2 piastre, 8 gambe -> 32 viti (nessuna "gamba" nel risultato: è un semilavorato con BOM propria)
        assert fabbisogno[piastra.id] == Decimal("2.000")
        assert fabbisogno[vite.id] == Decimal("32.000")
        assert gamba.id not in fabbisogno


def test_explode_bom_applies_scrap_percentage(app):
    with app.app_context():
        comp = Material(code="COMP-SCRAP", description="Componente", material_type="ROH")
        padre = Material(code="PADRE-SCRAP", description="Padre", material_type="FERT")
        db.session.add_all([comp, padre]); db.session.flush()
        bom = BillOfMaterial(parent_material_id=padre.id, version="1", active=True)
        db.session.add(bom); db.session.flush()
        db.session.add(BOMComponent(bom_id=bom.id, component_material_id=comp.id,
                                    qty_per=Decimal("10"), scrap_pct=Decimal("10")))
        db.session.commit()
        fabbisogno = explode_bom(padre.id, Decimal("1"))
        assert fabbisogno[comp.id] == Decimal("11.000")  # 10 + 10% scarto


def test_explode_bom_detects_cycle(app):
    with app.app_context():
        a = Material(code="CICLO-A", description="A", material_type="HALB")
        b = Material(code="CICLO-B", description="B", material_type="HALB")
        db.session.add_all([a, b]); db.session.flush()
        bom_a = BillOfMaterial(parent_material_id=a.id, version="1", active=True)
        bom_b = BillOfMaterial(parent_material_id=b.id, version="1", active=True)
        db.session.add_all([bom_a, bom_b]); db.session.flush()
        db.session.add(BOMComponent(bom_id=bom_a.id, component_material_id=b.id, qty_per=Decimal("1")))
        db.session.add(BOMComponent(bom_id=bom_b.id, component_material_id=a.id, qty_per=Decimal("1")))
        db.session.commit()
        with pytest.raises(WarehouseError, match="ciclica"):
            explode_bom(a.id, Decimal("1"))


def test_delivery_posts_stock_movement_and_blocks_when_insufficient(login, app):
    with app.app_context():
        _seed_sd_mm_accounts()
        customer = EconomicSubject.query.filter_by(code="C0001").one()
        mat = Material(code="SD-TEST-1", description="Prodotto", material_type="FERT",
                       standard_cost=Decimal("8"), qty_on_hand=Decimal("3"))
        db.session.add(mat); db.session.flush()
        so = SalesOrder(doc_number="SO-1", economic_subject_id=customer.id)
        db.session.add(so); db.session.flush()
        line = SalesOrderLine(order_id=so.id, material_id=mat.id, qty=Decimal("5"), price=Decimal("20"))
        db.session.add(line); db.session.commit()
        so_id, mat_id = so.id, mat.id

    # Giacenza insufficiente (3 disponibili, 5 richiesti): deve bloccare, non scaricare nulla
    resp = login.post("/sd/deliveries", data={"order_id": so_id})
    assert resp.status_code == 302
    with app.app_context():
        assert Material.query.get(mat_id).qty_on_hand == Decimal("3")
        assert StockMovement.query.filter_by(material_id=mat_id).count() == 0

    with app.app_context():
        mat = Material.query.get(mat_id)
        mat.qty_on_hand = Decimal("10")
        db.session.commit()

    resp = login.post("/sd/deliveries", data={"order_id": so_id})
    assert resp.status_code == 302
    with app.app_context():
        assert Material.query.get(mat_id).qty_on_hand == Decimal("5")  # 10 - 5 spedite
        mv = StockMovement.query.filter_by(material_id=mat_id).one()
        assert mv.qty == Decimal("-5")
        assert mv.movement_type == "delivery"


def test_goods_receipt_posts_stock_movement(login, app):
    with app.app_context():
        _seed_sd_mm_accounts()
        supplier = EconomicSubject.query.filter_by(code="F0001").one()
        mat = Material(code="MM-TEST-STOCK", description="Materia prima", material_type="ROH",
                       standard_cost=Decimal("5"), qty_on_hand=0)
        db.session.add(mat); db.session.flush()
        po = PurchaseOrder(doc_number="PO-STOCK-1", economic_subject_id=supplier.id)
        db.session.add(po); db.session.flush()
        line = PurchaseOrderLine(po_id=po.id, material_id=mat.id, qty=Decimal("20"), price=Decimal("5"))
        db.session.add(line); db.session.commit()
        po_id, line_id, mat_id = po.id, line.id, mat.id

    resp = login.post("/mm/goods-receipts", data={
        "po_id": po_id, "ddt_vendor_ref": "DDT-X", "ddt_date": "2026-01-10",
        "posting_date": "2026-01-10", f"recv_qty_{line_id}": "20",
    })
    assert resp.status_code == 302
    with app.app_context():
        assert Material.query.get(mat_id).qty_on_hand == Decimal("20")
        mv = StockMovement.query.filter_by(material_id=mat_id).one()
        assert mv.qty == Decimal("20")
        assert mv.movement_type == "goods_receipt"


def test_fabbisogni_acquisto_nets_stock_open_sales_and_open_purchases(app):
    with app.app_context():
        customer = EconomicSubject.query.filter_by(code="C0001").one()
        supplier = EconomicSubject.query.filter_by(code="F0001").one()
        mat = Material(code="FABB-TEST", description="Materiale", material_type="ROH",
                       qty_on_hand=Decimal("10"), active=True)
        db.session.add(mat); db.session.flush()

        so = SalesOrder(doc_number="SO-FABB-1", economic_subject_id=customer.id, status="aperto")
        db.session.add(so); db.session.flush()
        db.session.add(SalesOrderLine(order_id=so.id, material_id=mat.id, qty=Decimal("25"), price=Decimal("1")))

        po = PurchaseOrder(doc_number="PO-FABB-1", economic_subject_id=supplier.id)
        db.session.add(po); db.session.flush()
        db.session.add(PurchaseOrderLine(po_id=po.id, material_id=mat.id, qty=Decimal("5"), price=Decimal("1")))
        db.session.commit()

        # fabbisogno netto = 25 (impegnato) - 10 (stock) - 5 (in arrivo) = 10
        needs = {n["sku"]: n for n in fabbisogni_acquisto()}
        assert needs["FABB-TEST"]["fabbisogno_netto"] == 10.0
