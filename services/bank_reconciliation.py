"""
services/bank_reconciliation.py — Fase 4 (progettazione parti mancanti,
punto 6): riconciliazione bancaria.
"""
import csv
import hashlib
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation

from extensions import db
from models import BankStatement, BankStatementLine, BankReconciliationAllocation, JournalLine, JournalEntry


class BankReconciliationError(ValueError):
    pass


def _line_hash(value_date, description, amount, row_index):
    """Hash CONTESTUALIZZATO — l'unicità è (statement_id, hash), garantita
    dal vincolo a livello di tabella, non da questo hash da solo. row_index
    è incluso apposta: due righe realmente identiche (stesso importo,
    stessa data, stessa descrizione) nello stesso file NON devono essere
    scartate come falsi duplicati — sono due movimenti reali distinti."""
    raw = f"{value_date}|{description}|{amount}|{row_index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def import_statement_csv(bank_account, file_bytes, filename, imported_by_id=None):
    """Importa un CSV con colonne: data, descrizione, importo, saldo_iniziale,
    saldo_finale (le ultime due solo sulla prima riga, opzionali per riga).
    Formato atteso: data (YYYY-MM-DD), descrizione (testo libero),
    importo (positivo=accredito, negativo=addebito).
    """
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise BankReconciliationError("File non leggibile come testo UTF-8 — verifica l'export della banca.")

    file_fingerprint = hashlib.sha256(file_bytes).hexdigest()
    if BankStatement.query.filter_by(bank_account_id=bank_account.id, file_hash=file_fingerprint).first():
        raise BankReconciliationError(
            "Questo identico file risulta già importato per questo conto — "
            "reimportarlo creerebbe righe duplicate."
        )

    reader = csv.DictReader(io.StringIO(text))
    required = {"data", "descrizione", "importo"}
    if not reader.fieldnames or not required.issubset({f.strip().lower() for f in reader.fieldnames}):
        raise BankReconciliationError(
            "Il CSV deve avere le colonne: data, descrizione, importo "
            "(più saldo_iniziale, saldo_finale opzionali)."
        )

    rows = list(reader)
    if not rows:
        raise BankReconciliationError("Il file è vuoto.")

    parsed = []
    for idx, row in enumerate(rows):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items()}
        try:
            value_date = datetime.strptime(row["data"], "%Y-%m-%d").date()
        except ValueError:
            raise BankReconciliationError(f"Riga {idx + 2}: data '{row['data']}' non in formato YYYY-MM-DD.")
        try:
            amount = Decimal(row["importo"].replace(",", "."))
        except (InvalidOperation, KeyError):
            raise BankReconciliationError(f"Riga {idx + 2}: importo '{row.get('importo')}' non valido.")
        parsed.append((value_date, row.get("descrizione", ""), amount, row.get("bank_transaction_id", "")))

    saldo_iniziale = rows[0].get("saldo_iniziale") or rows[0].get("Saldo_Iniziale")
    saldo_finale = rows[0].get("saldo_finale") or rows[0].get("Saldo_Finale")
    try:
        opening = Decimal(str(saldo_iniziale).replace(",", ".")) if saldo_iniziale else None
        closing = Decimal(str(saldo_finale).replace(",", ".")) if saldo_finale else None
    except InvalidOperation:
        opening = closing = None

    movimento_totale = sum((a for _, _, a, _ in parsed), Decimal("0"))
    if opening is not None and closing is not None:
        if (opening + movimento_totale) != closing:
            raise BankReconciliationError(
                f"Controllo di quadratura fallito: saldo iniziale {opening:.2f} + movimenti "
                f"{movimento_totale:.2f} = {(opening + movimento_totale):.2f}, ma il saldo finale "
                f"dichiarato è {closing:.2f} — verifica il file prima di importarlo."
            )
    else:
        opening = opening if opening is not None else Decimal("0")
        closing = closing if closing is not None else opening + movimento_totale

    statement = BankStatement(
        bank_account_id=bank_account.id,
        period_from=min(d for d, _, _, _ in parsed), period_to=max(d for d, _, _, _ in parsed),
        opening_balance=opening, closing_balance=closing,
        import_filename=filename, file_hash=file_fingerprint, imported_by_id=imported_by_id,
    )
    db.session.add(statement)
    db.session.flush()

    for idx, (value_date, description, amount, bank_tx_id) in enumerate(parsed):
        h = _line_hash(value_date, description, amount, idx)
        db.session.add(BankStatementLine(
            statement_id=statement.id, value_date=value_date, description=description,
            amount=amount, bank_transaction_id=bank_tx_id or None, import_hash=h,
        ))
    db.session.commit()
    return statement, len(parsed)


