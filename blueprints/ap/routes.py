from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from extensions import db
from models import Account, CostCenter, EconomicSubject, JournalEntry
from services.posting import post_journal_entry, UnbalancedEntryError
from services.co import validate_co_assignment, COValidationError

ap_bp = Blueprint("ap", __name__, template_folder="../../templates/ap")


def _get_account_by_code(code):
    acc = Account.query.filter_by(code=code).first()
    if acc is None:
        raise ValueError(f"Conto {code} non trovato nel Piano dei Conti. Esegui 'flask seed' prima di continuare.")
    return acc


@ap_bp.route("/supplier_invoice", methods=["GET", "POST"])
@login_required
def supplier_invoice():
    """Fattura fornitore — Registrazione Fattura Fornitore."""
    vendors = EconomicSubject.query.filter_by(active=True, is_supplier=True).order_by(EconomicSubject.name).all()
    expense_accounts = Account.query.filter_by(account_type="costo", active=True).order_by(Account.code).all()
    cost_centers = CostCenter.query.filter_by(active=True).order_by(CostCenter.code).all()

    if request.method == "POST":
        vendor_id = request.form.get("vendor_id", type=int)
        invoice_number = request.form.get("invoice_number", "").strip()
        invoice_date_str = request.form.get("invoice_date")
        net = request.form.get("net", type=float) or 0
        vat_rate = request.form.get("vat_rate", type=float) or 0
        expense_account_id = request.form.get("expense_account_id", type=int)
        description = request.form.get("description", "").strip()
        cost_center_id = request.form.get("cost_center_id", type=int)

        vat = round(net * vat_rate / 100, 2)
        gross = net + vat

        if not vendor_id or not net or not expense_account_id:
            flash("Fornitore, imponibile e conto di costo sono obbligatori.", "danger")
            return render_template("ap/supplier_invoice.html", vendors=vendors, expense_accounts=expense_accounts, cost_centers=cost_centers)

        try:
            ap_account = _get_account_by_code("210000")   # Debiti v/Fornitori
            vat_account = _get_account_by_code("154000")  # IVA a Credito
            expense_account, cost_center = validate_co_assignment(expense_account_id, cost_center_id)

            lines = [
                {"account_id": expense_account.id, "dare": net, "avere": 0, "cost_center_id": cost_center.id if cost_center else None},
                {"account_id": vat_account.id, "dare": vat, "avere": 0},
                {"account_id": ap_account.id, "dare": 0, "avere": gross},
            ]

            invoice_date = datetime.strptime(invoice_date_str, "%Y-%m-%d").date() if invoice_date_str else None
            entry = post_journal_entry(
                doc_type="KR", prefix="19",
                doc_date=invoice_date, description=description or f"Fattura Fornitore {invoice_number}",
                lines=lines, source_module="LEDGER", reference=invoice_number,
                created_by_id=current_user.id, economic_subject_id=vendor_id, gross_amount=gross,
            )
            flash(f"Fattura fornitore registrata: Doc. {entry.doc_number} — Totale {gross:.2f} €.", "success")
            return redirect(url_for("gl.entry_detail", entry_id=entry.id))
        except (UnbalancedEntryError, ValueError, COValidationError) as e:
            flash(str(e), "danger")

    return render_template("ap/supplier_invoice.html", vendors=vendors, expense_accounts=expense_accounts, cost_centers=cost_centers)


