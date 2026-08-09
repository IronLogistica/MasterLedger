from datetime import datetime
from decimal import Decimal, InvalidOperation
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user

from extensions import db
from models import Account, AccountMapping, CostCenter, JournalEntry, JournalLine, EconomicSubject
from services.posting import post_journal_entry, reverse_journal_entry, UnbalancedEntryError, PeriodClosedError
from services.co import validate_co_assignment, COValidationError
from services.ai_posting import suggerisci_scrittura, estrai_testo_pdf, AISuggestionError
from services.classificazione_operazioni import classifica, guida_per_classe, CLASSI_OPERAZIONE
from services.rettifiche_operazioni import classifica_rettifica, guida_per_rettifica, CATALOGO_RETTIFICHE
from services.payments import allocate_payment, PaymentAllocationError
from services.bank_reconciliation import (import_statement_csv, auto_match, manual_match,
                                          BankReconciliationError)
from blueprints.decorators import commercialista_required
from models import (AccountingPeriod, AccountingPeriodLog, FiscalParameter, InvoiceInstallment,
                    PaymentAllocation, BankStatement, BankStatementLine, BankReconciliationAllocation)
from datetime import date
import calendar

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


@gl_bp.route("/periods", methods=["GET"])
@login_required
@commercialista_required
def periods():
    """Elenco periodi contabili — Fase 2, punto 5 della progettazione."""
    all_periods = AccountingPeriod.query.order_by(AccountingPeriod.year.desc(), AccountingPeriod.month.desc()).all()
    enforced = FiscalParameter.query.filter_by(key="period_lock_enforced").first()
    lock_active = bool(enforced and str(enforced.value).lower() == "true")
    return render_template("gl/periods.html", periods=all_periods, lock_active=lock_active)


@gl_bp.route("/periods/create", methods=["POST"])
@login_required
@commercialista_required
def periods_create():
    year = request.form.get("year", type=int)
    month = request.form.get("month", type=int)
    if not year or not month or not (1 <= month <= 12):
        flash("Anno e mese sono obbligatori (mese 1-12).", "danger")
        return redirect(url_for("gl.periods"))
    if AccountingPeriod.query.filter_by(company="Iron Appalti", year=year, month=month).first():
        flash(f"Il periodo {month:02d}/{year} esiste già — nessuna sovrapposizione permessa.", "danger")
        return redirect(url_for("gl.periods"))
    last_day = calendar.monthrange(year, month)[1]
    period = AccountingPeriod(
        company="Iron Appalti", year=year, month=month,
        start_date=date(year, month, 1), end_date=date(year, month, last_day),
        period_type="mensile", status="aperto",
    )
    db.session.add(period)
    db.session.commit()
    flash(f"Periodo {month:02d}/{year} creato (aperto).", "success")
    return redirect(url_for("gl.periods"))


@gl_bp.route("/periods/<int:period_id>/close", methods=["POST"])
@login_required
@commercialista_required
def periods_close(period_id):
    period = AccountingPeriod.query.get_or_404(period_id)
    if not period.is_open:
        flash("Il periodo è già chiuso.", "danger")
        return redirect(url_for("gl.periods"))
    period.status = "chiuso"
    period.closed_by_id = current_user.id
    period.closed_at = datetime.utcnow()
    db.session.add(AccountingPeriodLog(period_id=period.id, action="chiusura",
                                       performed_by_id=current_user.id,
                                       reason=request.form.get("reason") or None))
    db.session.commit()
    flash(f"Periodo {period.month:02d}/{period.year} chiuso.", "success")
    return redirect(url_for("gl.periods"))


