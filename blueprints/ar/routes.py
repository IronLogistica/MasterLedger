import io
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required, current_user

from extensions import db
from models import Account, EconomicSubject, JournalEntry, InvoiceLine
from services.posting import post_journal_entry, UnbalancedEntryError
from services.fatturapa import build_fatturapa_xml, FatturaPAConfigError

ar_bp = Blueprint("ar", __name__, template_folder="../../templates/ar")

# Codici Natura ammessi dal tracciato FatturaPA (tipo NaturaType dell'XSD
# ufficiale, Allegato A vers. 1.9). I codici generici N2, N3 e N6 non sono
# più validi dal 2021: vanno usate le sotto-codifiche.
# Le specifiche SdI impongono la Natura quando AliquotaIVA = 0 (controlli
# 00400/00429) e la VIETANO quando AliquotaIVA > 0 (controllo 00430).
NATURA_CODES = {
    "N1":   "N1 — Escluse ex art. 15",
    "N2.1": "N2.1 — Non soggette (artt. 7–7-septies)",
    "N2.2": "N2.2 — Non soggette (altri casi)",
    "N3.1": "N3.1 — Non imponibili: esportazioni",
    "N3.2": "N3.2 — Non imponibili: cessioni intracomunitarie",
    "N3.3": "N3.3 — Non imponibili: cessioni verso San Marino",
    "N3.4": "N3.4 — Non imponibili: operazioni assimilate",
    "N3.5": "N3.5 — Non imponibili: a seguito di dichiarazioni d'intento",
    "N3.6": "N3.6 — Non imponibili: altre operazioni",
    "N4":   "N4 — Esenti (art. 10)",
    "N5":   "N5 — Regime del margine / IVA non esposta",
    "N6.1": "N6.1 — Inversione contabile: rottami",
    "N6.2": "N6.2 — Inversione contabile: oro e argento",
    "N6.3": "N6.3 — Inversione contabile: subappalto edile",
    "N6.4": "N6.4 — Inversione contabile: cessione fabbricati",
    "N6.5": "N6.5 — Inversione contabile: telefoni cellulari",
    "N6.6": "N6.6 — Inversione contabile: prodotti elettronici",
    "N6.7": "N6.7 — Inversione contabile: comparto edile e settori connessi",
    "N6.8": "N6.8 — Inversione contabile: settore energetico",
    "N6.9": "N6.9 — Inversione contabile: altri casi",
    "N7":   "N7 — IVA assolta in altro stato UE",
}


def _round_half_up_2(value):
    """
    Arrotondamento alla seconda cifra decimale come richiesto dalle
    specifiche SdI (controllo 00421): "per difetto se la terza cifra
    decimale è inferiore a 5, per eccesso se uguale o superiore a 5".
    Il round() nativo di Python usa il banker's rounding (half-even) su
    float — NON è conforme: round(2.675, 2) = 2.67 invece di 2.68.
    """
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _get_account_by_code(code):
    acc = Account.query.filter_by(code=code).first()
    if acc is None:
        raise ValueError(f"Conto {code} non trovato nel Piano dei Conti. Esegui 'flask seed' prima di continuare.")
    return acc


