"""
blueprints/sd/routes.py — Ciclo attivo Vendite e Spedizione (VS).

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
    Account, AccountMapping, CostCenter, EconomicSubject, Material, Quotation, QuotationLine,
    SalesOrder, SalesOrderLine, Delivery, DeliveryLine, InvoiceLine,
    JournalEntry, JournalLine, InvoiceInstallment, PaymentAllocation,
)
from services.posting import post_journal_entry, UnbalancedEntryError, _reverse_gl_only
from services.co import validate_co_assignment, COValidationError
from services.reversals import reverse_delivery, ReversalError
from services.warehouse import current_stock, post_stock_movement, WarehouseError

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


@sd_bp.route("/quotations/<int:quot_id>/conferma")
@login_required
def quotation_confirmation(quot_id):
    """Conferma di Preventivo — documento formale da mandare al cliente,
    stampabile/salvabile in PDF. Stesso principio già usato per la Conferma
    d'Ordine: non è un nuovo modello dati, rilegge il Preventivo esistente."""
    q = Quotation.query.get_or_404(quot_id)
    return render_template("sd/quotation_confirmation.html", q=q)


def _elimina_pagamenti_orfani(entry_ids_da_eliminare):
    """Helper condiviso da tutte le cancellazioni VS sotto: dato un elenco
    di JournalEntry che stanno per essere eliminati, rimuove le
    allocazioni di pagamento verso le loro rate e — SOLO se un incasso
    resta senza più nessun legame verso fatture NON coinvolte in questa
    cancellazione — elimina anche quell'incasso. Se l'incasso pagava
    ANCHE una fattura non toccata da questa cancellazione, resta intatto."""
    installment_ids = [i.id for i in InvoiceInstallment.query.filter(
        InvoiceInstallment.entry_id.in_(entry_ids_da_eliminare)).all()]
    payment_entry_ids = set()
    if installment_ids:
        for pa in PaymentAllocation.query.filter(PaymentAllocation.installment_id.in_(installment_ids)).all():
            payment_entry_ids.add(pa.payment_entry_id)
            db.session.delete(pa)
    for e in JournalEntry.query.filter(JournalEntry.id.in_(entry_ids_da_eliminare)).all():
        if e.paid_by_entry_id:
            payment_entry_ids.add(e.paid_by_entry_id)
    db.session.flush()

    for peid in payment_entry_ids:
        ancora_usato = PaymentAllocation.query.filter_by(payment_entry_id=peid).first() is not None
        ancora_paga_altro = JournalEntry.query.filter(
            JournalEntry.paid_by_entry_id == peid,
            ~JournalEntry.id.in_(entry_ids_da_eliminare)
        ).first() is not None
        if not ancora_usato and not ancora_paga_altro:
            # Scollega PRIMA i riferimenti dalle fatture che stiamo per
            # eliminare (anche loro puntano a peid via paid_by_entry_id) —
            # senza questo, Postgres rifiuta la cancellazione per il
            # vincolo di chiave esterna, esattamente come già successo
            # sul cogs_entry_id di Delivery.
            JournalEntry.query.filter(
                JournalEntry.id.in_(entry_ids_da_eliminare),
                JournalEntry.paid_by_entry_id == peid
            ).update({"paid_by_entry_id": None}, synchronize_session=False)
            db.session.flush()
            JournalLine.query.filter_by(entry_id=peid).delete(synchronize_session=False)
            JournalEntry.query.filter_by(id=peid).delete(synchronize_session=False)