@gl_bp.route("/periods/<int:period_id>/reopen", methods=["POST"])
@login_required
@commercialista_required
def periods_reopen(period_id):
    period = AccountingPeriod.query.get_or_404(period_id)
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("La riapertura di un periodo chiuso richiede un motivo scritto.", "danger")
        return redirect(url_for("gl.periods"))
    if period.is_open:
        flash("Il periodo è già aperto.", "danger")
        return redirect(url_for("gl.periods"))
    period.status = "riaperto_temporaneamente"
    period.reopen_reason = reason
    db.session.add(AccountingPeriodLog(period_id=period.id, action="riapertura",
                                       performed_by_id=current_user.id, reason=reason))
    db.session.commit()
    flash(f"Periodo {period.month:02d}/{period.year} riaperto temporaneamente. Motivo registrato nel log.", "success")
    return redirect(url_for("gl.periods"))


@gl_bp.route("/periods/lock-toggle", methods=["POST"])
@login_required
@commercialista_required
def periods_lock_toggle():
    """Attiva/disattiva il blocco su periodi ASSENTI (non ancora creati).
    Va attivato non appena i periodi correnti sono stati creati — lasciarlo
    disattivo indefinitamente vanifica lo scopo della chiusura periodi."""
    enforced = FiscalParameter.query.filter_by(key="period_lock_enforced").first()
    new_value = "false" if (enforced and enforced.value.lower() == "true") else "true"
    if enforced:
        enforced.value = new_value
        enforced.updated_by_id = current_user.id
    else:
        db.session.add(FiscalParameter(key="period_lock_enforced", value=new_value,
                                       description="Se 'true', blocca le registrazioni su date senza un periodo contabile creato.",
                                       category="periodi", updated_by_id=current_user.id))
    db.session.commit()
    flash(f"Blocco su periodi assenti ora {'ATTIVO' if new_value == 'true' else 'disattivo'}.", "success")
    return redirect(url_for("gl.periods"))


@gl_bp.route("/riconciliazione-bancaria", methods=["GET"])
@login_required
def bank_reconciliation():
    """Fase 4 (progettazione parti mancanti, punto 6) — vista principale."""
    statements = BankStatement.query.order_by(BankStatement.period_from.desc()).all()
    bank_accounts = Account.query.filter(Account.account_type == "patrimoniale_attivo",
                                         Account.active.is_(True)).order_by(Account.code).all()
    statement_id = request.args.get("statement_id", type=int)
    lines = []
    unmatched_gl_lines = []
    if statement_id:
        statement = BankStatement.query.get(statement_id)
        lines = BankStatementLine.query.filter_by(statement_id=statement_id).order_by(BankStatementLine.value_date).all()
        if statement:
            candidates = (JournalLine.query.join(JournalEntry)
                         .filter(JournalLine.account_id == statement.bank_account_id,
                                 JournalEntry.is_reversed.is_(False))
                         .order_by(JournalEntry.doc_date.desc()).limit(200).all())
            for jl in candidates:
                jl_amount = abs(Decimal(str(jl.dare)) - Decimal(str(jl.avere)))
                allocated = sum((a.amount_allocated for a in jl.bank_allocations if not a.reversed), Decimal("0"))
                if jl_amount - allocated > 0:
                    unmatched_gl_lines.append(jl)
    return render_template("gl/bank_reconciliation.html", statements=statements, bank_accounts=bank_accounts,
                           lines=lines, statement_id=statement_id, unmatched_gl_lines=unmatched_gl_lines)


@gl_bp.route("/riconciliazione-bancaria/importa", methods=["POST"])
@login_required
def bank_reconciliation_import():
    bank_account_id = request.form.get("bank_account_id", type=int)
    bank_account = Account.query.get(bank_account_id) if bank_account_id else None
    file = request.files.get("csv_file")
    if bank_account is None or not file or not file.filename:
        flash("Seleziona il conto banca e un file CSV.", "danger")
        return redirect(url_for("gl.bank_reconciliation"))
    try:
        statement, n = import_statement_csv(bank_account, file.read(), file.filename, imported_by_id=current_user.id)
        n_matched = auto_match(statement.id, created_by_id=current_user.id)
        flash(f"Estratto conto importato: {n} righe, {n_matched} abbinate automaticamente.", "success")
        return redirect(url_for("gl.bank_reconciliation", statement_id=statement.id))
    except BankReconciliationError as e:
        db.session.rollback()
        flash(str(e), "danger")
        return redirect(url_for("gl.bank_reconciliation"))


