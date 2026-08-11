"""
services/reversals.py — Fase 4 (progettazione parti mancanti, punto 4):
storni completi di magazzino e produzione.

Il magazzino è ora un ledger interno (services/warehouse.py, StockMovement):
ogni storno qui sotto ripristina anche il movimento di magazzino, nella
stessa transazione della scrittura contabile e del ripristino qty_received/
qty_delivered — documento, giacenza e contabilità stornano sempre insieme.
"""
from datetime import datetime

from extensions import db
from models import GoodsReceipt, PurchaseOrderLine, Delivery, SalesOrderLine
from services.posting import _reverse_gl_only, PeriodClosedError
from services.warehouse import post_stock_movement, WarehouseError
from services.mm_invoice_quantities import (
    reconcile_invoiced_qty, has_untracked_active_invoice, lock_po_lines,
)


class ReversalError(ValueError):
    """Violazione di un vincolo di storno: dipendenza a valle non stornata,
    documento già stornato, o quantità coinvolta incoerente."""


def reverse_goods_receipt(receipt_id, reason, created_by_id=None):
    """Storna un'Entrata Merci: contro-movimento GL (chiude Magazzino,
    riapre EM/RF) + ripristino di qty_received sulle righe ordine — stessa
    transazione, o tutto o niente.

    Bloccato se una qualsiasi riga ricevuta è già stata (anche solo in
    parte) verificata in fattura (MIRO): va prima stornata la Verifica
    Fattura corrispondente, nell'ordine inverso a come sono stati creati.
    """
    if not reason or not reason.strip():
        raise ReversalError("Il motivo dello storno è obbligatorio.")

    receipt = GoodsReceipt.query.get(receipt_id)
    if receipt is None:
        raise ReversalError("Entrata Merci non trovata.")
    if receipt.is_reversed:
        raise ReversalError("Questa Entrata Merci è già stata stornata.")
    if receipt.journal_entry_id is None:
        raise ReversalError("Entrata Merci senza scrittura contabile collegata — dato incoerente.")

    locked_by_id = {line.id: line for line in lock_po_lines(receipt.po_id)}

    # Un KR MM legacy senza dettaglio per riga è reale, ma la quantità non è
    # ricostruibile con certezza: in questo caso il blocco resta conservativo.
    if has_untracked_active_invoice(receipt.po):
        raise ReversalError(
            f"L'ordine {receipt.po.doc_number} ha una Verifica Fattura MM attiva non tracciata per riga: "
            "riconcilia prima il documento."
        )

    # Blocco a catena: nessuna riga di questa GR può risultare già fatturata
    # oltre la quantità che RESTEREBBE ricevuta dopo lo storno. qty_invoiced
    # è una cache e viene quindi riconciliata prima del confronto.
    blocked = []
    for gr_line in receipt.lines:
        po_line = locked_by_id[gr_line.po_line_id]
        actual_invoiced = reconcile_invoiced_qty(po_line)
        residual_after = po_line.qty_received - gr_line.qty
        if actual_invoiced > residual_after:
            blocked.append(
                f"{po_line.material.code}: già fatturati {float(actual_invoiced):.0f}, "
                f"ma dopo lo storno resterebbero ricevuti solo {float(residual_after):.0f} — "
                f"storna prima la Verifica Fattura collegata."
            )
    if blocked:
        raise ReversalError("Impossibile stornare — " + "; ".join(blocked))

    try:
        new_entry = _reverse_gl_only(receipt.journal_entry, created_by_id=created_by_id)
        for gr_line in receipt.lines:
            locked_by_id[gr_line.po_line_id].qty_received -= gr_line.qty
            # Contro-movimento: la merce ricevuta esce di nuovo (lo storno di
            # un'Entrata Merci è, per il magazzino, uno scarico).
            post_stock_movement(
                material_id=locked_by_id[gr_line.po_line_id].material_id, qty=-gr_line.qty,
                movement_type="adjustment", source_type="goods_receipt_reversal", source_id=receipt.id,
                notes=f"Storno Entrata Merci {receipt.doc_number}: {reason.strip()}",
                created_by_id=created_by_id,
            )
        receipt.is_reversed = True
        receipt.reversal_reason = reason.strip()
        receipt.reversed_at = datetime.utcnow()
        receipt.reversed_by_id = created_by_id
        db.session.commit()
        return new_entry
    except Exception:
        db.session.rollback()
        raise


def reverse_delivery(delivery_id, reason, created_by_id=None):
    """Storna un DDT: contro-movimento GL del Costo del Venduto + ripristino
    di qty_delivered sull'ordine cliente — stessa transazione.

    Bloccato se il DDT è già stato fatturato al cliente (billing_entry_id
    valorizzato): va prima stornata la fattura, nell'ordine inverso a come
    sono stati creati.
    """
    if not reason or not reason.strip():
        raise ReversalError("Il motivo dello storno è obbligatorio.")

    delivery = Delivery.query.get(delivery_id)
    if delivery is None:
        raise ReversalError("DDT non trovato.")
    if delivery.is_reversed:
        raise ReversalError("Questo DDT è già stato stornato.")
    if delivery.billing_entry_id is not None:
        raise ReversalError(
            "Questo DDT è già stato fatturato al cliente — storna prima la fattura collegata."
        )
    if delivery.cogs_entry_id is None:
        raise ReversalError("DDT senza scrittura di Costo del Venduto collegata — dato incoerente.")

    try:
        new_entry = _reverse_gl_only(delivery.cogs_entry, created_by_id=created_by_id)
        for dl_line in delivery.lines:
            so_line = SalesOrderLine.query.filter_by(
                order_id=delivery.order_id, material_id=dl_line.material_id
            ).first()
            if so_line is not None:
                so_line.qty_delivered -= dl_line.qty
            # Contro-movimento: la merce spedita rientra in magazzino (lo
            # storno di un DDT è, per il magazzino, un carico).
            post_stock_movement(
                material_id=dl_line.material_id, qty=dl_line.qty,
                movement_type="adjustment", source_type="delivery_reversal", source_id=delivery.id,
                unit_cost=dl_line.unit_cost,
                notes=f"Storno DDT {delivery.doc_number}: {reason.strip()}",
                created_by_id=created_by_id,
            )
        delivery.is_reversed = True
        delivery.reversal_reason = reason.strip()
        delivery.reversed_at = datetime.utcnow()
        delivery.reversed_by_id = created_by_id
        db.session.commit()
        return new_entry
    except Exception:
        db.session.rollback()
        raise
