"""Riconciliazione delle quantità fatturate del ciclo MM.

``PurchaseOrderLine.qty_invoiced`` è un contatore derivato e può essere rimasto
stale dopo test, cancellazioni storiche o interventi manuali. La fonte
autorevole, per le Verifiche Fattura create dalla versione tracciata del
modulo, è ``InvoiceVerificationLine`` collegata a un KR MM ancora attivo.
"""
from decimal import Decimal

from sqlalchemy import func, or_

from extensions import db
from models import InvoiceVerificationLine, JournalEntry, PurchaseOrderLine


def lock_po_lines(po_id):
    """Serializza le operazioni concorrenti sulle righe dello stesso ordine.

    PostgreSQL applica il lock fino a commit/rollback; SQLite lo ignora, come
    previsto per i soli test in memoria.
    """
    return (
        PurchaseOrderLine.query.filter_by(po_id=po_id)
        .order_by(PurchaseOrderLine.id)
        .with_for_update()
        .all()
    )


def actual_invoiced_qty(po_line_id):
    """Quantità realmente coperta da Verifiche Fattura MM attive e tracciate."""
    value = (
        db.session.query(func.coalesce(func.sum(InvoiceVerificationLine.qty), 0))
        .join(JournalEntry, JournalEntry.id == InvoiceVerificationLine.entry_id)
        .filter(
            InvoiceVerificationLine.po_line_id == po_line_id,
            JournalEntry.source_module == "ACQUISTI",
            JournalEntry.doc_type == "KR",
            func.coalesce(JournalEntry.is_reversed, False).is_(False),
            JournalEntry.reverses_id.is_(None),
        )
        .scalar()
    )
    return Decimal(str(value or 0))


def reconcile_invoiced_qty(po_line):
    """Ricalcola e riallinea il contatore cache della riga ordine."""
    actual = actual_invoiced_qty(po_line.id)
    po_line.qty_invoiced = actual
    return actual


def has_tracked_active_invoice(po_id):
    """Indica se l'ordine ha almeno una Verifica Fattura MM attiva tracciata."""
    return (
        db.session.query(InvoiceVerificationLine.id)
        .join(JournalEntry, JournalEntry.id == InvoiceVerificationLine.entry_id)
        .join(PurchaseOrderLine, PurchaseOrderLine.id == InvoiceVerificationLine.po_line_id)
        .filter(
            PurchaseOrderLine.po_id == po_id,
            JournalEntry.source_module == "ACQUISTI",
            JournalEntry.doc_type == "KR",
            func.coalesce(JournalEntry.is_reversed, False).is_(False),
            JournalEntry.reverses_id.is_(None),
        )
        .first()
        is not None
    )


def has_untracked_active_invoice(po):
    """Rileva KR MM attivi riferiti all'OA ma privi di righe di tracciamento.

    Per questi documenti legacy non è possibile ricostruire in sicurezza la
    quantità per singola riga: vengono quindi segnalati al chiamante, che deve
    bloccare l'operazione anziché assumere arbitrariamente quantità zero.
    """
    marker = f"su OA {po.doc_number}"
    return (
        db.session.query(JournalEntry.id)
        .outerjoin(InvoiceVerificationLine, InvoiceVerificationLine.entry_id == JournalEntry.id)
        .filter(
            JournalEntry.source_module == "ACQUISTI",
            JournalEntry.doc_type == "KR",
            func.coalesce(JournalEntry.is_reversed, False).is_(False),
            JournalEntry.reverses_id.is_(None),
            or_(
                JournalEntry.reference == po.doc_number,
                JournalEntry.description.contains(marker, autoescape=True),
            ),
            InvoiceVerificationLine.id.is_(None),
        )
        .first()
        is not None
    )
