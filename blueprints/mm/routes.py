"""
blueprints/mm/routes.py — Ciclo passivo in stile SAP MM, con THREE-WAY MATCH.

Flusso:
    Ordine d'Acquisto (ME21N)
        → Entrata Merci (MIGO 101):  Dare Magazzino / Avere EM-RF (165000)
                                     al prezzo ordine + carico giacenza
        → Verifica Fattura (MIRO):   Dare EM-RF + IVA a Credito / Avere Fornitore
                                     SOLO se il three-way match passa:
                                       · Quantità fatturata ≤ quantità RICEVUTA
                                       · Prezzo fattura entro tolleranza dal prezzo ORDINE
                                     Altrimenti la fattura è BLOCCATA (come il
                                     blocco pagamento "R" di SAP) e non si registra.

Il conto transitorio EM/RF (in SAP: GR/IR clearing, WRX) garantisce che il
costo entri a magazzino al ricevimento merci, e che il debito verso il
fornitore nasca solo alla verifica fattura — mai doppie registrazioni.
"""
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user

from extensions import db
from models import (
    Account, EconomicSubject, Material, PurchaseOrder, PurchaseOrderLine,
    GoodsReceipt, GoodsReceiptLine,
)
from services.posting import post_journal_entry, UnbalancedEntryError

mm_bp = Blueprint("mm", __name__, template_folder="../../templates/mm")

PRICE_TOLERANCE_PCT = Decimal("2.0")   # tolleranza prezzo del three-way match


def _acc(code):
    a = Account.query.filter_by(code=code).first()
    if a is None:
        raise ValueError(f"Conto {code} mancante nel Piano dei Conti — lancia il seed.")
    return a


# ══════════════════════════════════════════════════════════════
# ORDINI D'ACQUISTO (ME21N)
# ══════════════════════════════════════════════════════════════
@mm_bp.route("/purchase-orders", methods=["GET", "POST"])
@login_required
def purchase_orders():
    vendors = EconomicSubject.query.filter_by(active=True, is_supplier=True).order_by(EconomicSubject.name).all()
    materials = Material.query.filter_by(active=True).order_by(Material.code).all()

    if request.method == "POST":
        vendor_id = request.form.get("vendor_id", type=int)
        if not vendor_id:
            flash("Seleziona il fornitore.", "danger")
        else:
            mat_by_id = {m.id: m for m in materials}
            rows, errors = [], []
            for i in range(1, 21):
                mid = request.form.get(f"material_id_{i}", type=int)
                if not mid:
                    continue
                qty = request.form.get(f"qty_{i}", type=float) or 0
                price = request.form.get(f"price_{i}", type=float)
                mat = mat_by_id.get(mid)
                if mat is None:
                    errors.append(f"Riga {i}: articolo non valido.")
                    continue
                if qty <= 0:
                    errors.append(f"Riga {i} ({mat.code}): quantità deve essere > 0.")
                    continue
                if price is None:
                    price = float(mat.standard_cost)
                rows.append({"material": mat, "qty": Decimal(str(qty)), "price": Decimal(str(price))})
            if not rows and not errors:
                errors.append("Inserisci almeno una riga articolo.")
            for e in errors:
                flash(e, "danger")
            if rows and not errors:
                from models import DocumentSequence
                po = PurchaseOrder(
                    doc_number=DocumentSequence.next_number("OA", "33"),
                    doc_date=datetime.strptime(request.form.get("doc_date"), "%Y-%m-%d").date()
                    if request.form.get("doc_date") else datetime.utcnow().date(),
                    economic_subject_id=vendor_id,
                    note=request.form.get("note", "").strip(),
                    created_by_id=current_user.id,
                )
                db.session.add(po)
                db.session.flush()
                for r in rows:
                    db.session.add(PurchaseOrderLine(po_id=po.id, material_id=r["material"].id,
                                                     qty=r["qty"], price=r["price"]))
                db.session.commit()
                flash(f"Ordine d'acquisto {po.doc_number} creato — "
                      f"totale {po.total_net:.2f} € netto.", "success")
                return redirect(url_for("mm.purchase_orders"))

    pos = PurchaseOrder.query.order_by(PurchaseOrder.id.desc()).all()
    return render_template("mm/purchase_orders.html", pos=pos,
                           vendors=vendors, materials=materials)