def _parse_invoice_lines(form):
    """
    Legge le righe fattura dal form (array paralleli line_*[]) e le valida
    secondo il tracciato FatturaPA. Ritorna (righe, errori) dove ogni riga è
    un dict {description, amount(Decimal), vat_rate(Decimal), natura, account_id}.
    """
    descs = form.getlist("line_description[]")
    nets = form.getlist("line_net[]")
    rates = form.getlist("line_vat_rate[]")
    naturas = form.getlist("line_natura[]")
    accounts = form.getlist("line_account_id[]")

    rows, errors = [], []
    for i in range(len(descs)):
        desc = (descs[i] or "").strip()
        net_raw = (nets[i] or "").strip() if i < len(nets) else ""
        if not desc and not net_raw:
            continue  # riga completamente vuota: la ignoriamo
        try:
            amount = _round_half_up_2(Decimal(net_raw.replace(",", ".")))
        except Exception:
            errors.append(f"Riga {i+1}: imponibile '{net_raw}' non valido.")
            continue
        if amount <= 0:
            errors.append(f"Riga {i+1}: l'imponibile deve essere maggiore di zero.")
            continue
        if not desc:
            errors.append(f"Riga {i+1}: la descrizione è obbligatoria (elemento <Descrizione> del tracciato).")
            continue
        try:
            rate = Decimal((rates[i] or "0").strip())
        except Exception:
            errors.append(f"Riga {i+1}: aliquota non valida.")
            continue
        natura = (naturas[i] or "").strip() if i < len(naturas) else ""
        natura = natura or None
        # Natura obbligatoria con aliquota 0 (00400/00429), vietata altrimenti (00430)
        if rate == 0:
            if not natura or natura not in NATURA_CODES:
                errors.append(f"Riga {i+1}: con aliquota 0% serve un codice Natura valido "
                              "(es. N4) — scarto SdI 00400/00429.")
                continue
        else:
            natura = None
        try:
            account_id = int(accounts[i])
        except Exception:
            errors.append(f"Riga {i+1}: seleziona il conto di ricavo.")
            continue
        rows.append({"description": desc, "amount": amount, "vat_rate": rate,
                     "natura": natura, "account_id": account_id})

    if not rows and not errors:
        errors.append("Inserisci almeno una riga fattura.")
    return rows, errors


def _vat_summary(rows):
    """
    Raggruppa le righe per coppia (aliquota, natura) e calcola l'imposta
    come da specifiche SdI (controlli 00421/00422): per ogni gruppo,
    ImponibileImporto = somma dei PrezzoTotale, Imposta = arrotondamento
    HALF_UP di (aliquota * imponibile / 100). L'IVA si calcola sul TOTALE
    del gruppo, non riga per riga — è la regola del tracciato.
    Ritorna lista di dict {vat_rate, natura, imponibile, imposta}.
    """
    groups = {}
    for r in rows:
        key = (r["vat_rate"], r["natura"])
        groups.setdefault(key, Decimal("0"))
        groups[key] += r["amount"]
    summary = []
    for (rate, natura), imponibile in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        imposta = _round_half_up_2(imponibile * rate / Decimal("100"))
        summary.append({"vat_rate": rate, "natura": natura,
                        "imponibile": imponibile, "imposta": imposta})
    return summary


