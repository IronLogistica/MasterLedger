"""
services/payments.py — Fase 3 (progettazione parti mancanti, punto 1):
pagamenti parziali, scadenze e residui.

Tutte le operazioni su rate/allocazioni passano da qui, mai a mano nei
blueprint — stesso principio già applicato a services/posting.py per le
scritture contabili.

INVARIANTE DI QUADRATURA (garantita nella stessa transazione, per ogni
operazione di allocazione):

    importo_pagamento (denaro reale, gross_amount del KZ/DZ)
        = somma(cash_amount allocato alle rate)
        + importo_non_allocato (anticipo/residuo in attesa di abbinamento)

Gli abbuoni (abbuono_amount) riducono il residuo della rata ESATTAMENTE
come il cash_amount, ma non fanno parte del denaro che si muove sul conto
banca: generano una riga contabile aggiuntiva sul conto abbuoni
autorizzato (AccountMapping: 'abbuoni_attivi' per i clienti, 'abbuoni_passivi'
per i fornitori), aggiunta alla STESSA scrittura del pagamento.
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from extensions import db
from models import InvoiceInstallment, PaymentAllocation, JournalLine, AccountMapping


CENT = Decimal("0.01")


class PaymentAllocationError(ValueError):
    """Violazione di un vincolo di allocazione: importo negativo, superiore
    al residuo, rata inesistente/di un altro soggetto, concorrenza persa."""


def _money(value, label):
    try:
        amount = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError, TypeError):
        raise PaymentAllocationError(f"{label}: importo non valido.")
    if not amount.is_finite():
        raise PaymentAllocationError(f"{label}: l'importo deve essere finito.")
    return amount.quantize(CENT)


def create_installments_for_invoice(entry, schedule=None):
    """Crea le rate di una fattura appena registrata (KR o DR).

    schedule: lista opzionale di (giorni_da_oggi, importo) per un piano a
    più scadenze. Se assente, crea UNA rata unica che copre l'intero
    gross_amount con scadenza a 30 giorni dalla data documento — stesso
    comportamento "paga tutto insieme" di oggi, nessuna sorpresa per chi
    non usa ancora i pagamenti parziali.

    Va chiamata nella STESSA transazione di post_journal_entry (prima del
    commit finale), per garantire che una fattura non resti mai senza rate.
    """
    gross = _money(entry.gross_amount, "Totale fattura")
    if gross <= 0:
        raise PaymentAllocationError("Impossibile generare rate per un documento con importo non positivo.")

    if schedule:
        total_schedule = sum(_money(amt, "Importo rata") for _, amt in schedule)
        if total_schedule != gross:
            raise PaymentAllocationError(
                f"Il piano rate ({total_schedule:.2f} €) non corrisponde al totale fattura ({gross:.2f} €)."
            )
        rows = schedule
    else:
        rows = [(30, gross)]

    installments = []
    for idx, (days_offset, amount) in enumerate(rows, start=1):
        due = (entry.doc_date or date.today()) + timedelta(days=days_offset)
        inst = InvoiceInstallment(
            entry_id=entry.id, numero_rata=idx, due_date=due,
            amount=_money(amount, f"Rata {idx}"), residual_amount=_money(amount, f"Rata {idx}"),
            version=0,
        )
        db.session.add(inst)
        installments.append(inst)
    db.session.flush()
    return installments


def allocate_payment(payment_entry, allocations, created_by_id=None):
    """Alloca un pagamento/incasso GIÀ CREATO E GIÀ BILANCIATO (comprese le
    eventuali righe di abbuono, che il chiamante deve aver già inserito
    nella scrittura PRIMA di chiamare questa funzione — altrimenti il
    controllo di quadratura di post_journal_entry non le vedrebbe mai:
    quel controllo gira sulle righe iniziali, non su righe aggiunte dopo).

    Questa funzione si occupa SOLO di: validare i vincoli sulle rate,
    aggiornarne il residuo con lock di concorrenza, creare le righe di
    allocazione (PaymentAllocation) e sincronizzare is_paid — non tocca
    mai JournalLine.

    allocations: lista di dict {"installment_id": int, "cash_amount": Decimal,
                                 "abbuono_amount": Decimal (opz., default 0)}

    Verifica, per OGNI rata coinvolta, con lock ottimistico (version):
      - la rata esiste e appartiene allo stesso soggetto del pagamento;
      - cash_amount e abbuono_amount non sono negativi;
      - cash_amount + abbuono_amount <= residual_amount al momento del lock
        (non al momento in cui il form è stato aperto — protegge da due
        operatori che saldano la stessa rata in contemporanea).

    Poi verifica l'INVARIANTE complessiva: la somma dei cash_amount di
    questa chiamata non può superare il gross_amount (denaro REALE, non
    include gli abbuoni) del pagamento meno quanto già allocato in cash da
    eventuali chiamate precedenti sullo stesso payment_entry.

    Ritorna (totale_cash_allocato, totale_abbuoni, importo_non_allocato).
    """
    if not allocations:
        raise PaymentAllocationError("Nessuna rata selezionata per l'allocazione.")

    gross = _money(payment_entry.gross_amount, "Importo pagamento")

    # Quanto cash è già stato allocato in precedenza su QUESTO pagamento
    # (permette chiamate multiple sullo stesso documento senza mai sforare).
    already_allocated_cash = db.session.query(db.func.coalesce(db.func.sum(PaymentAllocation.cash_amount), 0)) \
        .filter(PaymentAllocation.payment_entry_id == payment_entry.id,
                PaymentAllocation.reversed.is_(False)).scalar()
    already_allocated_cash = _money(already_allocated_cash, "Cash già allocato")

    total_cash_this_call = Decimal("0")
    total_abbuono_this_call = Decimal("0")
    new_allocations = []

    for row in allocations:
        installment_id = row.get("installment_id")
        cash_amount = _money(row.get("cash_amount", 0), "Cash allocato")
        abbuono_amount = _money(row.get("abbuono_amount", 0), "Abbuono")

        if cash_amount < 0 or abbuono_amount < 0:
            raise PaymentAllocationError("Gli importi allocati non possono essere negativi.")
        if cash_amount == 0 and abbuono_amount == 0:
            continue  # riga vuota nel form, si ignora silenziosamente

        # Lock ottimistico: rilegge la rata FRESCA dal DB (non da un oggetto
        # tenuto in mano da prima) e la blocca per la durata della transazione,
        # così un secondo operatore che prova ad allocare la stessa rata nello
        # stesso istante attende invece di leggere un residuo già stantio.
        inst = InvoiceInstallment.query.filter_by(id=installment_id).with_for_update().first()
        if inst is None:
            raise PaymentAllocationError(f"Rata #{installment_id} inesistente.")
        if inst.entry.economic_subject_id != payment_entry.economic_subject_id:
            raise PaymentAllocationError(
                f"La rata #{installment_id} appartiene a un soggetto diverso da quello del pagamento."
            )
        residual = _money(inst.residual_amount, "Residuo rata")
        richiesto = cash_amount + abbuono_amount
        if richiesto > residual:
            raise PaymentAllocationError(
                f"Rata {inst.numero_rata} di {inst.entry.doc_number}: richiesti {richiesto:.2f} € "
                f"ma il residuo è {residual:.2f} € — un altro pagamento potrebbe averla già ridotta."
            )

        inst.residual_amount = residual - richiesto
        inst.version += 1
        if inst.residual_amount <= 0:
            inst.entry.is_paid = True
            inst.entry.paid_by_entry_id = payment_entry.id
        db.session.add(inst)

        alloc = PaymentAllocation(
            payment_entry_id=payment_entry.id, installment_id=inst.id,
            cash_amount=cash_amount, abbuono_amount=abbuono_amount,
        )
        db.session.add(alloc)
        new_allocations.append(alloc)

        total_cash_this_call += cash_amount
        total_abbuono_this_call += abbuono_amount

    if already_allocated_cash + total_cash_this_call > gross:
        raise PaymentAllocationError(
            f"Il totale allocato in contanti ({already_allocated_cash + total_cash_this_call:.2f} €) "
            f"supera l'importo del pagamento ({gross:.2f} €)."
        )

    unallocated = gross - (already_allocated_cash + total_cash_this_call)
    db.session.flush()
    return total_cash_this_call, total_abbuono_this_call, unallocated


def reverse_payment_allocations(payment_entry_id, created_by_id=None):
    """Storna tutte le allocazioni (non ancora stornate) di un pagamento:
    ripristina il residuo di ogni rata coinvolta ESATTAMENTE della quota
    che QUESTA allocazione aveva chiuso — senza toccare allocazioni di
    ALTRI pagamenti successivi sulla stessa rata, che restano intatte.
    """
    allocations = PaymentAllocation.query.filter_by(
        payment_entry_id=payment_entry_id, reversed=False
    ).all()
    for alloc in allocations:
        inst = InvoiceInstallment.query.filter_by(id=alloc.installment_id).with_for_update().first()
        if inst is None:
            continue
        restored = _money(alloc.cash_amount, "cash") + _money(alloc.abbuono_amount, "abbuono")
        # Il residuo non può mai superare l'importo originario della rata,
        # anche se per qualche motivo il ripristino lo spingerebbe oltre.
        inst.residual_amount = min(inst.amount, _money(inst.residual_amount, "residuo") + restored)
        inst.version += 1
        if inst.residual_amount > 0 and inst.entry.paid_by_entry_id == payment_entry_id:
            inst.entry.is_paid = False
            inst.entry.paid_by_entry_id = None
        alloc.reversed = True
        alloc.reversed_at = db.func.now()
        alloc.reversed_by_id = created_by_id
        db.session.add(inst)
        db.session.add(alloc)
    db.session.flush()
    return len(allocations)
