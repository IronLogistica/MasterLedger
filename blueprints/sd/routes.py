"""
blueprints/sd/routes.py — Ciclo attivo in stile SAP SD.

Flusso documenti (copy control):
    Preventivo (VA21)  →  Ordine cliente (VA01)  →  DDT / Uscita merci (VL01N)
                                                        │  PGI: scarico giacenza +
                                                        │  Dare Costo del Venduto
                                                        │  Avere Magazzino
                                                        ▼
                                                    Fattura (VF01) — doc DR
                                                    integrata con AR e FatturaPA

Il COSTO DEL VENDUTO viene registrato AL MOMENTO DELL'USCITA MERCI, al costo
standard dell'articolo — esattamente come il movimento 601 di SAP. La fattura
poi registra solo Ricavi/IVA/Crediti. Il margine (Ricavo − COGS) è visibile
nel report Margini.
"""
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user

from extensions import db
from models import (
    Account, EconomicSubject, Material, Quotation, QuotationLine,
    SalesOrder, SalesOrderLine, Delivery, DeliveryLine, InvoiceLine,
    JournalEntry,
)
from services.posting import post_journal_entry, UnbalancedEntryError

sd_bp = Blueprint("sd", __name__, template_folder="../../templates/sd")


def _acc(code):
    a = Account.query.filter_by(code=code).first()
    if a is None:
        raise ValueError(f"Conto {code} mancante nel Piano dei Conti — lancia il seed.")
    return a


def _parse_lines(form, materials):
    """Legge le righe articolo (material_id_N, qty_N, price_N) dal form."""
    rows, errors = [], []
    mat_by_id = {m.id: m for m in materials}
    for i in range(1, 21):
        mid = form.get(f"material_id_{i}", type=int)
        if not mid:
            continue
        qty = form.get(f"qty_{i}", type=float) or 0
        price = form.get(f"price_{i}", type=float)
        mat = mat_by_id.get(mid)
        if mat is None:
            errors.append(f"Riga {i}: articolo non valido.")
            continue
        if qty <= 0:
            errors.append(f"Riga {i} ({mat.code}): quantità deve essere > 0.")
            continue
        if price is None:
            price = float(mat.sales_price)
        rows.append({"material": mat, "qty": Decimal(str(qty)), "price": Decimal(str(price))})
    if not rows and not errors:
        errors.append("Inserisci almeno una riga articolo.")
    return rows, errors


# ══════════════════════════════════════════════════════════════
# PREVENTIVI (VA21)
# ══════════════════════════════════════════════════════════════
@sd_bp.route("/quotations", methods=["GET", "POST"])
@login_required
def quotations():
    customers = EconomicSubject.query.filter_by(active=True, is_customer=True).order_by(EconomicSubject.name).all()
    materials = Material.query.filter_by(active=True).order_by(Material.code).all()

    if request.method == "POST":
        customer_id = request.form.get("customer_id", type=int)
        if not customer_id:
            flash("Seleziona il cliente.", "danger")
        else:
            rows, errors = _parse_lines(request.form, materials)
            for e in errors:
                flash(e, "danger")
            if rows and not errors:
                from models import DocumentSequence
                q = Quotation(
                    doc_number=DocumentSequence.next_number("QT", "30"),
                    doc_date=datetime.strptime(request.form.get("doc_date"), "%Y-%m-%d").date()
                    if request.form.get("doc_date") else datetime.utcnow().date(),
                    economic_subject_id=customer_id,
                    note=request.form.get("note", "").strip(),
                    created_by_id=current_user.id,
                )
                db.session.add(q)
                db.session.flush()
                for r in rows:
                    db.session.add(QuotationLine(quotation_id=q.id, material_id=r["material"].id,
                                                 qty=r["qty"], price=r["price"]))
                db.session.commit()
                flash(f"Preventivo {q.doc_number} creato — totale {q.total_net:.2f} € netto.", "success")
                return redirect(url_for("sd.quotations"))

    quots = Quotation.query.order_by(Quotation.id.desc()).all()
    return render_template("sd/quotations.html", quotations=quots,
                           customers=customers, materials=materials)