def _elimina_scrittura_sd(entry_id):
    """Elimina una scrittura VS (Fattura da DDT o Costo del Venduto) e
    tutto ciò che ne dipende, gestendo con cautela eventuali pagamenti.
    Blocca se la scrittura è coinvolta in uno storno (originale o storno
    stesso): eliminarla lascerebbe un riferimento rotto sull'altro lato,
    stesso tipo di violazione già vista sul collegamento DDT→COGS."""
    entry = JournalEntry.query.get(entry_id)
    if entry is None:
        return
    if entry.is_reversed or entry.reversed_by_id or entry.reverses_id:
        raise ValueError(
            f"Il documento {entry.doc_number} è collegato a uno storno — non eliminabile da qui."
        )
    _elimina_pagamenti_orfani([entry_id])
    InvoiceInstallment.query.filter_by(entry_id=entry_id).delete(synchronize_session=False)
    InvoiceLine.query.filter_by(entry_id=entry_id).delete(synchronize_session=False)
    JournalLine.query.filter_by(entry_id=entry_id).delete(synchronize_session=False)
    JournalEntry.query.filter_by(id=entry_id).delete(synchronize_session=False)


@sd_bp.route("/fatture/<int:entry_id>/elimina", methods=["POST"])
@login_required
def fattura_elimina(entry_id):
    """Elimina una fattura generata da DDT (Fatturazione DDT) — solo se
    generata dal ciclo VS (source_module='VENDITE'). Per dati di prova,
    non per correggere fatture reali già emesse: quelle si stornano."""
    entry = JournalEntry.query.get_or_404(entry_id)
    if entry.source_module != "VENDITE" or entry.doc_type not in ("DR", "DG"):
        flash("Questa fattura non è stata generata dal ciclo VS — non eliminabile da qui.", "danger")
        return redirect(url_for("sd.billing"))
    doc_number = entry.doc_number
    delivery = Delivery.query.filter_by(billing_entry_id=entry.id).first()
    if delivery:
        delivery.billing_entry_id = None
        db.session.flush()  # scollega davvero prima del delete, non fidarsi dell'autoflush implicito
    try:
        _elimina_scrittura_sd(entry.id)
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "danger")
        return redirect(url_for("sd.billing"))
    db.session.commit()
    flash(f"Fattura {doc_number} eliminata. Il DDT collegato torna disponibile per una nuova fatturazione.", "success")
    return redirect(url_for("sd.billing"))


@sd_bp.route("/deliveries/<int:delivery_id>/elimina", methods=["POST"])
@login_required
def delivery_elimina(delivery_id):
    """Elimina un DDT — blocca se è già stato fatturato: elimina prima la fattura."""
    d = Delivery.query.get_or_404(delivery_id)
    if d.billing_entry_id is not None:
        flash(f"Il DDT {d.doc_number} è già fatturato — elimina prima la fattura collegata.", "danger")
        return redirect(url_for("sd.deliveries"))
    doc_number = d.doc_number
    cogs_entry_id = d.cogs_entry_id
    d.cogs_entry_id = None  # scollega PRIMA di eliminare la scrittura — Postgres
    db.session.flush()      # applica subito l'UPDATE, altrimenti il vincolo di
                             # chiave esterna blocca la cancellazione della scrittura
    if cogs_entry_id:
        try:
            _elimina_scrittura_sd(cogs_entry_id)
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "danger")
            return redirect(url_for("sd.deliveries"))
    for dl_line in d.lines:
        so_line = SalesOrderLine.query.filter_by(order_id=d.order_id, material_id=dl_line.material_id).first()
        if so_line is not None:
            so_line.qty_delivered = max(Decimal("0"), Decimal(str(so_line.qty_delivered or 0)) - Decimal(str(dl_line.qty)))
    # Le righe (DeliveryLine) sono già eliminate in automatico dal cascade
    # della relationship — qui basta il delete dell'aggregato.
    db.session.delete(d)
    db.session.commit()
    flash(f"DDT {doc_number} eliminato. Le quantità sull'ordine sono state ripristinate.", "success")
    return redirect(url_for("sd.deliveries"))


