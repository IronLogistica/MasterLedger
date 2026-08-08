from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user

from extensions import db
from models import Account, CostCenter, JournalEntry, EconomicSubject
from services.posting import post_journal_entry, reverse_journal_entry, UnbalancedEntryError
from services.co import validate_co_assignment, COValidationError
from services.ai_posting import suggerisci_scrittura, estrai_testo_pdf, AISuggestionError
from services.classificazione_operazioni import classifica, guida_per_classe, CLASSI_OPERAZIONE
from services.rettifiche_operazioni import classifica_rettifica, guida_per_rettifica, CATALOGO_RETTIFICHE

gl_bp = Blueprint("gl", __name__, template_folder="../../templates/gl")

# Etichette leggibili per i doc_type — usate sia nel filtro del Giornale sia
# nel riepilogo per tipo documento (la "prova" che tutto quello che passa da
# SD/MM/Paghe/Cespiti sia effettivamente arrivato in Prima Nota).
DOC_TYPE_LABELS = {
    "SA": "Prima Nota manuale",
    "KR": "Fattura Fornitore (AP)",
    "DR": "Fattura Cliente (AR)",
    "KZ": "Pagamento",
    "DZ": "Incasso",
    "Cespiti": "Capitalizzazione Cespite",
    "AF": "Ammortamento",
    "QT": "Preventivo",
    "OR": "Ordine Cliente",
    "DL": "DDT / Uscita Merci",
    "OA": "Ordine d'Acquisto",
    "GR": "Entrata Merci",
    "RFQ": "Richiesta d'Offerta",
    "PG": "Paghe (accantonamento/F24/pagamento)",
}


@gl_bp.route("/")
@login_required
def journal_list():
    """Il 'Giornale' — lista cronologica di TUTTI i documenti (equivalente del
    vecchio 'Giornale Integrato' del simulatore, qui però è il vero libro
    giornale con numerazione progressiva reale).

    Filtrabile per tipo documento, modulo di provenienza, controparte e stato
    — è il posto dove verificare che TUTTO quello che esce da SD (Fatturazione
    DDT → doc_type DR), da MM (Verifica Fattura → doc_type KR) o da Paghe
    (doc_type PG) sia effettivamente arrivato in Prima Nota, e cosa resta
    ancora aperto/da pagare.
    """
    page = request.args.get("page", 1, type=int)
    doc_type = request.args.get("doc_type") or None
    source_module = request.args.get("source_module") or None
    party_id = request.args.get("party_id", type=int)
    status = request.args.get("status") or None  # aperto | pagato | stornato
    date_from = request.args.get("date_from") or None
    date_to = request.args.get("date_to") or None

    query = JournalEntry.query
    if doc_type:
        query = query.filter(JournalEntry.doc_type == doc_type)
    if source_module:
        query = query.filter(JournalEntry.source_module == source_module)
    if party_id:
        query = query.filter(JournalEntry.economic_subject_id == party_id)
    if status == "aperto":
        query = query.filter(JournalEntry.is_paid.is_(False), JournalEntry.is_reversed.is_(False))
    elif status == "pagato":
        query = query.filter(JournalEntry.is_paid.is_(True))
    elif status == "stornato":
        query = query.filter(JournalEntry.is_reversed.is_(True))
    if date_from:
        try:
            query = query.filter(JournalEntry.doc_date >= datetime.strptime(date_from, "%Y-%m-%d").date())
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(JournalEntry.doc_date <= datetime.strptime(date_to, "%Y-%m-%d").date())
        except ValueError:
            pass

    entries = query.order_by(JournalEntry.created_at.desc()).paginate(page=page, per_page=25, error_out=False)

    # Riepilogo per tipo documento — SEMPRE sul totale (non filtrato), è la
    # "prova del nove": qui vedi in un colpo d'occhio quante DR/KR/PG/... sono
    # arrivate in Prima Nota, da confrontare con quante fatture/DDT/buste
    # risultano emesse/ricevute nei rispettivi moduli (SD/MM/Paghe).
    counts_raw = (db.session.query(JournalEntry.doc_type, db.func.count(JournalEntry.id))
                  .group_by(JournalEntry.doc_type).all())
    doc_type_summary = sorted(
        ({"doc_type": t, "label": DOC_TYPE_LABELS.get(t, t), "count": c} for t, c in counts_raw),
        key=lambda r: -r["count"])

    all_doc_types = [t for t, in db.session.query(JournalEntry.doc_type).distinct().order_by(JournalEntry.doc_type)]
    all_modules = [m for m, in db.session.query(JournalEntry.source_module).distinct().order_by(JournalEntry.source_module)]
    parties = EconomicSubject.query.order_by(EconomicSubject.name).all()

    return render_template("gl/journal_list.html", entries=entries, doc_type_summary=doc_type_summary,
                           doc_type_labels=DOC_TYPE_LABELS, all_doc_types=all_doc_types,
                           all_modules=all_modules, parties=parties,
                           filters={"doc_type": doc_type, "source_module": source_module,
                                    "party_id": party_id, "status": status,
                                    "date_from": date_from, "date_to": date_to},
                           pager_args={k: v for k, v in {
                               "doc_type": doc_type, "source_module": source_module,
                               "party_id": party_id, "status": status,
                               "date_from": date_from, "date_to": date_to}.items() if v})