@sd_bp.route("/quotations/<int:quot_id>/convert", methods=["POST"])
@login_required
def quotation_convert(quot_id):
    """Copy control: Preventivo → Ordine cliente (tutte le righe)."""
    q = Quotation.query.get_or_404(quot_id)
    if q.status == "convertito":
        flash(f"Il preventivo {q.doc_number} è già stato convertito.", "warning")
        return redirect(url_for("sd.quotations"))

    from models import DocumentSequence
    o = SalesOrder(
        doc_number=DocumentSequence.next_number("OR", "31"),
        economic_subject_id=q.economic_subject_id, quotation_id=q.id,
        note=f"Da preventivo {q.doc_number}",
        created_by_id=current_user.id,
    )
    db.session.add(o)
    db.session.flush()
    for l in q.lines:
        db.session.add(SalesOrderLine(order_id=o.id, material_id=l.material_id,
                                      qty=l.qty, price=l.price))
    q.status = "convertito"
    db.session.commit()
    flash(f"Ordine {o.doc_number} creato da preventivo {q.doc_number}.", "success")
    return redirect(url_for("sd.orders"))


# ══════════════════════════════════════════════════════════════
# ORDINI CLIENTE (VA01)
# ══════════════════════════════════════════════════════════════
@sd_bp.route("/orders", methods=["GET", "POST"])
@login_required
def orders():
    customers = EconomicSubject.query.filter_by(active=True, is_customer=True).order_by(EconomicSubject.name).all()
    materials = Material.query.filter_by(active=True).order_by(Material.code).all()

    if request.method == "POST":
        customer_id = request.form.get("customer_id", type=int)
        if not customer_id:
            flash("Seleziona il cliente.", "danger")
        else:
            rows, errors = _parse_lines(request.form, materials)
            for e in errors:
                flash(e, "danger")
            if rows and not errors:
                from models import DocumentSequence
                o = SalesOrder(
                    doc_number=DocumentSequence.next_number("OR", "31"),
                    economic_subject_id=customer_id,
                    note=request.form.get("note", "").strip(),
                    created_by_id=current_user.id,
                )
                db.session.add(o)
                db.session.flush()
                for r in rows:
                    db.session.add(SalesOrderLine(order_id=o.id, material_id=r["material"].id,
                                                  qty=r["qty"], price=r["price"]))
                db.session.commit()
                flash(f"Ordine {o.doc_number} creato — totale {o.total_net:.2f} € netto.", "success")
                return redirect(url_for("sd.orders"))

    order_list = SalesOrder.query.order_by(SalesOrder.id.desc()).all()
    return render_template("sd/orders.html", orders=order_list,
                           customers=customers, materials=materials)