def _register_ar_document(doc_type, prefix, form, customers, template, extra_ctx=None):
    """
    Logica condivisa Fattura cliente (fattura, DR/TD01) e Nota di credito cliente (nota di credito, DG/TD04).
    Per la NOTA DI CREDITO la scrittura contabile è speculare alla fattura:
        Dare  Ricavi (rettifica) + IVA a Debito (storno)
        Avere Crediti v/Clienti
    Gli importi nell'XML restano POSITIVI: è il TipoDocumento TD04 a
    qualificare il segno del documento, come da prassi del tracciato.
    """
    revenue_accounts = Account.query.filter_by(account_type="ricavo", active=True).order_by(Account.code).all()
    ctx = {"customers": customers, "revenue_accounts": revenue_accounts,
           "natura_codes": NATURA_CODES}
    if extra_ctx:
        ctx.update(extra_ctx)

    customer_id = form.get("customer_id", type=int)
    invoice_number = form.get("invoice_number", "").strip()
    invoice_date_str = form.get("invoice_date")
    description = form.get("description", "").strip()
    linked_invoice_id = form.get("linked_invoice_id", type=int)  # solo Nota di credito cliente

    if not customer_id:
        flash("Seleziona il cliente.", "danger")
        return render_template(template, **ctx)

    rows, errors = _parse_invoice_lines(form)
    if errors:
        for e in errors:
            flash(e, "danger")
        return render_template(template, **ctx)

    summary = _vat_summary(rows)
    total_net = sum(g["imponibile"] for g in summary)
    total_vat = sum(g["imposta"] for g in summary)
    gross = total_net + total_vat

    try:
        ar_account = _get_account_by_code("140000")   # Crediti v/Clienti
        vat_account = _get_account_by_code("170000")  # IVA a Debito

        if doc_type == "DR":
            # Fattura: Dare Crediti — Avere Ricavi (per riga) + IVA
            journal_lines = [{"account_id": ar_account.id, "dare": gross, "avere": 0}]
            for r in rows:
                journal_lines.append({"account_id": r["account_id"], "dare": 0,
                                      "avere": r["amount"], "description": r["description"]})
            if total_vat:
                journal_lines.append({"account_id": vat_account.id, "dare": 0, "avere": total_vat})
            doc_label = "Fattura Cliente"
        else:
            # Nota di credito: Dare Ricavi (per riga) + IVA — Avere Crediti
            journal_lines = []
            for r in rows:
                journal_lines.append({"account_id": r["account_id"], "dare": r["amount"],
                                      "avere": 0, "description": r["description"]})
            if total_vat:
                journal_lines.append({"account_id": vat_account.id, "dare": total_vat, "avere": 0})
            journal_lines.append({"account_id": ar_account.id, "dare": 0, "avere": gross})
            doc_label = "Nota di Credito Cliente"

        invoice_date = datetime.strptime(invoice_date_str, "%Y-%m-%d").date() if invoice_date_str else None
        entry = post_journal_entry(
            doc_type=doc_type, prefix=prefix,
            doc_date=invoice_date, description=description or f"{doc_label} {invoice_number}",
            lines=journal_lines, source_module="LEDGER", reference=invoice_number,
            created_by_id=current_user.id, economic_subject_id=customer_id, gross_amount=gross,
        )

        # Righe commerciali (alimentano DettaglioLinee/DatiRiepilogo dell'XML)
        for n, r in enumerate(rows, start=1):
            db.session.add(InvoiceLine(entry_id=entry.id, line_number=n,
                                       description=r["description"], amount=r["amount"],
                                       vat_rate=r["vat_rate"], natura=r["natura"],
                                       account_id=r["account_id"]))
        if doc_type == "DG" and linked_invoice_id:
            linked = JournalEntry.query.get(linked_invoice_id)
            if linked and linked.doc_type == "DR" and linked.economic_subject_id == customer_id:
                entry.linked_invoice_id = linked.id
        db.session.commit()

        flash(f"{doc_label} registrata: Doc. {entry.doc_number} — Totale {gross:.2f} € "
              f"({len(rows)} righe, {len(summary)} aliquote).", "success")
        return redirect(url_for("gl.entry_detail", entry_id=entry.id))
    except (UnbalancedEntryError, ValueError) as e:
        db.session.rollback()
        flash(str(e), "danger")
    return render_template(template, **ctx)


@ar_bp.route("/customer_invoice", methods=["GET", "POST"])
@login_required
def customer_invoice():
    """Fattura cliente — Registrazione Fattura Cliente (multi-riga, multi-aliquota)."""
    customers = EconomicSubject.query.filter_by(active=True, is_customer=True).order_by(EconomicSubject.name).all()

    if request.method == "POST":
        return _register_ar_document("DR", "14", request.form, customers, "ar/customer_invoice.html")

    revenue_accounts = Account.query.filter_by(account_type="ricavo", active=True).order_by(Account.code).all()
    return render_template("ar/customer_invoice.html", customers=customers,
                           revenue_accounts=revenue_accounts, natura_codes=NATURA_CODES)