@gl_bp.route("/riconciliazione-bancaria/abbina", methods=["POST"])
@login_required
def bank_reconciliation_match():
    statement_line_id = request.form.get("statement_line_id", type=int)
    journal_line_id = request.form.get("journal_line_id", type=int)
    amount = request.form.get("amount")
    statement_id = request.form.get("statement_id", type=int)
    try:
        manual_match(statement_line_id, journal_line_id, amount, created_by_id=current_user.id)
        flash("Abbinamento registrato.", "success")
    except (BankReconciliationError, ValueError) as e:
        flash(str(e), "danger")
    return redirect(url_for("gl.bank_reconciliation", statement_id=statement_id))


@gl_bp.route("/account-mapping", methods=["GET", "POST"])
@login_required
@commercialista_required
def account_mapping():
    """Piano dei conti canonico — configurazione dei concetti contabili usati
    da AP/AR/GL/Cespiti (banca, IVA a credito/debito, crediti clienti, debiti
    fornitori, cespiti, ammortamenti, ritenute...). Solo ruolo commercialista.

    Progettazione parti mancanti, punto 0: prima di questa pagina i codici
    erano scritti a mano in ogni blueprint (_get_account_by_code("180000")
    sparso in AP/AR/GL/Cespiti). Ora si cambiano da un solo posto.
    """
    mappings = AccountMapping.query.order_by(AccountMapping.category, AccountMapping.label).all()
    accounts = Account.query.filter_by(active=True).order_by(Account.code).all()

    # Natura attesa per concetto — stesso controllo già applicato in payroll/config.html,
    # per evitare che un errore di distrazione punti "IVA a Debito" su un conto di costo.
    EXPECTED_NATURE = {
        "banca_principale": "patrimoniale_attivo", "iva_credito": "patrimoniale_attivo",
        "iva_debito": "patrimoniale_passivo", "crediti_clienti": "patrimoniale_attivo",
        "debiti_fornitori": "patrimoniale_passivo", "cespiti_impianti": "patrimoniale_attivo",
        "ammortamenti_costo": "costo", "fondo_ammortamento": "patrimoniale_passivo",
        "ritenute_professionisti": "patrimoniale_passivo",
        "abbuoni_attivi": "costo", "abbuoni_passivi": "ricavo",
    }

    if request.method == "POST":
        for m in mappings:
            new_account_id = request.form.get(f"account_{m.id}", type=int)
            if new_account_id and new_account_id != m.account_id:
                new_account = Account.query.get(new_account_id)
                if not new_account or not new_account.active:
                    flash(f"Conto selezionato non valido/attivo per '{m.label}'.", "danger")
                    return render_template("gl/account_mapping.html", mappings=mappings, accounts=accounts)
                expected = EXPECTED_NATURE.get(m.concept_key)
                if expected and new_account.account_type != expected:
                    flash(f"Il conto {new_account.code} non ha natura coerente per '{m.label}' (attesa: {expected}).", "danger")
                    return render_template("gl/account_mapping.html", mappings=mappings, accounts=accounts)
                m.account_id = new_account.id
                m.updated_by_id = current_user.id
        db.session.commit()
        flash("Piano dei conti canonico aggiornato. Le nuove registrazioni useranno i conti selezionati; quelle già registrate NON vengono modificate.", "success")
        return redirect(url_for("gl.account_mapping"))

    return render_template("gl/account_mapping.html", mappings=mappings, accounts=accounts)