@gl_bp.route("/piano-conti")
@login_required
def piano_conti():
    """Piano dei Conti — elenco completo (attivi e non) per verificare a colpo
    d'occhio cosa esiste davvero nel database, senza doverlo dedurre dal
    menu a tendina della Prima Nota."""
    accounts = Account.query.order_by(Account.account_type, Account.code).all()
    gruppi = {}
    for a in accounts:
        gruppi.setdefault(a.account_type, []).append(a)
    ordine_tipi = ["patrimoniale_attivo", "patrimoniale_passivo", "costo", "ricavo"]
    etichette_tipo = {"patrimoniale_attivo": "Stato Patrimoniale — Attivo",
                       "patrimoniale_passivo": "Stato Patrimoniale — Passivo",
                       "costo": "Conto Economico — Costi", "ricavo": "Conto Economico — Ricavi"}
    return render_template("gl/piano_conti.html", gruppi=gruppi, ordine_tipi=ordine_tipi,
                           etichette_tipo=etichette_tipo, totale=len(accounts))


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
    code_to_name = {a.code: a.name for a in accounts}

    # Classificazione deterministica (a regole, non AI) del documento in una
    # classe nota di operazione — se riconosciuta, restringe l'AI ai conti
    # esatti di quella classe invece di lasciarla scegliere su tutto il
    # piano dei conti. Se il documento non rientra in nessuna classe nota,
    # o la classe non ha uno schema fisso (es. rettifica generica), si
    # procede comunque con l'AI generica ma si segnala l'incertezza.
    testo_da_classificare = f"{descrizione}\n{testo_documento or ''}"
    classe_chiave, confidenza_classe = classifica(testo_da_classificare)
    guida_extra = None
    contatta_commercialista = False
    motivo_alert = None
    classe_nome = None
    rettifica_chiave = None

    if classe_chiave and not CLASSI_OPERAZIONE[classe_chiave].get("sempre_incerta"):
        # una delle 12 classi ordinarie riconosciuta con schema fisso
        classe_nome = CLASSI_OPERAZIONE[classe_chiave]["nome"]
        if confidenza_classe >= 0.6:
            guida_extra = guida_per_classe(classe_chiave, code_to_name)
    else:
        # non è una delle 12 ordinarie (o è RETTIFICA_GENERICA): prova il
        # catalogo dettagliato delle rettifiche (37 sottoclassi A1-H3)
        rettifica_chiave, confidenza_rettifica = classifica_rettifica(testo_da_classificare)
        if rettifica_chiave:
            sc = CATALOGO_RETTIFICHE[rettifica_chiave]
            classe_nome = f"{rettifica_chiave} — {sc['nome']}"
            if confidenza_rettifica >= 0.6:
                guida_extra = guida_per_rettifica(rettifica_chiave, code_to_name)
            if sc.get("conto_mancante"):
                contatta_commercialista = True
                motivo_alert = (f"Sottoclasse '{sc['nome']}' richiede un conto che non esiste "
                                 f"ancora nel piano dei conti: nessuna proposta completa possibile.")
            elif sc["livello"] == "M":
                contatta_commercialista = True
                motivo_alert = (f"Sottoclasse '{sc['nome']}' è di livello M (sempre manuale): "
                                 f"richiede giudizio professionale specifico, non solo il click "
                                 f"di conferma ordinario.")
        else:
            contatta_commercialista = True
            motivo_alert = "Documento non riconducibile a nessuna classe o sottoclasse nota."

    try:
        suggerimento = suggerisci_scrittura(descrizione, accounts, testo_documento=testo_documento,
                                             tipo_documento=tipo_documento, guida_extra=guida_extra)
    except AISuggestionError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Errore imprevisto: {e}"}), 500

    # L'AI conosce solo i CODICI conto (non gli id del database) — li risolviamo qui.
    code_to_id = {a.code: a.id for a in accounts}
    righe_risolte = []
    avvisi = []
    if guida_extra and classe_chiave and not CLASSI_OPERAZIONE[classe_chiave].get("sempre_incerta"):
        conti_ammessi_classe = set(
            CLASSI_OPERAZIONE[classe_chiave].get("dare_fissi", []) +
            CLASSI_OPERAZIONE[classe_chiave].get("dare_variabili", []) +
            CLASSI_OPERAZIONE[classe_chiave].get("avere_fissi", []) +
            CLASSI_OPERAZIONE[classe_chiave].get("avere_variabili", [])
        )
        for line in suggerimento.get("lines", []):
            if str(line.get("account_code", "")).strip() not in conti_ammessi_classe:
                contatta_commercialista = True
                motivo_alert = (f"L'AI ha proposto un conto fuori dallo schema della classe "
                                 f"'{classe_nome}'.")
                break
    elif guida_extra and rettifica_chiave:
        conti_ammessi_rettifica = set()
        for variante in CATALOGO_RETTIFICHE[rettifica_chiave].get("schema", []):
            for riga in variante:
                conti_ammessi_rettifica.add(riga["conto"])
        for line in suggerimento.get("lines", []):
            if str(line.get("account_code", "")).strip() not in conti_ammessi_rettifica:
                contatta_commercialista = True
                motivo_alert = (f"L'AI ha proposto un conto fuori dallo schema della sottoclasse "
                                 f"'{classe_nome}'.")
                break
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

    totale_dare = sum(r["amount"] for r in righe_risolte if r["pk"] == "40")
    totale_avere = sum(r["amount"] for r in righe_risolte if r["pk"] == "50")
    if abs(totale_dare - totale_avere) > 0.01:
        contatta_commercialista = True
        motivo_alert = (f"La proposta non pareggia (Dare {totale_dare:.2f} / Avere {totale_avere:.2f}): "
                         f"verificare con il commercialista prima di registrare.")

    return jsonify({
        "description": suggerimento.get("description") or descrizione,
        "lines": righe_risolte,
        "note": suggerimento.get("note"),
        "avvisi": avvisi,
        "classe_operazione": classe_nome,
        "contatta_commercialista": contatta_commercialista,
        "motivo_alert": motivo_alert,
    })