@ar_bp.route("/customer_credit_note", methods=["GET", "POST"])
@login_required
def customer_credit_note():
    """
    Nota di credito cliente — Nota di Credito Cliente (TD04), multi-riga. Rettifica una
    fattura già emessa: la scrittura contabile è speculare alla fattura e
    l'XML esce con TipoDocumento TD04 e — se indicata — la fattura
    originale nel blocco <DatiFattureCollegate>.
    """
    customers = EconomicSubject.query.filter_by(active=True, is_customer=True).order_by(EconomicSubject.name).all()
    dr_invoices = (JournalEntry.query
                   .filter_by(doc_type="DR", is_reversed=False)
                   .order_by(JournalEntry.doc_date.desc())
                   .limit(200).all())

    if request.method == "POST":
        return _register_ar_document("DG", "17", request.form, customers, "ar/customer_credit_note.html",
                                     extra_ctx={"dr_invoices": dr_invoices})

    revenue_accounts = Account.query.filter_by(account_type="ricavo", active=True).order_by(Account.code).all()
    return render_template("ar/customer_credit_note.html", customers=customers,
                           revenue_accounts=revenue_accounts, natura_codes=NATURA_CODES,
                           dr_invoices=dr_invoices)


@ar_bp.route("/customer_payment", methods=["GET", "POST"])
@login_required
def customer_payment():
    """Incasso cliente — Incasso Cliente su fatture aperte (compensazione semplificata)."""
    open_invoices = (JournalEntry.query
                     .filter_by(doc_type="DR", is_paid=False, is_reversed=False)
                     .order_by(JournalEntry.doc_date)
                     .all())

    if request.method == "POST":
        selected_ids = request.form.getlist("invoice_ids[]")
        if not selected_ids:
            flash("Seleziona almeno una fattura da incassare.", "warning")
            return redirect(url_for("ar.customer_payment"))

        try:
            ar_account = _get_account_by_code("140000")
            bank_account = _get_account_by_code("180000")

            total = 0
            refs = []
            for eid in selected_ids:
                inv = JournalEntry.query.get(int(eid))
                if inv and not inv.is_paid:
                    total += float(inv.gross_amount or 0)
                    refs.append(inv.doc_number)

            lines = [
                {"account_id": bank_account.id, "dare": total, "avere": 0},
                {"account_id": ar_account.id, "dare": 0, "avere": total},
            ]
            payment_entry = post_journal_entry(
                doc_type="DZ", prefix="14",
                doc_date=None, description=f"Incasso cliente — {', '.join(refs)}",
                lines=lines, source_module="LEDGER", reference=", ".join(refs),
                created_by_id=current_user.id,
            )

            for eid in selected_ids:
                inv = JournalEntry.query.get(int(eid))
                if inv:
                    inv.is_paid = True
                    inv.paid_by_entry_id = payment_entry.id
            db.session.commit()

            flash(f"Incasso registrato: Doc. {payment_entry.doc_number} — Totale {total:.2f} €. {len(refs)} fatture compensate.", "success")
            return redirect(url_for("gl.entry_detail", entry_id=payment_entry.id))
        except (UnbalancedEntryError, ValueError) as e:
            flash(str(e), "danger")

    return render_template("ar/customer_payment.html", open_invoices=open_invoices)


# ══════════════════════════════════════════════════════════════════════
# FATTURAZIONE ELETTRONICA — generazione XML FatturaPA per una fattura Fattura cliente
#
# Il flusso resta: 1) registri la fattura con Fattura cliente (come sempre) — 2) da
# qui scarichi l'XML già pronto — 3) l'unico passo manuale che resta è
# caricarlo sul pannello del tuo intermediario (es. Aruba Fatturazione
# Elettronica, sezione "Carica Fatture") per firma e invio allo SdI.
# Questa app NON firma né trasmette nulla: genera solo il file conforme.
# ══════════════════════════════════════════════════════════════════════
@ar_bp.route("/customer_invoice/<int:entry_id>/xml")
@ar_bp.route("/documento/<int:entry_id>/xml")
@login_required
def invoice_xml(entry_id):
    entry = JournalEntry.query.get_or_404(entry_id)
    try:
        filename, xml_bytes = build_fatturapa_xml(entry)
    except (FatturaPAConfigError, ValueError) as e:
        flash(str(e), "danger")
        return redirect(url_for("gl.entry_detail", entry_id=entry_id))

    return send_file(
        io.BytesIO(xml_bytes),
        mimetype="application/xml",
        as_attachment=True,
        download_name=filename,
    )