@sd_bp.route("/orders/<int:order_id>/elimina", methods=["POST"])
@login_required
def order_elimina(order_id):
    """Elimina un ordine cliente — blocca se ha già DDT collegati: elimina prima quelli."""
    o = SalesOrder.query.get_or_404(order_id)
    if Delivery.query.filter_by(order_id=o.id).first():
        flash(f"L'ordine {o.doc_number} ha già DDT collegati — eliminali prima.", "danger")
        return redirect(url_for("sd.orders"))
    doc_number = o.doc_number
    if o.quotation_id:
        q = Quotation.query.get(o.quotation_id)
        if q and q.status == "convertito":
            q.status = "aperto"  # il preventivo torna convertibile
    # Le righe (SalesOrderLine) sono già eliminate in automatico dal cascade.
    db.session.delete(o)
    db.session.commit()
    flash(f"Ordine {doc_number} eliminato.", "success")
    return redirect(url_for("sd.orders"))


@sd_bp.route("/quotations/<int:quot_id>/elimina", methods=["POST"])
@login_required
def quotation_elimina(quot_id):
    """Elimina un preventivo — blocca se già convertito in ordine: elimina prima l'ordine."""
    q = Quotation.query.get_or_404(quot_id)
    if SalesOrder.query.filter_by(quotation_id=q.id).first():
        flash(f"Il preventivo {q.doc_number} è già stato convertito in ordine — elimina prima l'ordine.", "danger")
        return redirect(url_for("sd.quotations"))
    doc_number = q.doc_number
    # Le righe (QuotationLine) sono già eliminate in automatico dal cascade.
    db.session.delete(q)
    db.session.commit()
    flash(f"Preventivo {doc_number} eliminato.", "success")
    return redirect(url_for("sd.quotations"))


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


@sd_bp.route("/orders/<int:order_id>/conferma")
@login_required
def order_confirmation(order_id):
    """
    Conferma d'Ordine — documento formale da mandare al cliente, distinto
    dall'Ordine stesso (SalesOrder) che resta il record di sistema. Non è un
    nuovo modello dati: rilegge i dati del SalesOrder e li presenta in un
    layout stampabile pensato per il cliente (niente dettagli interni).
    """
    o = SalesOrder.query.get_or_404(order_id)
    return render_template("sd/order_confirmation.html", o=o)


