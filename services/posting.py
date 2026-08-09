"""
services/posting.py — registrazione centralizzata e validata della prima nota.

Tutti i moduli applicativi devono passare da ``post_journal_entry``: gli
importi vengono normalizzati ai centesimi *prima* del controllo, così ciò che
viene verificato è esattamente ciò che sarà persistito nei campi NUMERIC(14,2).
"""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from extensions import db
from models import (Account, CostCenter, EconomicSubject, JournalEntry,
                    JournalLine, DocumentSequence, AccountingPeriod, FiscalParameter,
                    InvoiceInstallment)


CENT = Decimal("0.01")


class UnbalancedEntryError(ValueError):
    """Scrittura non valida o non quadrata in Dare/Avere."""


class PeriodClosedError(ValueError):
    """Sollevata quando si tenta di registrare (o stornare) su un periodo
    contabile chiuso, o su un periodo assente mentre il blocco è attivo."""


def _check_period_open(effective_date):
    """Fase 2 (progettazione parti mancanti, punto 5) — nessuna scrittura
    entra in un periodo chiuso. Controllo centralizzato QUI, non ripetuto
    in ogni blueprint, così nessun modulo può dimenticarselo — vale sia
    per le registrazioni dirette sia per gli storni (che passano da qui).

    Il blocco su periodo ASSENTE (mai creato) è disattivabile con
    FiscalParameter(key='period_lock_enforced') — utile SOLO in fase di
    avvio, prima che l'azienda abbia creato i periodi. Una volta creati,
    va sempre lasciato attivo: un periodo mancante che si comporta come
    "aperto" a regime rischia di far rientrare dalla finestra una
    registrazione dimenticata su un mese già chiuso civilisticamente.
    """
    period = AccountingPeriod.find_for_date(effective_date)
    if period is None:
        enforced = FiscalParameter.query.filter_by(key="period_lock_enforced").first()
        if enforced and str(enforced.value).lower() == "true":
            raise PeriodClosedError(
                f"Nessun periodo contabile configurato per il {effective_date:%d/%m/%Y}. "
                f"Il blocco periodi è attivo: crea prima il periodo in Configurazione → Periodi contabili."
            )
        return  # periodo non configurato e blocco non ancora attivo: nessuna sorpresa, comportamento storico
    if not period.is_open:
        raise PeriodClosedError(
            f"Il periodo {period.month:02d}/{period.year} è CHIUSO — non è possibile registrare "
            f"né stornare su questa data. Serve una riapertura esplicita del commercialista."
        )


