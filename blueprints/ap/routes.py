from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from extensions import db
from models import Account, AccountMapping, CostCenter, EconomicSubject, JournalEntry
from services.posting import post_journal_entry, UnbalancedEntryError
from services.co import validate_co_assignment, COValidationError
from services.payments import create_installments_for_invoice, allocate_payment, PaymentAllocationError
from models import InvoiceInstallment

ap_bp = Blueprint("ap", __name__, template_folder="../../templates/ap")


def _get_account_by_code(code):
    acc = Account.query.filter_by(code=code).first()
    if acc is None:
        raise ValueError(f"Conto {code} non trovato nel Piano dei Conti. Esegui 'flask seed' prima di continuare.")
    return acc


def _residuo_fattura(inv):
    """Residuo REALE da pagare per questo documento — MAI il lordo originale
    a occhi chiusi: se la fattura è già stata parzialmente saldata dallo
    Scadenzario granulare (/gl/scadenzario/paga), le rate hanno un residuo
    inferiore al lordo. Usare gross_amount qui pagherebbe/incasserebbe di
    nuovo la parte già chiusa. Nessuna rata trovata (dato legacy, mai
    passato da create_installments_for_invoice) → il lordo resta corretto,
    perché equivale a "mai stato toccato".
    """
    installments = InvoiceInstallment.query.filter_by(entry_id=inv.id).all()
    if not installments:
        return Decimal(str(inv.gross_amount or 0))
    return sum((Decimal(str(i.residual_amount or 0)) for i in installments), Decimal("0"))