@gl_bp.route("/scadenzario")
@login_required
def scadenzario():
    """Fase 3 — elenco rate aperte/scadute, cliente e fornitore insieme.
    Sola lettura: il pagamento vero avviene in /gl/scadenzario/paga."""
    subject_id = request.args.get("subject_id", type=int)
    only_overdue = request.args.get("only_overdue") == "1"

    query = (InvoiceInstallment.query
             .join(JournalEntry, InvoiceInstallment.entry_id == JournalEntry.id)
             .filter(InvoiceInstallment.residual_amount > 0, JournalEntry.is_reversed.is_(False)))
    if subject_id:
        query = query.filter(JournalEntry.economic_subject_id == subject_id)
    installments = query.order_by(InvoiceInstallment.due_date.asc()).all()
    if only_overdue:
        installments = [i for i in installments if i.is_overdue]

    subjects = EconomicSubject.query.filter_by(active=True).order_by(EconomicSubject.name).all()
    return render_template("gl/scadenzario.html", installments=installments, subjects=subjects,
                           subject_id=subject_id, only_overdue=only_overdue)


@gl_bp.route("/scadenzario/paga", methods=["GET", "POST"])
@login_required
def scadenzario_paga():
    """Pagamento/incasso PARZIALE su una o più rate dello stesso soggetto —
    Fase 3, alternativa granulare a supplier_payment/customer_payment
    (che restano per la compensazione a saldo pieno, invariate)."""
    if request.method == "GET":
        ids = request.args.getlist("installment_id", type=int)
        installments = InvoiceInstallment.query.filter(InvoiceInstallment.id.in_(ids)).all() if ids else []
        return render_template("gl/scadenzario_paga.html", installments=installments)

    raw_ids = request.form.getlist("installment_id[]", type=int)
    installments = InvoiceInstallment.query.filter(InvoiceInstallment.id.in_(raw_ids)).all()
    if not installments:
        flash("Nessuna rata selezionata.", "warning")
        return redirect(url_for("gl.scadenzario"))

    subject_ids = {i.entry.economic_subject_id for i in installments}
    doc_types = {i.entry.doc_type for i in installments}
    if len(subject_ids) != 1 or None in subject_ids:
        flash("Seleziona rate di un unico soggetto economico.", "danger")
        return redirect(url_for("gl.scadenzario"))
    if len(doc_types) != 1 or not doc_types.issubset({"KR", "DR"}):
        flash("Non è possibile mescolare rate fornitore e rate cliente nello stesso pagamento.", "danger")
        return redirect(url_for("gl.scadenzario"))
    is_supplier_side = doc_types == {"KR"}

    try:
        allocations = []
        total_cash = Decimal("0")
        total_abbuono = Decimal("0")
        for inst in installments:
            cash = Decimal(request.form.get(f"cash_{inst.id}", "0") or "0")
            abbuono = Decimal(request.form.get(f"abbuono_{inst.id}", "0") or "0")
            if cash == 0 and abbuono == 0:
                continue
            allocations.append({"installment_id": inst.id, "cash_amount": cash, "abbuono_amount": abbuono})
            total_cash += cash
            total_abbuono += abbuono
        if not allocations:
            flash("Nessun importo inserito.", "warning")
            return redirect(url_for("gl.scadenzario"))
        if total_cash <= 0:
            flash("Il pagamento deve movimentare un importo in contanti positivo (l'abbuono da solo non basta).", "danger")
            return redirect(url_for("gl.scadenzario"))

        economic_subject_id = next(iter(subject_ids))
        refs = [i.entry.doc_number for i in installments]
        # L'importo che chiude la partita fornitore/cliente è cash + abbuono
        # (l'intero debito/credito estinto); la banca movimenta SOLO il cash;
        # l'abbuono, se presente, va sul conto autorizzato — così la
        # scrittura è GIÀ bilanciata al momento della creazione, non dopo.
        total_extinto = total_cash + total_abbuono
        if is_supplier_side:
            ap_account = AccountMapping.get_or_error("debiti_fornitori")
            bank_account = AccountMapping.get_or_error("banca_principale")
            lines = [
                {"account_id": ap_account.id, "dare": total_extinto, "avere": 0},
                {"account_id": bank_account.id, "dare": 0, "avere": total_cash},
            ]
            if total_abbuono > 0:
                abbuono_account = AccountMapping.get_or_error("abbuoni_passivi")
                lines.append({"account_id": abbuono_account.id, "dare": 0, "avere": total_abbuono})
            payment_entry = post_journal_entry(
                doc_type="KZ", prefix="15", doc_date=None,
                description=f"Pagamento parziale fornitore — rate {', '.join(refs)}",
                lines=lines, source_module="LEDGER", reference=", ".join(refs),
                created_by_id=current_user.id, economic_subject_id=economic_subject_id,
                gross_amount=total_cash, commit=False,
            )
        else:
            ar_account = AccountMapping.get_or_error("crediti_clienti")
            bank_account = AccountMapping.get_or_error("banca_principale")
            lines = [
                {"account_id": bank_account.id, "dare": total_cash, "avere": 0},
                {"account_id": ar_account.id, "dare": 0, "avere": total_extinto},
            ]
            if total_abbuono > 0:
                abbuono_account = AccountMapping.get_or_error("abbuoni_attivi")
                lines.append({"account_id": abbuono_account.id, "dare": total_abbuono, "avere": 0})
            payment_entry = post_journal_entry(
                doc_type="DZ", prefix="14", doc_date=None,
                description=f"Incasso parziale cliente — rate {', '.join(refs)}",
                lines=lines, source_module="LEDGER", reference=", ".join(refs),
                created_by_id=current_user.id, economic_subject_id=economic_subject_id,
                gross_amount=total_cash, commit=False,
            )

        cash_alloc, abbuono_alloc, unallocated = allocate_payment(
            payment_entry, allocations, created_by_id=current_user.id
        )
        db.session.commit()
        flash(f"Pagamento parziale registrato: Doc. {payment_entry.doc_number} — "
              f"contanti {cash_alloc:.2f} €, abbuoni {abbuono_alloc:.2f} €.", "success")
        return redirect(url_for("gl.entry_detail", entry_id=payment_entry.id))
    except (UnbalancedEntryError, PeriodClosedError, PaymentAllocationError, ValueError) as e:
        db.session.rollback()
        flash(str(e), "danger")
        return redirect(url_for("gl.scadenzario"))


