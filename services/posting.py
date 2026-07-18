"""
services/posting.py — Il "postFI" reale, lato server.

Ogni Blueprint (GL, AP, AR, Cespiti) che deve registrare una scrittura
contabile passa da QUI, non scrive mai direttamente JournalEntry/JournalLine
a mano — così la validazione (Dare = Avere) e la numerazione progressiva
sono garantite in un unico punto, senza doppioni di logica.
"""
from datetime import date
from extensions import db
from models import JournalEntry, JournalLine, DocumentSequence


class UnbalancedEntryError(Exception):
    """Sollevata quando una scrittura non torna in Dare = Avere."""
    pass


def post_journal_entry(doc_type, prefix, doc_date, description, lines, source_module="LEDGER",
                        reference=None, created_by_id=None, economic_subject_id=None,
                        gross_amount=None, vat_rate=None, natura=None):
    """
    Crea e salva un documento contabile in partita doppia.

    lines: lista di dict, ognuno con:
        {"account_id": int, "dare": Decimal|float, "avere": Decimal|float,
         "description": str (opz.), "cost_center_id": int|None (opz.)}

    Solleva UnbalancedEntryError se Dare != Avere (nessuna scrittura a metà
    viene mai salvata: o passa tutta, o niente — via rollback automatico).
    """
    total_dare = sum(float(l.get("dare", 0) or 0) for l in lines)
    total_avere = sum(float(l.get("avere", 0) or 0) for l in lines)

    if abs(total_dare - total_avere) > 0.01:
        raise UnbalancedEntryError(
            f"Documento non bilanciato: Dare {total_dare:.2f} € vs Avere {total_avere:.2f} €."
        )

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
        gross_amount=gross_amount,
        vat_rate=vat_rate,
        natura=natura,
    )
    db.session.add(entry)
    db.session.flush()  # serve l'id per collegare le righe

    for line in lines:
        db.session.add(JournalLine(
            entry_id=entry.id,
            account_id=line["account_id"],
            dare=line.get("dare", 0) or 0,
            avere=line.get("avere", 0) or 0,
            description=line.get("description"),
            cost_center_id=line.get("cost_center_id"),
        ))

    db.session.commit()
    return entry


def reverse_journal_entry(entry_id, created_by_id=None):
    """
    Storna un documento esistente: crea un NUOVO documento con le righe
    invertite (Dare<->Avere), collegato all'originale. L'originale non
    viene mai toccato — resta storicamente intatto (immutabilità).
    """
    original = JournalEntry.query.get_or_404(entry_id)
    if original.is_reversed:
        raise ValueError("Questo documento è già stato stornato.")

    reversed_lines = [
        {
            "account_id": l.account_id,
            "dare": l.avere,
            "avere": l.dare,
            "description": f"STORNO — {l.description or ''}",
            "cost_center_id": l.cost_center_id,
        }
        for l in original.lines
    ]

    new_entry = post_journal_entry(
        doc_type=original.doc_type,
        prefix=DocumentSequence.query.filter_by(doc_type=original.doc_type).first().prefix,
        doc_date=date.today(),
        description=f"Storno di {original.doc_number}",
        lines=reversed_lines,
        source_module=original.source_module,
        reference=original.doc_number,
        created_by_id=created_by_id,
    )

    original.is_reversed = True
    original.reversed_by_id = new_entry.id
    new_entry.reverses_id = original.id
    db.session.commit()
    return new_entry
