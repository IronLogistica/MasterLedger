"""
services/warehouse.py — Magazzino interno a MasterLedger.

Sostituisce services/logistic_client.py (integrazione in sola lettura verso
MasterLogistic-WMS): qui non serve nessun parser di PDF né una chiamata HTTP
a un sistema esterno, perché gli ordini cliente e fornitore vivono già come
righe vere in QUESTO database — SalesOrderLine.qty_delivered,
PurchaseOrderLine.qty_received — non come documenti da rileggere ogni volta.

Fonte di verità della giacenza: la somma dei movimenti in StockMovement.
Material.qty_on_hand resta come CACHE aggiornata ad ogni movimento per le
query veloci (liste, badge), ma va sempre considerata un riflesso del
ledger, mai l'inverso — se mai divergessero, ricalcola da StockMovement.
"""
from decimal import Decimal, InvalidOperation

from extensions import db
from models import Material, StockMovement, BillOfMaterial, BOMComponent


class WarehouseError(ValueError):
    """Errori di dominio del magazzino interno (sostituisce LogisticError)."""


def current_stock(material_id):
    """Giacenza vera, ricalcolata dal ledger (usata per riconciliare/verificare la cache)."""
    total = db.session.query(db.func.coalesce(db.func.sum(StockMovement.qty), 0)) \
        .filter(StockMovement.material_id == material_id).scalar()
    return Decimal(str(total))


def post_stock_movement(material_id, qty, movement_type, source_type=None, source_id=None,
                        warehouse_area_id=None, unit_cost=None, doc_date=None, notes=None,
                        created_by_id=None, allow_negative=False, commit=False):
    """
    Registra UN movimento di magazzino e aggiorna la cache Material.qty_on_hand
    nello stesso momento — mai l'uno senza l'altro, per evitare che ledger e
    cache divergano. qty positiva = carico, negativa = scarico.

    Solleva WarehouseError se lo scarico porterebbe la giacenza sotto zero
    (a meno di allow_negative=True, riservato a rettifiche esplicite).
    """
    material = Material.query.get(material_id)
    if material is None:
        raise WarehouseError(f"Articolo id={material_id} non trovato.")
    try:
        qty = Decimal(str(qty))
    except (InvalidOperation, TypeError):
        raise WarehouseError("Quantità movimento non valida.")
    if qty == 0:
        raise WarehouseError("Un movimento di magazzino con quantità zero non ha senso.")

    nuovo_saldo = Decimal(str(material.qty_on_hand or 0)) + qty
    if nuovo_saldo < 0 and not allow_negative:
        raise WarehouseError(
            f"Giacenza insufficiente per {material.code}: disponibili "
            f"{float(material.qty_on_hand or 0):.3f}, richiesti {float(-qty):.3f}."
        )

    mv = StockMovement(
        material_id=material_id, warehouse_area_id=warehouse_area_id, qty=qty,
        unit_cost=Decimal(str(unit_cost)) if unit_cost is not None else None,
        movement_type=movement_type, source_type=source_type, source_id=source_id,
        doc_date=doc_date or db.func.current_date(), notes=notes, created_by_id=created_by_id,
    )
    if doc_date is None:
        from datetime import date
        mv.doc_date = date.today()
    db.session.add(mv)
    material.qty_on_hand = nuovo_saldo
    db.session.flush()
    if commit:
        db.session.commit()
    return mv


def bom_components(material_id):
    """Componenti di PRIMO livello della distinta base ATTIVA più recente del materiale
    (sostituisce get_bom). Lista vuota se il materiale non ha una BOM."""
    bom = (BillOfMaterial.query.filter_by(parent_material_id=material_id, active=True)
           .order_by(BillOfMaterial.id.desc()).first())
    if not bom:
        return []
    return bom.components


def explode_bom(material_id, qty, _visitati=None):
    """
    Esplosione multilivello: ritorna {material_id: qty_necessaria} con SOLO
    i componenti "foglia" (senza una propria BOM) — i semilavorati intermedi
    che hanno a loro volta una BOM vengono esplosi ricorsivamente invece di
    essere consumati come tali. _visitati previene loop su BOM cicliche
    (mai dovrebbero esistere, ma un errore di data entry non deve appendere
    il server).
    """
    if _visitati is None:
        _visitati = set()
    if material_id in _visitati:
        raise WarehouseError(f"Distinta base ciclica rilevata sull'articolo id={material_id}.")
    _visitati = _visitati | {material_id}

    qty = Decimal(str(qty))
    fabbisogno = {}
    comps = bom_components(material_id)
    if not comps:
        return {material_id: qty}
    for c in comps:
        qty_componente = (qty * Decimal(str(c.qty_per)) * (Decimal("1") + Decimal(str(c.scrap_pct or 0)) / 100)).quantize(Decimal("0.001"))
        figli = bom_components(c.component_material_id)
        if figli:
            sotto = explode_bom(c.component_material_id, qty_componente, _visitati)
            for mid, q in sotto.items():
                fabbisogno[mid] = fabbisogno.get(mid, Decimal("0")) + q
        else:
            fabbisogno[c.component_material_id] = fabbisogno.get(c.component_material_id, Decimal("0")) + qty_componente
    return fabbisogno


def fabbisogni_acquisto():
    """
    Sostituisce get_fabbisogni_acquisto(): per ogni materiale attivo, fabbisogno
    netto = impegnato su ordini cliente aperti (non ancora consegnato) − giacenza
    disponibile − in arrivo da ordini fornitore aperti (non ancora ricevuto).
    Nessun PDF, nessuna chiamata esterna: righe vere di SalesOrderLine/PurchaseOrderLine.
    """
    from models import SalesOrderLine, SalesOrder, PurchaseOrderLine, PurchaseOrder

    impegnato = {}
    for l in (SalesOrderLine.query.join(SalesOrder).filter(SalesOrder.status == "aperto").all()):
        residuo = Decimal(str(l.qty)) - Decimal(str(l.qty_delivered or 0))
        if residuo > 0:
            impegnato[l.material_id] = impegnato.get(l.material_id, Decimal("0")) + residuo

    in_arrivo = {}
    for l in PurchaseOrderLine.query.all():
        residuo = Decimal(str(l.qty)) - Decimal(str(l.qty_received or 0))
        if residuo > 0:
            in_arrivo[l.material_id] = in_arrivo.get(l.material_id, Decimal("0")) + residuo

    needs = []
    materiali = {m.id: m for m in Material.query.filter_by(active=True).all()}
    for mid, materiale in materiali.items():
        fabbisogno_netto = impegnato.get(mid, Decimal("0")) - Decimal(str(materiale.qty_on_hand or 0)) - in_arrivo.get(mid, Decimal("0"))
        if fabbisogno_netto > 0:
            needs.append({
                "material_id": mid, "sku": materiale.code, "descrizione": materiale.description,
                "stock": float(materiale.qty_on_hand or 0), "impegnato": float(impegnato.get(mid, Decimal("0"))),
                "in_arrivo": float(in_arrivo.get(mid, Decimal("0"))), "fabbisogno_netto": float(fabbisogno_netto),
            })
    return sorted(needs, key=lambda r: -r["fabbisogno_netto"])