# ══════════════════════════════════════════════════════════════
# DDT / USCITA MERCI (VL01N + PGI 601) — qui nasce il COSTO DEL VENDUTO
# ══════════════════════════════════════════════════════════════
@sd_bp.route("/deliveries", methods=["GET", "POST"])
@login_required
def deliveries():
    open_orders = [o for o in SalesOrder.query.order_by(SalesOrder.id.desc()).all()
                   if o.status == "aperto"]
    cost_centers = CostCenter.query.filter_by(active=True).order_by(CostCenter.code).all()

    if request.method == "POST":
        order_id = request.form.get("order_id", type=int)
        cost_center_id_default = request.form.get("cost_center_id", type=int)  # riserva: usato solo se una riga non ha il suo
        o = SalesOrder.query.get(order_id)
        if o is None:
            flash("Ordine non trovato.", "danger")
            return redirect(url_for("sd.deliveries"))
        if o.status != "aperto":
            flash(f"L'ordine {o.doc_number} è già stato consegnato.", "warning")
            return redirect(url_for("sd.deliveries"))

        # ── controllo disponibilità: da qui in poi il magazzino è INTERNO a
        # MasterLedger (ledger StockMovement) — non serve più MasterLogistic-WMS
        # né un parser di alcun tipo, perché l'ordine cliente è già una riga
        # vera in questo database (SalesOrderLine), non un PDF da rileggere.
        to_ship = []
        insufficienti = []
        for l in o.lines:
            residual = Decimal(str(l.qty)) - Decimal(str(l.qty_delivered or 0))
            if residual <= 0:
                continue
            disponibile = Decimal(str(l.material.qty_on_hand or 0))
            if disponibile < residual:
                insufficienti.append(
                    f"{l.material.code}: disponibili {float(disponibile):.0f}, richiesti {float(residual):.0f}"
                )
                continue
            to_ship.append((l, residual))
        if insufficienti:
            flash("Giacenza insufficiente — " + "; ".join(insufficienti) +
                  ". Registra prima un'Entrata Merci (AM).", "danger")
            return redirect(url_for("sd.deliveries"))
        if not to_ship:
            flash("Nulla da consegnare su questo ordine.", "warning")
            return redirect(url_for("sd.deliveries"))

        try:
            from models import DocumentSequence
            d = Delivery(
                doc_number=DocumentSequence.next_number("DL", "32"),
                order_id=o.id, economic_subject_id=o.economic_subject_id,
                created_by_id=current_user.id, stock_verified=True,
            )
            db.session.add(d)
            db.session.flush()

            # ── PGI: scarico giacenza + scrittura COGS ───────
            # Il Costo del Venduto (450000) è CO-rilevante — il centro di
            # costo/ricavo è richiesto PER RIGA: un DDT multi-articolo può
            # avere articoli diversi che vanno spesati su centri diversi
            # (es. reparto Produzione per uno, Servizi per un altro).
            cogs_acc = _acc("450000")
            journal_lines = []
            total_cogs = Decimal("0")
            for l, qty in to_ship:
                riga_cc_id = request.form.get(f"cost_center_id_{l.id}", type=int) or cost_center_id_default
                _, riga_cost_center = validate_co_assignment(cogs_acc.id, riga_cc_id)
                unit_cost = Decimal(str(l.material.standard_cost))
                line_cogs = (qty * unit_cost).quantize(Decimal("0.01"))
                dl = DeliveryLine(delivery_id=d.id, material_id=l.material_id,
                                  qty=qty, price=l.price, unit_cost=unit_cost)
                db.session.add(dl)
                db.session.flush()
                # Scarico REALE di magazzino (PGI): il ledger interno sostituisce
                # la sola-lettura da MasterLogistic-WMS — questo è l'unico posto
                # in cui la giacenza del componente diminuisce davvero.
                post_stock_movement(
                    material_id=l.material_id, qty=-qty, movement_type="delivery",
                    source_type="delivery_line", source_id=dl.id, unit_cost=unit_cost,
                    notes=f"DDT {d.doc_number}", created_by_id=current_user.id,
                )
                l.qty_delivered = Decimal(str(l.qty_delivered or 0)) + qty
                if line_cogs > 0:
                    inv_acc = _acc(l.material.inventory_account_code)
                    journal_lines.append({"account_id": cogs_acc.id, "dare": line_cogs, "avere": 0,
                                          "description": f"COGS {l.material.code} × {float(qty):.0f}",
                                          "cost_center_id": riga_cost_center.id if riga_cost_center else None})
                    journal_lines.append({"account_id": inv_acc.id, "dare": 0, "avere": line_cogs,
                                          "description": f"Scarico {l.material.code}"})
                    total_cogs += line_cogs

            if journal_lines:
                entry = post_journal_entry(
                    doc_type="SA", prefix="10", doc_date=None,
                    description=f"Uscita merci DDT {d.doc_number} (ord. {o.doc_number}) — Costo del Venduto",
                    lines=journal_lines, source_module="VENDITE",
                    reference=d.doc_number, created_by_id=current_user.id, commit=False,
                )
                d.cogs_entry_id = entry.id

            if all(Decimal(str(l.qty_delivered or 0)) >= Decimal(str(l.qty)) for l in o.lines):
                o.status = "consegnato"
            db.session.commit()
            flash(f"DDT {d.doc_number} registrato — Uscita Merci eseguita, "
                  f"Costo del Venduto {float(total_cogs):.2f} € contabilizzato.", "success")
        except (UnbalancedEntryError, ValueError, WarehouseError) as e:
            db.session.rollback()
            flash(str(e), "danger")
        return redirect(url_for("sd.deliveries"))

    delivery_list = Delivery.query.order_by(Delivery.id.desc()).all()
    # Righe aperte di ogni ordine, per popolare via JS la tabella "un
    # centro per riga" quando l'operatore sceglie l'ordine da consegnare.
    order_lines_json = {
        o.id: [
            {"line_id": l.id, "code": l.material.code, "description": l.material.description,
             "residual": float(Decimal(str(l.qty)) - Decimal(str(l.qty_delivered or 0)))}
            for l in o.lines if Decimal(str(l.qty)) - Decimal(str(l.qty_delivered or 0)) > 0
        ] for o in open_orders
    }
    return render_template("sd/deliveries.html", deliveries=delivery_list,
                           open_orders=open_orders, cost_centers=cost_centers,
                           order_lines_json=order_lines_json)