# ══════════════════════════════════════════════════════════════
# DDT / USCITA MERCI (VL01N + PGI 601) — qui nasce il COSTO DEL VENDUTO
# ══════════════════════════════════════════════════════════════
@sd_bp.route("/deliveries", methods=["GET", "POST"])
@login_required
def deliveries():
    open_orders = [o for o in SalesOrder.query.order_by(SalesOrder.id.desc()).all()
                   if o.status == "aperto"]

    if request.method == "POST":
        order_id = request.form.get("order_id", type=int)
        o = SalesOrder.query.get(order_id)
        if o is None:
            flash("Ordine non trovato.", "danger")
            return redirect(url_for("sd.deliveries"))
        if o.status != "aperto":
            flash(f"L'ordine {o.doc_number} è già stato consegnato.", "warning")
            return redirect(url_for("sd.deliveries"))

        # ── controllo disponibilità ──────────────────────────
        to_ship = []
        for l in o.lines:
            residual = Decimal(str(l.qty)) - Decimal(str(l.qty_delivered or 0))
            if residual <= 0:
                continue
            if Decimal(str(l.material.qty_on_hand)) < residual:
                flash(f"Giacenza insufficiente per {l.material.code}: "
                      f"disponibili {float(l.material.qty_on_hand):.0f}, richiesti {float(residual):.0f}. "
                      f"Registra prima un'Entrata Merci (MM).", "danger")
                return redirect(url_for("sd.deliveries"))
            to_ship.append((l, residual))
        if not to_ship:
            flash("Nulla da consegnare su questo ordine.", "warning")
            return redirect(url_for("sd.deliveries"))

        try:
            from models import DocumentSequence
            d = Delivery(
                doc_number=DocumentSequence.next_number("DL", "32"),
                order_id=o.id, economic_subject_id=o.economic_subject_id,
                created_by_id=current_user.id,
            )
            db.session.add(d)
            db.session.flush()

            # ── PGI: scarico giacenza + scrittura COGS ───────
            cogs_acc = _acc("450000")
            journal_lines = []
            total_cogs = Decimal("0")
            for l, qty in to_ship:
                unit_cost = Decimal(str(l.material.standard_cost))
                line_cogs = (qty * unit_cost).quantize(Decimal("0.01"))
                db.session.add(DeliveryLine(delivery_id=d.id, material_id=l.material_id,
                                            qty=qty, price=l.price, unit_cost=unit_cost))
                l.material.qty_on_hand = Decimal(str(l.material.qty_on_hand)) - qty
                l.qty_delivered = Decimal(str(l.qty_delivered or 0)) + qty
                if line_cogs > 0:
                    inv_acc = _acc(l.material.inventory_account_code)
                    journal_lines.append({"account_id": cogs_acc.id, "dare": line_cogs, "avere": 0,
                                          "description": f"COGS {l.material.code} × {float(qty):.0f}"})
                    journal_lines.append({"account_id": inv_acc.id, "dare": 0, "avere": line_cogs,
                                          "description": f"Scarico {l.material.code}"})
                    total_cogs += line_cogs

            if journal_lines:
                entry = post_journal_entry(
                    doc_type="SA", prefix="10", doc_date=None,
                    description=f"Uscita merci DDT {d.doc_number} (ord. {o.doc_number}) — Costo del Venduto",
                    lines=journal_lines, source_module="VENDITE",
                    reference=d.doc_number, created_by_id=current_user.id,
                )
                d.cogs_entry_id = entry.id

            if all(Decimal(str(l.qty_delivered or 0)) >= Decimal(str(l.qty)) for l in o.lines):
                o.status = "consegnato"
            db.session.commit()
            flash(f"DDT {d.doc_number} registrato — Uscita Merci eseguita, "
                  f"Costo del Venduto {float(total_cogs):.2f} € contabilizzato.", "success")
        except (UnbalancedEntryError, ValueError) as e:
            db.session.rollback()
            flash(str(e), "danger")
        return redirect(url_for("sd.deliveries"))

    delivery_list = Delivery.query.order_by(Delivery.id.desc()).all()
    return render_template("sd/deliveries.html", deliveries=delivery_list,
                           open_orders=open_orders)