@gl_bp.route("/mastrino/<int:account_id>")
@login_required
def mastrino_conto(account_id):
    """Mastrino con importi Decimal e saldo progressivo comprensivo dell'apertura."""
    account = Account.query.get_or_404(account_id)
    date_from = request.args.get("date_from") or None
    date_to = request.args.get("date_to") or None
    parsed_from = parsed_to = None
    try:
        parsed_from = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else None
        parsed_to = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else None
        if parsed_from and parsed_to and parsed_from > parsed_to:
            raise ValueError
    except ValueError:
        flash("Intervallo date non valido.", "warning")
        parsed_from = parsed_to = None
        date_from = date_to = None

    base_query = (JournalLine.query.join(JournalEntry)
                  .filter(JournalLine.account_id == account_id))
    saldo_iniziale = Decimal("0")
    if parsed_from:
        prior = base_query.filter(JournalEntry.doc_date < parsed_from).all()
        saldo_iniziale = sum(
            (Decimal(str(r.dare or 0)) - Decimal(str(r.avere or 0)) for r in prior), Decimal("0")
        )
    query = base_query
    if parsed_from:
        query = query.filter(JournalEntry.doc_date >= parsed_from)
    if parsed_to:
        query = query.filter(JournalEntry.doc_date <= parsed_to)
    righe = query.order_by(JournalEntry.doc_date.asc(), JournalEntry.id.asc(), JournalLine.id.asc()).all()

    movimenti = []
    saldo = saldo_iniziale
    for r in righe:
        dare = Decimal(str(r.dare or 0))
        avere = Decimal(str(r.avere or 0))
        saldo += dare - avere
        movimenti.append({"entry": r.entry, "dare": dare, "avere": avere,
                          "saldo_progressivo": saldo, "cost_center": r.cost_center})
    totale_dare = sum((m["dare"] for m in movimenti), Decimal("0"))
    totale_avere = sum((m["avere"] for m in movimenti), Decimal("0"))
    return render_template(
        "gl/mastrino_conto.html", account=account, movimenti=movimenti,
        totale_dare=totale_dare, totale_avere=totale_avere, saldo_iniziale=saldo_iniziale,
        saldo_finale=saldo, filters={"date_from": date_from, "date_to": date_to}
    )