@ar_bp.route("/customers/<int:customer_id>/dati-fiscali", methods=["GET", "POST"])
@login_required
def customer_fiscali(customer_id):
    """
    Anagrafica minima necessaria per generare l'XML FatturaPA per questo
    cliente (CessionarioCommittente): P.IVA/CF, indirizzo, e Codice
    Destinatario oppure PEC per il recapito tramite SdI.
    """
    customer = EconomicSubject.query.get_or_404(customer_id)

    if request.method == "POST":
        piva = request.form.get("piva", "").strip() or None
        codice_fiscale = request.form.get("codice_fiscale", "").strip().upper() or None
        indirizzo = request.form.get("indirizzo", "").strip() or None
        cap = request.form.get("cap", "").strip() or None
        comune = request.form.get("comune", "").strip() or None
        provincia = request.form.get("provincia", "").strip().upper() or None
        nazione = request.form.get("nazione", "IT").strip().upper() or "IT"
        codice_dest = request.form.get("codice_destinatario", "").strip().upper() or "0000000"
        pec = request.form.get("pec_destinatario", "").strip() or None

        # ── Validazioni conformi al tracciato FatturaPA (Allegato A v1.9) ──
        errors = []
        # 00417: né IdFiscaleIVA né CodiceFiscale → scarto
        if not piva and not codice_fiscale:
            errors.append("Serve almeno uno tra Partita IVA e Codice Fiscale "
                          "(altrimenti lo SdI scarta con errore 00417).")
        if piva and nazione == "IT" and not re.fullmatch(r"\d{11}", piva):
            errors.append("La Partita IVA italiana deve essere di 11 cifre numeriche.")
        if codice_fiscale and not re.fullmatch(r"[A-Z0-9]{11,16}", codice_fiscale):
            errors.append("Il Codice Fiscale deve avere da 11 a 16 caratteri alfanumerici.")
        # CAPType nell'XSD: esattamente 5 cifre numeriche
        if cap and not re.fullmatch(r"\d{5}", cap):
            errors.append("Il CAP deve essere di 5 cifre numeriche (formato CAPType del tracciato).")
        # CodiceDestinatario: esattamente 7 caratteri alfanumerici per FPR12
        # ("XXXXXXX" è il valore per i soggetti esteri)
        if not re.fullmatch(r"[A-Z0-9]{7}", codice_dest):
            errors.append("Il Codice Destinatario deve essere di esattamente 7 caratteri "
                          "alfanumerici (usa 0000000 se il cliente non lo ha comunicato).")
        if indirizzo and len(indirizzo) > 60:
            errors.append("L'Indirizzo può avere al massimo 60 caratteri (tracciato SdI).")
        if comune and len(comune) > 60:
            errors.append("Il Comune può avere al massimo 60 caratteri (tracciato SdI).")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("ar/customer_fiscali.html", customer=customer)

        customer.piva = piva
        customer.codice_fiscale = codice_fiscale
        customer.indirizzo = indirizzo
        customer.cap = cap
        customer.comune = comune
        customer.provincia = provincia
        customer.nazione = nazione
        customer.codice_destinatario = codice_dest
        customer.pec_destinatario = pec
        db.session.commit()
        flash(f"Dati fiscali di {customer.name} aggiornati.", "success")
        return redirect(url_for("ar.customer_invoice"))

    return render_template("ar/customer_fiscali.html", customer=customer)