# ══════════════════════════════════════════════════════════════
# ENTRATA MERCI (MIGO 101) — Dare Magazzino / Avere EM-RF
# ══════════════════════════════════════════════════════════════
@mm_bp.route("/goods-receipts", methods=["GET", "POST"])
@login_required
def goods_receipts():
    open_pos = [p for p in PurchaseOrder.query.order_by(PurchaseOrder.id.desc()).all()
                if p.status in ("aperto", "parz. ricevuto")]

    if request.method == "POST":
        po_id = request.form.get("po_id", type=int)
        po = PurchaseOrder.query.get(po_id)
        if po is None:
            flash("Ordine d'acquisto non trovato.", "danger")
            return redirect(url_for("mm.goods_receipts"))

        try:
            emrf_acc = _acc("165000")
            from models import DocumentSequence
            gr = GoodsReceipt(
                doc_number=DocumentSequence.next_number("GR", "34"),
                po_id=po.id,
                ddt_vendor_ref=request.form.get("ddt_vendor_ref", "").strip(),
                created_by_id=current_user.id,
            )
            db.session.add(gr)
            db.session.flush()

            journal_lines = []
            total = Decimal("0")
            received_any = False
            for l in po.lines:
                qty = request.form.get(f"recv_qty_{l.id}", type=float)
                if qty is None:
                    # default: ricevi tutto il residuo
                    qty = float(Decimal(str(l.qty)) - Decimal(str(l.qty_received or 0)))
                qty = Decimal(str(qty))
                if qty <= 0:
                    continue
                residual = Decimal(str(l.qty)) - Decimal(str(l.qty_received or 0))
                if qty > residual:
                    raise ValueError(f"{l.material.code}: ricevi {float(qty):.0f} ma il residuo "
                                     f"ordine è {float(residual):.0f}. Non si riceve più dell'ordinato.")
                value = (qty * Decimal(str(l.price))).quantize(Decimal("0.01"))
                inv_acc = _acc(l.material.inventory_account_code)
                journal_lines.append({"account_id": inv_acc.id, "dare": value, "avere": 0,
                                      "description": f"Carico {l.material.code} × {float(qty):.0f}"})
                total += value
                db.session.add(GoodsReceiptLine(receipt_id=gr.id, po_line_id=l.id, qty=qty))
                # NOTA (decisione di Mauri): per ora MasterLedger SOLO LEGGE la
                # giacenza da MasterLogistic-WMS, non scrive ancora. L'aggiornamento
                # fisico della giacenza per questo DDT avviene nel processo di
                # MasterLogistic-WMS stesso (carico da DDT fornitore), non qui.
                l.qty_received = Decimal(str(l.qty_received or 0)) + qty
                received_any = True

            if not received_any:
                raise ValueError("Nessuna quantità da ricevere.")

            journal_lines.append({"account_id": emrf_acc.id, "dare": 0, "avere": total,
                                  "description": f"EM/RF ordine {po.doc_number}"})
            entry = post_journal_entry(
                doc_type="SA", prefix="10", doc_date=None,
                description=f"Entrata Merci {gr.doc_number} su OA {po.doc_number}"
                            + (f" — DDT forn. {gr.ddt_vendor_ref}" if gr.ddt_vendor_ref else ""),
                lines=journal_lines, source_module="MAGAZZINO",
                reference=gr.doc_number, created_by_id=current_user.id,
            )
            gr.journal_entry_id = entry.id
            db.session.commit()
            flash(f"Entrata Merci {gr.doc_number} registrata — carico magazzino "
                  f"{float(total):.2f} € (conto transitorio EM/RF).", "success")
        except (UnbalancedEntryError, ValueError) as e:
            db.session.rollback()
            flash(str(e), "danger")
        return redirect(url_for("mm.goods_receipts"))

    receipts = GoodsReceipt.query.order_by(GoodsReceipt.id.desc()).all()
    return render_template("mm/goods_receipts.html", receipts=receipts, open_pos=open_pos)