@ap_bp.route("/supplier_invoice/import", methods=["GET", "POST"])
@login_required
def supplier_invoice_import():
    """
    Import XML fattura fornitore: carichi il file .xml (o .xml.p7m)
    scaricato dal pannello dell'intermediario, l'app lo legge e
    pre-compila la registrazione Fattura fornitore. Tu scegli solo il conto di costo
    e confermi — numero, data, fornitore, imponibile e IVA arrivano
    direttamente dal file, senza ricopiatura manuale (e senza errori di
    battitura).

    Gestisce TD04 (nota di credito fornitore): la scrittura viene
    registrata a segni invertiti (Dare Debiti — Avere Costo + IVA).
    """
    from decimal import Decimal
    from services.fatturapa_import import parse_fatturapa, FatturaImportError

    expense_accounts = Account.query.filter_by(account_type="costo", active=True).order_by(Account.code).all()
    cost_centers = CostCenter.query.filter_by(active=True).order_by(CostCenter.code).all()

    # ── FASE 2: conferma e registrazione (dati già estratti, in hidden) ──
    if request.method == "POST" and request.form.get("phase") == "confirm":
        expense_account_id = request.form.get("expense_account_id", type=int)
        cost_center_id = request.form.get("cost_center_id", type=int)
        if not expense_account_id:
            flash("Seleziona il conto di costo.", "danger")
            return redirect(url_for("ap.supplier_invoice_import"))
        try:
            ap_account = _get_account_by_code("210000")   # Debiti v/Fornitori
            vat_account = _get_account_by_code("154000")  # IVA a Credito
            expense_account, cost_center = validate_co_assignment(expense_account_id, cost_center_id)

            piva = request.form.get("cedente_piva", "").strip()
            denominazione = request.form.get("cedente_denominazione", "").strip()
            numero = request.form.get("numero", "").strip()
            data_str = request.form.get("data", "").strip()
            tipo_doc = request.form.get("tipo_documento", "TD01").strip()
            net = Decimal(request.form.get("totale_imponibile", "0"))
            vat = Decimal(request.form.get("totale_imposta", "0"))
            gross = net + vat
            descr = request.form.get("descrizione", "").strip()

            # Fornitore: match per P.IVA, altrimenti creato al volo
            vendor = EconomicSubject.query.filter_by(piva=piva).first() if piva else None
            if vendor is None:
                next_code = f"F{EconomicSubject.query.count() + 1:04d}"
                vendor = EconomicSubject(code=next_code, name=denominazione or f"Fornitore {piva}",
                                piva=piva or None, is_supplier=True)
                db.session.add(vendor)
                db.session.flush()

            vendor.is_supplier = True

            if tipo_doc == "TD04":
                # Nota di credito fornitore: segni invertiti
                lines = [
                    {"account_id": ap_account.id, "dare": gross, "avere": 0},
                    {"account_id": expense_account.id, "dare": 0, "avere": net, "cost_center_id": cost_center.id if cost_center else None},
                ]
                if vat:
                    lines.append({"account_id": vat_account.id, "dare": 0, "avere": vat})
                label = "Nota Credito Fornitore"
            else:
                lines = [
                    {"account_id": expense_account.id, "dare": net, "avere": 0, "cost_center_id": cost_center.id if cost_center else None},
                ]
                if vat:
                    lines.append({"account_id": vat_account.id, "dare": vat, "avere": 0})
                lines.append({"account_id": ap_account.id, "dare": 0, "avere": gross})
                label = "Fattura Fornitore"

            invoice_date = datetime.strptime(data_str, "%Y-%m-%d").date() if data_str else None
            entry = post_journal_entry(
                doc_type="KR", prefix="19",
                doc_date=invoice_date,
                description=descr or f"{label} {numero} — {denominazione}",
                lines=lines, source_module="LEDGER", reference=numero,
                created_by_id=current_user.id, economic_subject_id=vendor.id, gross_amount=gross,
            )
            flash(f"{label} importata da XML: Doc. {entry.doc_number} — "
                  f"{denominazione}, n. {numero}, totale {gross:.2f} €.", "success")
            return redirect(url_for("gl.entry_detail", entry_id=entry.id))
        except (UnbalancedEntryError, ValueError, COValidationError) as e:
            db.session.rollback()
            flash(str(e), "danger")
            return redirect(url_for("ap.supplier_invoice_import"))

    # ── FASE 1: upload e lettura del file ──
    if request.method == "POST":
        file = request.files.get("xml_file")
        if not file or not file.filename:
            flash("Seleziona un file .xml o .xml.p7m.", "warning")
            return render_template("ap/supplier_invoice_import.html", parsed=None,
                                   expense_accounts=expense_accounts, cost_centers=cost_centers)
        try:
            parsed = parse_fatturapa(file.read(), filename=file.filename)
        except FatturaImportError as e:
            flash(str(e), "danger")
            return render_template("ap/supplier_invoice_import.html", parsed=None,
                                   expense_accounts=expense_accounts, cost_centers=cost_centers)

        if parsed["multi_body"]:
            flash("Attenzione: il file contiene un LOTTO di più fatture. "
                  "Verrà importata solo la prima — per le altre serve un import separato.", "warning")
        if parsed["tipo_documento"] not in ("TD01", "TD02", "TD03", "TD04", "TD06", "TD24", "TD25"):
            flash(f"Tipo documento {parsed['tipo_documento']} non gestito da questo import "
                  "(documenti di integrazione/autofattura TD16-TD29 richiedono una "
                  "registrazione manuale con reverse charge).", "danger")
            return render_template("ap/supplier_invoice_import.html", parsed=None,
                                   expense_accounts=expense_accounts, cost_centers=cost_centers)

        vendor_match = EconomicSubject.query.filter_by(piva=parsed["cedente_piva"]).first() if parsed["cedente_piva"] else None
        return render_template("ap/supplier_invoice_import.html", parsed=parsed,
                               vendor_match=vendor_match,
                               expense_accounts=expense_accounts, cost_centers=cost_centers)

    return render_template("ap/supplier_invoice_import.html", parsed=None,
                           expense_accounts=expense_accounts, cost_centers=cost_centers)