@sd_bp.route("/deliveries/<int:delivery_id>/reverse", methods=["POST"])
@login_required
def deliveries_reverse(delivery_id):
    """Fase 4 (progettazione parti mancanti, punto 4) — storno di dominio."""
    reason = (request.form.get("reason") or "").strip()
    try:
        reverse_delivery(delivery_id, reason, created_by_id=current_user.id)
        flash("DDT stornato: quantità ordine ripristinate e Costo del Venduto contro-mosso.", "success")
    except ReversalError as e:
        flash(str(e), "danger")
    return redirect(url_for("sd.deliveries"))


# ══════════════════════════════════════════════════════════════
# FATTURAZIONE DDT (VF01) — crea la fattura DR integrata con AR/FatturaPA
# ══════════════════════════════════════════════════════════════
# FATTURE DA EMETTERE (rateo attivo) — DDT spediti non ancora fatturati
# ══════════════════════════════════════════════════════════════
def _revenue_account_for_delivery(d):
    """Stessa logica di scelta conto ricavi già usata in billing() —
    centralizzata qui perché serve identica anche per il rateo."""
    channel = d.party.revenue_channel if d.party else None
    if channel == "affidamento_diretto":
        return _acc("4001"), channel
    return _acc("4000"), channel


def _genera_rateo_ddt(d, cost_center_id=None, cost_center_ids=None):
    """Genera la scrittura provvisoria di competenza per un DDT spedito ma
    non ancora fatturato: Dare Fatture da Emettere / Avere Ricavi (SENZA
    IVA — non è ancora dovuta, non esiste una vera fattura). Va SEMPRE
    stornata quando arriva la fattura reale (vedi billing()).

    cost_center_ids: dict {delivery_line_id: cost_center_id} per un centro
    diverso per riga (generazione singola); se assente o una riga non è
    presente nel dict, si usa cost_center_id come riserva unica (usato
    dalla generazione massiva, dove non c'è un form per riga)."""
    if d.cogs_entry_id is None:
        raise ValueError(f"DDT {d.doc_number}: nessuna Uscita Merci collegata, non generabile.")
    if d.billing_entry_id is not None:
        raise ValueError(f"DDT {d.doc_number}: già fatturato, il rateo non serve più.")
    if d.accrual_entry_id is not None:
        raise ValueError(f"DDT {d.doc_number}: rateo già generato in precedenza.")

    fde_acc = AccountMapping.get_or_error("fatture_da_emettere")
    rev_acc, channel = _revenue_account_for_delivery(d)
    cost_center_ids = cost_center_ids or {}

    total_net = Decimal("0")
    journal_lines = []
    for l in d.lines:
        net = (Decimal(str(l.qty)) * Decimal(str(l.price))).quantize(Decimal("0.01"))
        total_net += net
        riga_cc_id = cost_center_ids.get(l.id) or cost_center_id
        riga_cc = CostCenter.query.get(riga_cc_id) if riga_cc_id else None
        journal_lines.append({"account_id": rev_acc.id, "dare": 0, "avere": net,
                              "description": f"Rateo — {l.material.code} - {l.material.description}",
                              "cost_center_id": riga_cc.id if riga_cc else None})
    journal_lines.insert(0, {"account_id": fde_acc.id, "dare": total_net, "avere": 0})

    entry = post_journal_entry(
        doc_type="SA", prefix="10", doc_date=None,
        description=f"Rateo di competenza — DDT {d.doc_number} non ancora fatturato",
        lines=journal_lines, source_module="VENDITE", reference=d.doc_number,
        created_by_id=current_user.id, economic_subject_id=d.economic_subject_id,
        gross_amount=total_net, commit=False,
    )
    d.accrual_entry_id = entry.id
    db.session.flush()
    return entry


