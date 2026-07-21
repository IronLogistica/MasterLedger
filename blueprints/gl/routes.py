from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user

from extensions import db
from models import Account, CostCenter, JournalEntry
from services.posting import post_journal_entry, reverse_journal_entry, UnbalancedEntryError
from services.co import validate_co_assignment, COValidationError
from services.ai_posting import suggerisci_scrittura, estrai_testo_pdf, AISuggestionError

gl_bp = Blueprint("gl", __name__, template_folder="../../templates/gl")


@gl_bp.route("/")
@login_required
def journal_list():
    """Il 'Giornale' — lista cronologica di TUTTI i documenti (equivalente del
    vecchio 'Giornale Integrato' del simulatore, qui però è il vero libro
    giornale con numerazione progressiva reale)."""
    page = request.args.get("page", 1, type=int)
    entries = (JournalEntry.query
               .order_by(JournalEntry.created_at.desc())
               .paginate(page=page, per_page=25, error_out=False))
    return render_template("gl/journal_list.html", entries=entries)


@gl_bp.route("/entry/<int:entry_id>")
@login_required
def entry_detail(entry_id):
    entry = JournalEntry.query.get_or_404(entry_id)
    return render_template("gl/entry_detail.html", entry=entry)


@gl_bp.route("/entry/<int:entry_id>/reverse", methods=["POST"])
@login_required
def entry_reverse(entry_id):
    try:
        new_entry = reverse_journal_entry(entry_id, created_by_id=current_user.id)
        flash(f"Documento stornato correttamente. Nuovo documento: {new_entry.doc_number}.", "success")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(url_for("gl.entry_detail", entry_id=entry_id))


@gl_bp.route("/journal_entry", methods=["GET", "POST"])
@login_required
def journal_entry():
    """Prima nota — Registrazione manuale in Prima Nota (General Journal Entry)."""
    accounts = Account.query.filter_by(active=True).order_by(Account.code).all()
    cost_centers = CostCenter.query.filter_by(active=True).order_by(CostCenter.code).all()

    if request.method == "POST":
        doc_date_str = request.form.get("doc_date")
        description = request.form.get("description", "").strip()
        account_ids = request.form.getlist("account_id[]")
        pks = request.form.getlist("pk[]")           # '40' = Dare, '50' = Avere
        amounts = request.form.getlist("amount[]")
        cost_centers_sel = request.form.getlist("cost_center_id[]")

        lines = []
        for acc_id, pk, amt, cc in zip(account_ids, pks, amounts, cost_centers_sel):
            if not acc_id or not amt:
                continue
            amount = float(amt.replace(",", "."))
            account, center = validate_co_assignment(int(acc_id), int(cc) if cc else None)
            lines.append({
                "account_id": account.id,
                "dare": amount if pk == "40" else 0,
                "avere": amount if pk == "50" else 0,
                "cost_center_id": center.id if center else None,
            })

        if len(lines) < 2:
            flash("Servono almeno due righe (una in Dare e una in Avere).", "danger")
            return render_template("gl/journal_entry.html", accounts=accounts, cost_centers=cost_centers)

        try:
            doc_date = datetime.strptime(doc_date_str, "%Y-%m-%d").date() if doc_date_str else None
            entry = post_journal_entry(
                doc_type="SA", prefix="10",
                doc_date=doc_date, description=description or "Prima Nota Manuale",
                lines=lines, source_module="LEDGER", created_by_id=current_user.id,
            )
            flash(f"Documento {entry.doc_number} registrato correttamente in Prima Nota.", "success")
            return redirect(url_for("gl.entry_detail", entry_id=entry.id))
        except (UnbalancedEntryError, COValidationError, ValueError) as e:
            flash(str(e), "danger")

    return render_template("gl/journal_entry.html", accounts=accounts, cost_centers=cost_centers)


@gl_bp.route("/ai/suggerisci", methods=["POST"])
@login_required
def ai_suggerisci():
    """
    Suggerimento AI per la Prima Nota: prende una descrizione in linguaggio
    naturale e/o un documento PDF caricato (es. una fattura) e propone le
    righe (conto, Dare/Avere, importo) da mostrare PRE-COMPILATE nel form —
    l'utente le controlla e conferma lui stesso con "Registra Documento".
    Questa rotta non scrive MAI su JournalEntry: non passa da
    post_journal_entry, si limita a restituire un suggerimento.

    Accetta sia JSON semplice ({"descrizione": "..."}) sia multipart/form-data
    (campo "descrizione" opzionale + campo file "documento" opzionale).
    """
    file_pdf = request.files.get("documento")
    if file_pdf is not None and file_pdf.filename:
        descrizione = (request.form.get("descrizione") or "").strip()
        tipo_documento = (request.form.get("tipo_documento") or "").strip() or None
    else:
        payload = request.get_json(silent=True) or {}
        descrizione = (payload.get("descrizione") or "").strip()
        tipo_documento = (payload.get("tipo_documento") or "").strip() or None
        file_pdf = None

    testo_documento = None
    if file_pdf is not None:
        if not file_pdf.filename.lower().endswith(".pdf"):
            return jsonify({"error": "Per ora accetto solo file PDF."}), 400
        try:
            testo_documento, pagine_lette = estrai_testo_pdf(file_pdf.stream)
        except AISuggestionError as e:
            return jsonify({"error": str(e)}), 400
        if not testo_documento:
            return jsonify({"error": "Non sono riuscito a leggere testo da questo PDF — probabilmente è "
                                      "una scansione/immagine senza testo selezionabile (serve OCR, non ancora "
                                      "disponibile). Prova a descrivere l'operazione a mano qui sopra."}), 400

    if not descrizione and not testo_documento:
        return jsonify({"error": "Descrivi l'operazione oppure carica un documento PDF."}), 400

    accounts = Account.query.filter_by(active=True).order_by(Account.code).all()

    try:
        suggerimento = suggerisci_scrittura(descrizione, accounts, testo_documento=testo_documento,
                                             tipo_documento=tipo_documento)
    except AISuggestionError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Errore imprevisto: {e}"}), 500

    # L'AI conosce solo i CODICI conto (non gli id del database) — li risolviamo qui.
    code_to_id = {a.code: a.id for a in accounts}
    righe_risolte = []
    avvisi = []
    for line in suggerimento.get("lines", []):
        code = str(line.get("account_code", "")).strip()
        acc_id = code_to_id.get(code)
        if not acc_id:
            avvisi.append(f'Conto "{code}" proposto dall\'AI non esiste nel piano dei conti: riga saltata.')
            continue
        try:
            amount = float(line.get("amount") or 0)
        except (TypeError, ValueError):
            avvisi.append(f'Importo non valido per il conto "{code}": riga saltata.')
            continue
        righe_risolte.append({
            "account_id": acc_id,
            "pk": "40" if str(line.get("pk")) == "40" else "50",
            "amount": amount,
        })

    if len(righe_risolte) < 2:
        return jsonify({"error": "Dopo aver verificato i conti proposti, non restano abbastanza righe valide. "
                                  "Prova a riformulare la richiesta.", "avvisi": avvisi}), 400

    return jsonify({
        "description": suggerimento.get("description") or descrizione,
        "lines": righe_risolte,
        "note": suggerimento.get("note"),
        "avvisi": avvisi,
    })