@ap_bp.route("/supplier_payment", methods=["GET", "POST"])
@login_required
def supplier_payment():
    """Pagamento fornitore — Pagamento Fornitore su fatture aperte (compensazione semplificata)."""
    open_invoices = (JournalEntry.query
                     .filter_by(doc_type="KR", is_paid=False, is_reversed=False)
                     .order_by(JournalEntry.doc_date)
                     .all())

    if request.method == "POST":
        selected_ids = request.form.getlist("invoice_ids[]")
        if not selected_ids:
            flash("Seleziona almeno una fattura da pagare.", "warning")
            return redirect(url_for("ap.supplier_payment"))

        try:
            ap_account = _get_account_by_code("210000")
            bank_account = _get_account_by_code("180000")  # Banca c/c

            total = 0
            refs = []
            for eid in selected_ids:
                inv = JournalEntry.query.get(int(eid))
                if inv and not inv.is_paid:
                    total += float(inv.gross_amount or 0)
                    refs.append(inv.doc_number)

            lines = [
                {"account_id": ap_account.id, "dare": total, "avere": 0},
                {"account_id": bank_account.id, "dare": 0, "avere": total},
            ]
            payment_entry = post_journal_entry(
                doc_type="KZ", prefix="15",
                doc_date=None, description=f"Pagamento fornitore — {', '.join(refs)}",
                lines=lines, source_module="LEDGER", reference=", ".join(refs),
                created_by_id=current_user.id,
            )

            for eid in selected_ids:
                inv = JournalEntry.query.get(int(eid))
                if inv:
                    inv.is_paid = True
                    inv.paid_by_entry_id = payment_entry.id
            db.session.commit()

            flash(f"Pagamento registrato: Doc. {payment_entry.doc_number} — Totale {total:.2f} €. {len(refs)} fatture compensate.", "success")
            return redirect(url_for("gl.entry_detail", entry_id=payment_entry.id))
        except (UnbalancedEntryError, ValueError) as e:
            flash(str(e), "danger")

    return render_template("ap/supplier_payment.html", open_invoices=open_invoices)