def auto_match(statement_id, created_by_id=None, date_tolerance_days=3):
    """Matching automatico di primo livello: stesso importo (in valore
    assoluto) e data entro tolleranza, tra righe non ancora riconciliate.
    Crea solo abbinamenti 1:1 INEQUIVOCABILI (un solo candidato possibile
    per importo); tutto il resto resta per il matching manuale."""
    lines = BankStatementLine.query.filter_by(statement_id=statement_id).all()
    matched = 0
    for sl in lines:
        if sl.is_reconciled:
            continue
        residual = sl.residual_amount
        if residual <= 0:
            continue
        candidates = (JournalLine.query.join(JournalEntry)
                     .filter(JournalLine.account_id == BankStatement.query.get(statement_id).bank_account_id,
                             JournalEntry.is_reversed.is_(False)).all())
        exact = []
        for jl in candidates:
            jl_amount = Decimal(str(jl.dare)) - Decimal(str(jl.avere))  # segno coerente con l'estratto conto
            jl_residual = Decimal(str(abs(jl_amount))) - sum(
                (a.amount_allocated for a in jl.bank_allocations if not a.reversed), Decimal("0")
            )
            if jl_residual <= 0:
                continue
            same_sign = (jl_amount > 0) == (Decimal(str(sl.amount)) > 0)
            same_amount = abs(jl_residual - residual) < Decimal("0.01")
            close_date = abs((jl.entry.doc_date - sl.value_date).days) <= date_tolerance_days
            if same_sign and same_amount and close_date:
                exact.append((jl, jl_residual))
        if len(exact) == 1:
            jl, jl_residual = exact[0]
            db.session.add(BankReconciliationAllocation(
                statement_line_id=sl.id, journal_line_id=jl.id,
                amount_allocated=min(residual, jl_residual), created_by_id=created_by_id,
            ))
            matched += 1
    db.session.commit()
    return matched


def manual_match(statement_line_id, journal_line_id, amount, created_by_id=None):
    sl = BankStatementLine.query.get(statement_line_id)
    jl = JournalLine.query.get(journal_line_id)
    if sl is None or jl is None:
        raise BankReconciliationError("Riga estratto conto o riga contabile non trovata.")
    amount = Decimal(str(amount))
    if amount <= 0:
        raise BankReconciliationError("L'importo da abbinare deve essere positivo.")
    if amount > sl.residual_amount:
        raise BankReconciliationError(
            f"Importo richiesto ({amount:.2f} €) superiore al residuo della riga estratto conto "
            f"({sl.residual_amount:.2f} €)."
        )
    jl_amount = abs(Decimal(str(jl.dare)) - Decimal(str(jl.avere)))
    jl_allocated = sum((a.amount_allocated for a in jl.bank_allocations if not a.reversed), Decimal("0"))
    jl_residual = jl_amount - jl_allocated
    if amount > jl_residual:
        raise BankReconciliationError(
            f"Importo richiesto ({amount:.2f} €) superiore al residuo della riga contabile "
            f"({jl_residual:.2f} €)."
        )
    alloc = BankReconciliationAllocation(
        statement_line_id=sl.id, journal_line_id=jl.id,
        amount_allocated=amount, created_by_id=created_by_id,
    )
    db.session.add(alloc)
    db.session.commit()
    return alloc