@ap_bp.route("/supplier_invoice", methods=["GET", "POST"])
@login_required
def supplier_invoice():
    """Fattura fornitore — Registrazione Fattura Fornitore, multi-riga:
    ogni riga ha il proprio conto di costo E il proprio centro di
    costo/ricavo — una fattura con 6 prodotti per 6 centri diversi si
    frazionano in 6 righe, ognuna spesata sul centro giusto. I costi
    vanno SEMPRE spesati subito, riga per riga — non è un'eccezione
    legata a una variazione di prezzo (quella è un concetto diverso,
    del three-way match MM)."""
    vendors = EconomicSubject.query.filter_by(active=True, is_supplier=True).order_by(EconomicSubject.name).all()
    expense_accounts = Account.query.filter_by(account_type="costo", active=True).order_by(Account.code).all()
    cost_centers = CostCenter.query.filter_by(active=True).order_by(CostCenter.code).all()

    if request.method == "POST":
        vendor_id = request.form.get("vendor_id", type=int)
        invoice_number = request.form.get("invoice_number", "").strip()
        invoice_date_str = request.form.get("invoice_date")
        description = request.form.get("description", "").strip()

        descs = request.form.getlist("line_description[]")
        nets = request.form.getlist("line_net[]")
        rates = request.form.getlist("line_vat_rate[]")
        accounts_ids = request.form.getlist("line_expense_account_id[]")
        centers_ids = request.form.getlist("line_cost_center_id[]")

        if not vendor_id:
            flash("Il fornitore è obbligatorio.", "danger")
            return render_template("ap/supplier_invoice.html", vendors=vendors, expense_accounts=expense_accounts, cost_centers=cost_centers)

        try:
            ap_account = AccountMapping.get_or_error("debiti_fornitori")
            vat_account = AccountMapping.get_or_error("iva_credito")

            rows = []
            for i in range(len(descs)):
                net_str = (nets[i] if i < len(nets) else "").strip()
                if not net_str:
                    continue  # riga vuota nel form, si ignora
                net = Decimal(net_str.replace(",", ".")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if net <= 0:
                    raise ValueError(f"Riga {i+1}: l'imponibile deve essere positivo.")
                rate = Decimal(rates[i].replace(",", ".")) if i < len(rates) and rates[i] else Decimal("22.0")
                account_id = int(accounts_ids[i]) if i < len(accounts_ids) and accounts_ids[i] else None
                center_id = int(centers_ids[i]) if i < len(centers_ids) and centers_ids[i] else None
                if not account_id:
                    raise ValueError(f"Riga {i+1}: seleziona un conto di costo.")
                expense_account, cost_center = validate_co_assignment(account_id, center_id)
                vat = (net * rate / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                rows.append({
                    "description": (descs[i] if i < len(descs) else "").strip(),
                    "net": net, "vat": vat,
                    "account_id": expense_account.id,
                    "cost_center_id": cost_center.id if cost_center else None,
                })

            if not rows:
                raise ValueError("Inserisci almeno una riga con conto di costo e importo.")

            total_net = sum((r["net"] for r in rows), Decimal("0"))
            total_vat = sum((r["vat"] for r in rows), Decimal("0"))
            gross = total_net + total_vat

            lines = [{"account_id": r["account_id"], "dare": r["net"], "avere": 0,
                     "description": r["description"], "cost_center_id": r["cost_center_id"]} for r in rows]
            if total_vat:
                lines.append({"account_id": vat_account.id, "dare": round(total_vat, 2), "avere": 0})
            lines.append({"account_id": ap_account.id, "dare": 0, "avere": round(gross, 2)})

            invoice_date = datetime.strptime(invoice_date_str, "%Y-%m-%d").date() if invoice_date_str else None
            entry = post_journal_entry(
                doc_type="KR", prefix="19",
                doc_date=invoice_date, description=description or f"Fattura Fornitore {invoice_number}",
                lines=lines, source_module="LEDGER", reference=invoice_number,
                created_by_id=current_user.id, economic_subject_id=vendor_id, gross_amount=round(gross, 2),
                commit=False,
            )
            create_installments_for_invoice(entry)
            db.session.commit()
            flash(f"Fattura fornitore registrata: Doc. {entry.doc_number} — Totale {gross:.2f} € "
                  f"({len(rows)} righe di costo).", "success")
            return redirect(url_for("gl.entry_detail", entry_id=entry.id))
        except (UnbalancedEntryError, ValueError, COValidationError) as e:
            db.session.rollback()
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
            ap_account = AccountMapping.get_or_error("debiti_fornitori")
            vat_account = AccountMapping.get_or_error("iva_credito")
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
                created_by_id=current_user.id, economic_subject_id=vendor.id, gross_amount=(-gross if tipo_doc == "TD04" else gross),
                commit=False,
            )
            if tipo_doc != "TD04":
                # Le note di credito fornitore (TD04, importo negativo) si compensano
                # direttamente in blocco — niente rate: una rata a importo negativo
                # non avrebbe senso nel modello di scadenzario.
                create_installments_for_invoice(entry)
            db.session.commit()
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
        raw_ids = request.form.getlist("invoice_ids[]")
        try:
            selected_ids = {int(value) for value in raw_ids}
        except (TypeError, ValueError):
            selected_ids = set()
        if not selected_ids:
            flash("Seleziona almeno una fattura da pagare.", "warning")
            return redirect(url_for("ap.supplier_payment"))

        try:
            invoices = (JournalEntry.query
                        .filter(JournalEntry.id.in_(selected_ids), JournalEntry.doc_type == "KR",
                                JournalEntry.is_paid.is_(False), JournalEntry.is_reversed.is_(False))
                        .all())
            if len(invoices) != len(selected_ids):
                raise ValueError("La selezione contiene documenti non validi, già chiusi o stornati.")
            subject_ids = {inv.economic_subject_id for inv in invoices}
            if None in subject_ids or len(subject_ids) != 1:
                raise ValueError("Compensare in un unico pagamento solo documenti dello stesso fornitore.")

            ap_account = AccountMapping.get_or_error("debiti_fornitori")
            bank_account = AccountMapping.get_or_error("banca_principale")
            total = sum((_residuo_fattura(inv) for inv in invoices), Decimal("0"))
            if total <= 0:
                raise ValueError("Il netto da pagare, dopo le note di credito, deve essere positivo.")
            refs = [inv.doc_number for inv in invoices]
            economic_subject_id = next(iter(subject_ids))
            payment_entry = post_journal_entry(
                doc_type="KZ", prefix="15", doc_date=None,
                description=f"Pagamento fornitore — {', '.join(refs)}",
                lines=[
                    {"account_id": ap_account.id, "dare": total, "avere": 0},
                    {"account_id": bank_account.id, "dare": 0, "avere": total},
                ],
                source_module="LEDGER", reference=", ".join(refs),
                created_by_id=current_user.id, economic_subject_id=economic_subject_id,
                gross_amount=total, commit=False,
            )
            for inv in invoices:
                inv.is_paid = True
                inv.paid_by_entry_id = payment_entry.id
                # Sincronizza le rate (Fase 3): un pagamento a saldo pieno da
                # questa vista chiude anche lo Scadenzario, non solo il flag.
                for inst in InvoiceInstallment.query.filter_by(entry_id=inv.id).all():
                    inst.residual_amount = 0
            db.session.commit()
            flash(f"Pagamento registrato: Doc. {payment_entry.doc_number} — Totale {total:.2f} €. "
                  f"{len(invoices)} documenti compensati.", "success")
            return redirect(url_for("gl.entry_detail", entry_id=payment_entry.id))
        except (UnbalancedEntryError, ValueError) as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("ap/supplier_payment.html", open_invoices=open_invoices)