# ══════════════════════════════════════════════════════════════
# VERIFICA FATTURA (MIRO) — THREE-WAY MATCH
# ══════════════════════════════════════════════════════════════
@mm_bp.route("/invoice-verification", methods=["GET", "POST"])
@login_required
def invoice_verification():
    pos = [p for p in PurchaseOrder.query.order_by(PurchaseOrder.id.desc()).all()
           if any(Decimal(str(l.qty_received or 0)) > Decimal(str(l.qty_invoiced or 0))
                  for l in p.lines)]

    if request.method == "POST":
        po_id = request.form.get("po_id", type=int)
        po = PurchaseOrder.query.get(po_id)
        if po is None:
            flash("Ordine d'acquisto non trovato.", "danger")
            return redirect(url_for("mm.invoice_verification"))

        invoice_ref = request.form.get("invoice_ref", "").strip()
        vat_rate = Decimal(str(request.form.get("vat_rate", type=float) or 22))

        try:
            # ── THREE-WAY MATCH: Ordinato vs Ricevuto vs Fatturato ──
            blocked = []
            match_rows = []
            for l in po.lines:
                qty = request.form.get(f"inv_qty_{l.id}", type=float)
                price = request.form.get(f"inv_price_{l.id}", type=float)
                if qty is None:
                    qty = float(Decimal(str(l.qty_received or 0)) - Decimal(str(l.qty_invoiced or 0)))
                if price is None:
                    price = float(l.price)
                qty = Decimal(str(qty))
                price = Decimal(str(price))
                if qty <= 0:
                    continue

                open_qty = Decimal(str(l.qty_received or 0)) - Decimal(str(l.qty_invoiced or 0))
                # Regola 1 — quantità: non si fattura più del RICEVUTO
                if qty > open_qty:
                    blocked.append(f"{l.material.code}: fatturati {float(qty):.0f} ma ricevuti "
                                   f"non ancora fatturati {float(open_qty):.0f} "
                                   f"(ordinati {float(l.qty):.0f}, ricevuti {float(l.qty_received):.0f}).")
                # Regola 2 — prezzo: scostamento dal prezzo ORDINE entro tolleranza
                po_price = Decimal(str(l.price))
                if po_price > 0:
                    diff_pct = abs(price - po_price) / po_price * 100
                    if diff_pct > PRICE_TOLERANCE_PCT:
                        blocked.append(f"{l.material.code}: prezzo fattura {float(price):.4f} € vs "
                                       f"prezzo ordine {float(po_price):.4f} € — scostamento "
                                       f"{float(diff_pct):.1f}% oltre tolleranza {float(PRICE_TOLERANCE_PCT):.0f}%.")
                match_rows.append({"line": l, "qty": qty, "price": price})

            if not match_rows:
                raise ValueError("Nessuna quantità da fatturare.")
            if blocked:
                flash("FATTURA BLOCCATA — three-way match fallito:", "danger")
                for b in blocked:
                    flash(f"⛔ {b}", "danger")
                return redirect(url_for("mm.invoice_verification"))

            # ── Registrazione: Dare EM-RF + IVA / Avere Fornitore ──
            emrf_acc = _acc("165000")
            vat_acc = _acc("154000")
            ap_acc = _acc("210000")

            total_net = Decimal("0")
            journal_lines = []
            for r in match_rows:
                net = (r["qty"] * r["price"]).quantize(Decimal("0.01"))
                total_net += net
                journal_lines.append({"account_id": emrf_acc.id, "dare": net, "avere": 0,
                                      "description": f"Chiusura EM/RF {r['line'].material.code}"})
                r["line"].qty_invoiced = Decimal(str(r["line"].qty_invoiced or 0)) + r["qty"]
            total_vat = (total_net * vat_rate / 100).quantize(Decimal("0.01"))
            gross = total_net + total_vat
            if total_vat:
                journal_lines.append({"account_id": vat_acc.id, "dare": total_vat, "avere": 0,
                                      "description": f"IVA a credito {float(vat_rate):.0f}%"})
            journal_lines.append({"account_id": ap_acc.id, "dare": 0, "avere": gross})

            entry = post_journal_entry(
                doc_type="KR", prefix="19", doc_date=None,
                description=f"Verifica fattura {invoice_ref or 's.n.'} su OA {po.doc_number} "
                            f"(three-way match OK)",
                lines=journal_lines, source_module="ACQUISTI",
                reference=invoice_ref or po.doc_number, created_by_id=current_user.id,
                economic_subject_id=po.economic_subject_id, gross_amount=gross,
            )
            db.session.commit()
            flash(f"✅ Three-way match superato. Fattura {entry.doc_number} registrata — "
                  f"{float(gross):.2f} € (imponibile {float(total_net):.2f} + IVA {float(total_vat):.2f}). "
                  f"Debito v/{po.party.name} aperto in Pagamenti fornitori.", "success")
            return redirect(url_for("gl.entry_detail", entry_id=entry.id))
        except (UnbalancedEntryError, ValueError) as e:
            db.session.rollback()
            flash(str(e), "danger")
        return redirect(url_for("mm.invoice_verification"))

    return render_template("mm/invoice_verification.html", pos=pos,
                           tolerance=float(PRICE_TOLERANCE_PCT))