# ══════════════════════════════════════════════════════════════
# FATTURAZIONE DDT (VF01) — crea la fattura DR integrata con AR/FatturaPA
# ══════════════════════════════════════════════════════════════
@sd_bp.route("/billing", methods=["GET", "POST"])
@login_required
def billing():
    to_bill = Delivery.query.filter_by(billing_entry_id=None).order_by(Delivery.id.desc()).all()

    if request.method == "POST":
        delivery_id = request.form.get("delivery_id", type=int)
        d = Delivery.query.get(delivery_id)
        if d is None:
            flash("DDT non trovato.", "danger")
            return redirect(url_for("sd.billing"))
        if d.is_billed:
            flash(f"Il DDT {d.doc_number} è già stato fatturato.", "warning")
            return redirect(url_for("sd.billing"))

        try:
            ar_acc = _acc("140000")
            vat_acc = _acc("170000")
            rev_acc = _acc("4000")

            total_net = Decimal("0")
            total_vat = Decimal("0")
            journal_lines = []
            inv_rows = []
            for l in d.lines:
                net = (Decimal(str(l.qty)) * Decimal(str(l.price))).quantize(Decimal("0.01"))
                vat = (net * Decimal(str(l.material.vat_rate)) / 100).quantize(Decimal("0.01"))
                total_net += net
                total_vat += vat
                journal_lines.append({"account_id": rev_acc.id, "dare": 0, "avere": net,
                                      "description": f"{l.material.code} — {l.material.description}"})
                inv_rows.append((f"{l.material.code} — {l.material.description} "
                                 f"({float(l.qty):.0f} {l.material.uom} × {float(l.price):.2f} €)",
                                 net, Decimal(str(l.material.vat_rate))))
            gross = total_net + total_vat
            journal_lines.insert(0, {"account_id": ar_acc.id, "dare": gross, "avere": 0})
            if total_vat:
                journal_lines.append({"account_id": vat_acc.id, "dare": 0, "avere": total_vat})

            vat_rates = {r[2] for r in inv_rows}
            entry = post_journal_entry(
                doc_type="DR", prefix="14", doc_date=None,
                description=f"Fattura da DDT {d.doc_number} (ord. {d.order.doc_number})",
                lines=journal_lines, source_module="VENDITE",
                reference=d.doc_number, created_by_id=current_user.id,
                economic_subject_id=d.economic_subject_id, gross_amount=gross,
                vat_rate=(vat_rates.pop() if len(vat_rates) == 1 else None),
            )
            for n, (desc, net, rate) in enumerate(inv_rows, start=1):
                db.session.add(InvoiceLine(entry_id=entry.id, line_number=n, description=desc,
                                           amount=net, vat_rate=rate, account_id=rev_acc.id))
            d.billing_entry_id = entry.id
            db.session.commit()
            flash(f"Fattura {entry.doc_number} creata da DDT {d.doc_number} — "
                  f"totale {float(gross):.2f} € (imponibile {float(total_net):.2f} + "
                  f"IVA {float(total_vat):.2f}).", "success")
            return redirect(url_for("gl.entry_detail", entry_id=entry.id))
        except (UnbalancedEntryError, ValueError) as e:
            db.session.rollback()
            flash(str(e), "danger")
        return redirect(url_for("sd.billing"))

    billed = Delivery.query.filter(Delivery.billing_entry_id.isnot(None)) \
                           .order_by(Delivery.id.desc()).limit(30).all()
    return render_template("sd/billing.html", to_bill=to_bill, billed=billed)


# ══════════════════════════════════════════════════════════════
# REPORT MARGINI — Ricavi vs Costo del Venduto per documento
# ══════════════════════════════════════════════════════════════
@sd_bp.route("/margini")
@login_required
def margini():
    billed = Delivery.query.filter(Delivery.billing_entry_id.isnot(None)) \
                           .order_by(Delivery.id.desc()).all()
    rows = []
    tot_rev = tot_cogs = 0.0
    for d in billed:
        rev = d.total_net
        cogs = d.total_cogs
        rows.append({"delivery": d, "revenue": rev, "cogs": cogs,
                     "margin": rev - cogs,
                     "margin_pct": (rev - cogs) / rev * 100 if rev else 0})
        tot_rev += rev
        tot_cogs += cogs
    return render_template("sd/margini.html", rows=rows, tot_rev=tot_rev,
                           tot_cogs=tot_cogs, tot_margin=tot_rev - tot_cogs,
                           tot_pct=(tot_rev - tot_cogs) / tot_rev * 100 if tot_rev else 0)