@sd_bp.route("/fatture-da-emettere", methods=["GET"])
@login_required
def fatture_da_emettere():
    """Elenco DDT spediti ma non ancora fatturati — con la possibilità di
    generare (o vedere già generato) il rateo di competenza di fine periodo."""
    da_fatturare = (Delivery.query
                    .filter(Delivery.billing_entry_id.is_(None), Delivery.cogs_entry_id.isnot(None))
                    .order_by(Delivery.id.desc()).all())
    cost_centers = CostCenter.query.filter_by(active=True).order_by(CostCenter.code).all()
    valori_stimati = {
        d.id: sum((Decimal(str(l.qty)) * Decimal(str(l.price)) for l in d.lines), Decimal("0"))
        for d in da_fatturare
    }
    return render_template("sd/fatture_da_emettere.html", deliveries=da_fatturare,
                           cost_centers=cost_centers, valori_stimati=valori_stimati)


@sd_bp.route("/fatture-da-emettere/<int:delivery_id>/genera", methods=["POST"])
@login_required
def fatture_da_emettere_genera(delivery_id):
    d = Delivery.query.get_or_404(delivery_id)
    cost_center_id = request.form.get("cost_center_id", type=int)  # riserva
    cost_center_ids = {}
    for l in d.lines:
        v = request.form.get(f"cost_center_id_{l.id}", type=int)
        if v:
            cost_center_ids[l.id] = v
    try:
        entry = _genera_rateo_ddt(d, cost_center_id, cost_center_ids)
        db.session.commit()
        flash(f"Rateo generato per DDT {d.doc_number}: Doc. {entry.doc_number} — "
              f"{float(entry.gross_amount):.2f} € di ricavo di competenza.", "success")
    except (ValueError, UnbalancedEntryError) as e:
        db.session.rollback()
        flash(str(e), "danger")
    return redirect(url_for("sd.fatture_da_emettere"))


@sd_bp.route("/fatture-da-emettere/genera-tutti", methods=["POST"])
@login_required
def fatture_da_emettere_genera_tutti():
    """Genera il rateo per TUTTI i DDT spediti/non fatturati/senza rateo
    già esistente — pensato per la chiusura di fine periodo."""
    cost_center_id = request.form.get("cost_center_id", type=int)
    candidati = (Delivery.query
                .filter(Delivery.billing_entry_id.is_(None), Delivery.cogs_entry_id.isnot(None),
                        Delivery.accrual_entry_id.is_(None)).all())
    generati, errori = 0, []
    for d in candidati:
        try:
            _genera_rateo_ddt(d, cost_center_id)
            generati += 1
        except (ValueError, UnbalancedEntryError) as e:
            errori.append(str(e))
    if generati:
        db.session.commit()
    if errori:
        for e in errori:
            flash(e, "danger")
    flash(f"Generati {generati} ratei su {len(candidati)} DDT candidati.",
          "success" if generati else "warning")
    return redirect(url_for("sd.fatture_da_emettere"))


@sd_bp.route("/fatture-da-emettere/<int:delivery_id>/storna", methods=["POST"])
@login_required
def fatture_da_emettere_storna(delivery_id):
    """Storna un rateo generato per errore (senza dover aspettare la
    fattura vera) — usa lo stesso storno di dominio già collaudato."""
    d = Delivery.query.get_or_404(delivery_id)
    if d.accrual_entry_id is None:
        flash(f"DDT {d.doc_number}: nessun rateo da stornare.", "warning")
        return redirect(url_for("sd.fatture_da_emettere"))
    entry = JournalEntry.query.get(d.accrual_entry_id)
    try:
        _reverse_gl_only(entry, created_by_id=current_user.id)
        d.accrual_entry_id = None
        db.session.commit()
        flash(f"Rateo per DDT {d.doc_number} stornato.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Errore nello storno: {e}", "danger")
    return redirect(url_for("sd.fatture_da_emettere"))