@gl_bp.route("/partitario/<int:economic_subject_id>")
@login_required
def partitario(economic_subject_id):
    """Partitario individuale cliente/fornitore — tutti i documenti (fatture,
    pagamenti/incassi, note credito) legati a questo soggetto economico, con
    stato aperto/pagato/stornato e saldo residuo. Fino ad oggi l'unico modo
    di vedere la posizione di un cliente/fornitore era filtrare il Giornale
    per controparte: qui invece si vede la partita, non solo l'elenco.
    """
    party = EconomicSubject.query.get_or_404(economic_subject_id)

    entries = (JournalEntry.query
               .filter(JournalEntry.economic_subject_id == economic_subject_id)
               .order_by(JournalEntry.doc_date.asc(), JournalEntry.id.asc())
               .all())

    documenti = []
    saldo_aperto = Decimal("0")
    for e in entries:
        importo = (Decimal(str(e.gross_amount)) if e.gross_amount is not None
                   else Decimal(str(e.total_dare)))
        if e.doc_type in ("KR", "DR", "DG"):
            # Documenti-fattura: hanno un proprio stato aperto/pagato (is_paid).
            # Contano nel saldo SOLO finché sono aperti — una volta pagati,
            # il pagamento/incasso collegato li azzera (non va sommato di nuovo).
            segno = Decimal("-1") if e.doc_type == "DG" else Decimal("1")
            if not e.is_reversed and not e.is_paid:
                saldo_aperto += segno * importo
            stato = "Stornato" if e.is_reversed else ("Pagato/Incassato" if e.is_paid else "Aperto")
        else:
            # Pagamenti/incassi (KZ/DZ): non hanno un proprio "aperto" — servono
            # solo a chiudere la fattura collegata, che si è già azzerata sopra.
            # Compaiono qui solo per tracciabilità, saldo_aperto invariato.
            stato = "Stornato" if e.is_reversed else "Eseguito"
        documenti.append({
            "entry": e,
            "importo": importo,
            "stato": stato,
        })

    return render_template("gl/partitario.html", party=party, documenti=documenti,
                           saldo_aperto=saldo_aperto, doc_type_labels=DOC_TYPE_LABELS)


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
        try:
            if not (len(account_ids) == len(pks) == len(amounts) == len(cost_centers_sel)):
                raise ValueError("Righe contabili incomplete o alterate.")
            for acc_id, pk, amt, cc in zip(account_ids, pks, amounts, cost_centers_sel):
                if not acc_id and not amt:
                    continue
                if not acc_id or not amt or pk not in ("40", "50"):
                    raise ValueError("Ogni riga deve indicare conto, lato Dare/Avere e importo.")
                amount = Decimal(amt.replace(",", "."))
                account, center = validate_co_assignment(int(acc_id), int(cc) if cc else None)
                lines.append({
                    "account_id": account.id,
                    "dare": amount if pk == "40" else 0,
                    "avere": amount if pk == "50" else 0,
                    "cost_center_id": center.id if center else None,
                })
        except (InvalidOperation, ValueError, TypeError, COValidationError) as e:
            flash(str(e) or "Righe contabili non valide.", "danger")
            return render_template("gl/journal_entry.html", accounts=accounts, cost_centers=cost_centers)

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
        # una delle classi ordinarie riconosciuta con schema fisso
        classe_nome = CLASSI_OPERAZIONE[classe_chiave]["nome"]
        if confidenza_classe >= 0.6:
            guida_extra = guida_per_classe(classe_chiave, code_to_name)
    else:
        # non è una delle ordinarie (o è RETTIFICA_GENERICA): prova il
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