def _money(value, label):
    """Converte un input numerico in Decimal finito e arrotondato HALF_UP."""
    try:
        amount = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError, TypeError):
        raise UnbalancedEntryError(f"{label}: importo non valido.")
    if not amount.is_finite():
        raise UnbalancedEntryError(f"{label}: l'importo deve essere finito.")
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def post_journal_entry(doc_type, prefix, doc_date, description, lines, source_module="LEDGER",
                       reference=None, created_by_id=None, economic_subject_id=None,
                       gross_amount=None, vat_rate=None, natura=None, commit=True,
                       allow_inactive_accounts=False):
    """Crea una scrittura atomica dopo averne validato righe, conti e quadratura.

    Ogni riga deve avere un solo lato positivo (Dare oppure Avere). Importi
    negativi, non finiti, righe a zero e conti/centri inesistenti sono rifiutati.
    Con ``commit=False`` il chiamante può collegare il documento operativo e
    concludere tutto con un unico commit.
    """
    if not isinstance(lines, (list, tuple)) or len(lines) < 2:
        raise UnbalancedEntryError("Servono almeno due righe contabili.")

    effective_date = doc_date or date.today()
    _check_period_open(effective_date)

    normalized = []
    account_ids = set()
    center_ids = set()
    for idx, line in enumerate(lines, start=1):
        if not isinstance(line, dict) or not line.get("account_id"):
            raise UnbalancedEntryError(f"Riga {idx}: conto obbligatorio.")
        dare = _money(line.get("dare", 0), f"Riga {idx} Dare")
        avere = _money(line.get("avere", 0), f"Riga {idx} Avere")
        if dare < 0 or avere < 0:
            raise UnbalancedEntryError(f"Riga {idx}: Dare e Avere non possono essere negativi.")
        if (dare > 0) == (avere > 0):
            raise UnbalancedEntryError(
                f"Riga {idx}: valorizzare esattamente uno tra Dare e Avere con importo positivo."
            )
        try:
            account_id = int(line["account_id"])
        except (TypeError, ValueError):
            raise UnbalancedEntryError(f"Riga {idx}: conto non valido.")
        center_id = line.get("cost_center_id") or None
        if center_id is not None:
            try:
                center_id = int(center_id)
            except (TypeError, ValueError):
                raise UnbalancedEntryError(f"Riga {idx}: centro di costo non valido.")
            center_ids.add(center_id)
        account_ids.add(account_id)
        normalized.append({
            "account_id": account_id,
            "dare": dare,
            "avere": avere,
            "description": line.get("description"),
            "cost_center_id": center_id,
        })

    accounts = {a.id: a for a in Account.query.filter(Account.id.in_(account_ids)).all()}
    missing = account_ids - set(accounts)
    if missing:
        raise UnbalancedEntryError(f"Conti inesistenti: {', '.join(map(str, sorted(missing)))}.")
    inactive = [a.code for a in accounts.values() if not a.active]
    if inactive and not allow_inactive_accounts:
        raise UnbalancedEntryError(f"Conti non attivi: {', '.join(sorted(inactive))}.")

    if center_ids:
        centers = {c.id: c for c in CostCenter.query.filter(CostCenter.id.in_(center_ids)).all()}
        missing_centers = center_ids - set(centers)
        if missing_centers:
            raise UnbalancedEntryError(
                f"Centri di costo inesistenti: {', '.join(map(str, sorted(missing_centers)))}."
            )
        inactive_centers = [c.code for c in centers.values() if not c.active]
        if inactive_centers and not allow_inactive_accounts:
            raise UnbalancedEntryError(
                f"Centri di costo non attivi: {', '.join(sorted(inactive_centers))}."
            )

    if economic_subject_id is not None and db.session.get(EconomicSubject, economic_subject_id) is None:
        raise UnbalancedEntryError("Soggetto economico inesistente.")

    total_dare = sum((l["dare"] for l in normalized), Decimal("0"))
    total_avere = sum((l["avere"] for l in normalized), Decimal("0"))
    if total_dare <= 0:
        raise UnbalancedEntryError("Il totale della scrittura deve essere positivo.")
    if total_dare != total_avere:
        raise UnbalancedEntryError(
            f"Documento non bilanciato: Dare {total_dare:.2f} € vs Avere {total_avere:.2f} €."
        )

    normalized_gross = None if gross_amount is None else _money(gross_amount, "Totale documento")
    if vat_rate is not None:
        try:
            normalized_vat_rate = Decimal(str(vat_rate))
        except (InvalidOperation, ValueError, TypeError):
            raise UnbalancedEntryError("Aliquota IVA non valida.")
        if not normalized_vat_rate.is_finite():
            raise UnbalancedEntryError("Aliquota IVA non valida.")
    else:
        normalized_vat_rate = None

    doc_number = DocumentSequence.next_number(doc_type, prefix)
    entry = JournalEntry(
        doc_number=doc_number,
        doc_type=doc_type,
        doc_date=doc_date or date.today(),
        posting_date=date.today(),
        description=description,
        source_module=source_module,
        reference=reference,
        created_by_id=created_by_id,
        economic_subject_id=economic_subject_id,
        gross_amount=normalized_gross,
        vat_rate=normalized_vat_rate,
        natura=natura,
    )
    db.session.add(entry)
    db.session.flush()

    for line in normalized:
        db.session.add(JournalLine(entry_id=entry.id, **line))

    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return entry