# ══════════════════════════════════════════════════════════════
@sd_bp.route("/billing", methods=["GET", "POST"])
@login_required
def billing():
    to_bill = Delivery.query.filter_by(billing_entry_id=None).order_by(Delivery.id.desc()).all()
    cost_centers = CostCenter.query.filter_by(active=True).order_by(CostCenter.code).all()

    if request.method == "POST":
        delivery_id = request.form.get("delivery_id", type=int)
        d = Delivery.query.get(delivery_id)
        if d is None:
            flash("DDT non trovato.", "danger")
            return redirect(url_for("sd.billing"))
        if d.is_billed:
            flash(f"Il DDT {d.doc_number} è già stato fatturato.", "warning")
            return redirect(url_for("sd.billing"))

        cost_center_id_default = request.form.get("cost_center_id", type=int)  # riserva, se una riga non ha il suo

        try:
            # Se esisteva già un rateo di competenza (Fatture da Emettere)
            # per questo DDT, va stornato PRIMA della fattura vera — altrimenti
            # il ricavo verrebbe contato due volte (una col rateo, una con
            # la fattura reale).
            if d.accrual_entry_id is not None:
                rateo_entry = JournalEntry.query.get(d.accrual_entry_id)
                if rateo_entry and not rateo_entry.is_reversed:
                    _reverse_gl_only(rateo_entry, created_by_id=current_user.id)
                d.accrual_entry_id = None

            ar_acc = AccountMapping.get_or_error("crediti_clienti")
            vat_acc = AccountMapping.get_or_error("iva_debito")
            # Conto ricavi scelto in base al canale del cliente: 'subappalto'
            # (lavori per conto di un appaltatore principale) → 4000,
            # 'affidamento_diretto' (grande committente senza intermediari)
            # → 4001. Se il cliente non è ancora qualificato, si ricade sul
            # conto storico 4000 e lo si segnala all'utente, così se ne accorge
            # e può classificare l'anagrafica (Soggetti Economici).
            channel = d.party.revenue_channel if d.party else None
            if channel == "affidamento_diretto":
                rev_acc = _acc("4001")
            else:
                rev_acc = _acc("4000")
                if channel is None:
                    flash(f"Cliente {d.party.name if d.party else ''} senza canale ricavo qualificato: "
                          f"fatturato su 4000 (Subappalto) di default. Imposta il canale in Soggetti Economici "
                          f"se si tratta invece di un affidamento diretto.", "warning")

            total_net = Decimal("0")
            total_vat = Decimal("0")
            journal_lines = []
            inv_rows = []
            for l in d.lines:
                net = (Decimal(str(l.qty)) * Decimal(str(l.price))).quantize(Decimal("0.01"))
                vat = (net * Decimal(str(l.material.vat_rate)) / 100).quantize(Decimal("0.01"))
                total_net += net
                total_vat += vat
                riga_cc_id = request.form.get(f"cost_center_id_{l.id}", type=int) or cost_center_id_default
                riga_cc = CostCenter.query.get(riga_cc_id) if riga_cc_id else None
                journal_lines.append({"account_id": rev_acc.id, "dare": 0, "avere": net,
                                      "description": f"{l.material.code} - {l.material.description}",
                                      "cost_center_id": riga_cc.id if riga_cc else None})
                # Caratteri ASCII semplici SOLO qui: questa stringa finisce
                # verbatim nel campo <Descrizione> dell'XML FatturaPA (vedi
                # services/fatturapa.py). Em-dash (—), simbolo moltiplicazione
                # (×) e simbolo euro (€) sono stati respinti da fatturacheck.it
                # come "caratteri non validi" — meglio trattino, "x" e nessun
                # simbolo valuta (l'importo è già in colonna a parte).
                inv_rows.append((f"{l.material.code} - {l.material.description} "
                                 f"({float(l.qty):.0f} {l.material.uom} x {float(l.price):.2f})",
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
                vat_rate=(vat_rates.pop() if len(vat_rates) == 1 else None), commit=False,
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
    return render_template("sd/billing.html", to_bill=to_bill, billed=billed, cost_centers=cost_centers)


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