def _reverse_gl_only(original, created_by_id=None):
    """Contro-movimento GL puro (righe invertite, stessa logica di sempre) —
    SENZA il controllo 'documento operativo, storno vietato da qui'. Usata
    da reverse_journal_entry (Prima Nota) e dai domain-reversal dedicati
    di Fase 4 (services/reversals.py) che gestiscono ANCHE quantità/stato
    del modulo sorgente nella stessa transazione."""
    reversed_lines = [{
        "account_id": line.account_id,
        "dare": line.avere,
        "avere": line.dare,
        "description": f"STORNO — {line.description or ''}",
        "cost_center_id": line.cost_center_id,
    } for line in original.lines]

    sequence = DocumentSequence.query.filter_by(doc_type=original.doc_type).first()
    if sequence is None:
        raise ValueError(f"Sequenza documentale {original.doc_type} non trovata.")

    new_entry = post_journal_entry(
        doc_type=original.doc_type,
        prefix=sequence.prefix,
        doc_date=date.today(),
        description=f"Storno di {original.doc_number}",
        lines=reversed_lines,
        source_module=original.source_module,
        reference=original.doc_number,
        created_by_id=created_by_id,
        commit=False,
        allow_inactive_accounts=True,
    )
    original.is_reversed = True
    original.reversed_by_id = new_entry.id
    new_entry.reverses_id = original.id
    return new_entry


def reverse_journal_entry(entry_id, created_by_id=None):
    """Storna un documento con una nuova scrittura opposta, in modo atomico."""
    original = db.session.get(JournalEntry, entry_id)
    if original is None:
        raise ValueError("Documento contabile non trovato.")
    if original.is_reversed:
        raise ValueError("Questo documento è già stato stornato.")
    if original.reverses_id is not None:
        raise ValueError("Non è possibile stornare a sua volta un documento di storno.")
    # I documenti operativi modificano anche quantità/stati nei moduli sorgente.
    # Senza uno storno di dominio dedicato, il solo contro-movimento GL li
    # renderebbe incoerenti; quindi non è consentito dalla Prima Nota.
    # Fase 4: per MAGAZZINO/VENDITE lo storno di dominio ORA ESISTE — vedi
    # services/reversals.py (reverse_goods_receipt, reverse_delivery) — ma
    # resta volutamente non richiamabile da qui: la Prima Nota non conosce
    # le regole di dominio (giacenza, three-way match) per farlo in sicurezza.
    if original.source_module != "LEDGER" or original.doc_type in ("Cespiti", "AF"):
        raise ValueError(
            "Documento generato da un modulo operativo: lo storno deve essere eseguito "
            "dal flusso sorgente per mantenere coerenti contabilità e stato operativo."
        )
    if original.doc_type in ("KR", "DR", "DG") and original.is_paid:
        raise ValueError("Prima di stornare il documento occorre stornarne il pagamento/incasso.")

    try:
        new_entry = _reverse_gl_only(original, created_by_id=created_by_id)
        # Lo storno del pagamento/incasso riapre atomically tutte le partite
        # che quel movimento aveva chiuso.
        if original.doc_type in ("KZ", "DZ"):
            from models import PaymentAllocation
            from services.payments import reverse_payment_allocations

            has_tracked_allocations = PaymentAllocation.query.filter_by(
                payment_entry_id=original.id
            ).first() is not None

            if has_tracked_allocations:
                # Pagamento granulare (Scadenzario, Fase 3): il ripristino
                # esatto lo fa reverse_payment_allocations, rata per rata,
                # in base a quanto QUESTO pagamento aveva davvero chiuso —
                # non un reset pieno, che sovrascriverebbe residui corretti
                # lasciati da altri pagamenti fatti prima o dopo.
                reverse_payment_allocations(original.id, created_by_id=created_by_id)
                settled = JournalEntry.query.filter_by(paid_by_entry_id=original.id).all()
                for invoice in settled:
                    invoice.is_paid = False
                    invoice.paid_by_entry_id = None
            else:
                # Pagamento a saldo pieno (supplier_payment/customer_payment,
                # nessuna allocazione tracciata): reset diretto delle rate
                # coinvolte al loro importo originario.
                settled = JournalEntry.query.filter_by(paid_by_entry_id=original.id).all()
                for invoice in settled:
                    invoice.is_paid = False
                    invoice.paid_by_entry_id = None
                    for inst in InvoiceInstallment.query.filter_by(entry_id=invoice.id).all():
                        inst.residual_amount = inst.amount
                        inst.version += 1
        db.session.commit()
        return new_entry
    except Exception:
        db.session.rollback()
        raise
