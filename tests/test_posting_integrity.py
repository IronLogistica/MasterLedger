from datetime import date
from decimal import Decimal
import pytest

from extensions import db
from models import Account, JournalEntry, JournalLine, EconomicSubject
from services.posting import post_journal_entry, reverse_journal_entry, UnbalancedEntryError


def test_balanced_entry_is_rounded_and_persisted(app, account):
    with app.app_context():
        a, b = account("180000"), account("310000")
        entry = post_journal_entry("SA", "10", date(2026, 1, 2), "test", [
            {"account_id": a.id, "dare": "10.004", "avere": 0},
            {"account_id": b.id, "dare": 0, "avere": "10.00"},
        ])
        assert entry.total_dare == entry.total_avere == Decimal("10.00")
        assert entry.is_balanced
        assert entry.doc_number == "1000000001"


def test_unbalanced_entry_is_rejected_without_partial_rows(app, account):
    with app.app_context():
        with pytest.raises(UnbalancedEntryError, match="non bilanciato"):
            post_journal_entry("SA", "10", None, "bad", [
                {"account_id": account("180000").id, "dare": 10, "avere": 0},
                {"account_id": account("310000").id, "dare": 0, "avere": 9},
            ])
        db.session.rollback()
        assert JournalEntry.query.count() == 0
        assert JournalLine.query.count() == 0


@pytest.mark.parametrize("bad_lines", [
    lambda a,b: [{"account_id": a.id, "dare": -1, "avere": 0}, {"account_id": b.id, "dare": 0, "avere": -1}],
    lambda a,b: [{"account_id": a.id, "dare": 1, "avere": 1}, {"account_id": b.id, "dare": 0, "avere": 2}],
    lambda a,b: [{"account_id": a.id, "dare": 0, "avere": 0}, {"account_id": b.id, "dare": 0, "avere": 0}],
    lambda a,b: [{"account_id": a.id, "dare": "NaN", "avere": 0}, {"account_id": b.id, "dare": 0, "avere": 1}],
])
def test_invalid_line_shapes_are_rejected(app, account, bad_lines):
    with app.app_context():
        with pytest.raises(UnbalancedEntryError):
            post_journal_entry("SA", "10", None, "bad", bad_lines(account("180000"), account("310000")))
        db.session.rollback()


def test_missing_or_inactive_accounts_and_subject_are_rejected(app, account):
    with app.app_context():
        a, b = account("180000"), account("310000")
        b.active = False; db.session.commit()
        with pytest.raises(UnbalancedEntryError, match="non attivi"):
            post_journal_entry("SA", "10", None, "bad", [
                {"account_id": a.id, "dare": 1, "avere": 0}, {"account_id": b.id, "dare": 0, "avere": 1}])
        db.session.rollback(); b.active = True; db.session.commit()
        with pytest.raises(UnbalancedEntryError, match="Soggetto"):
            post_journal_entry("SA", "10", None, "bad", [
                {"account_id": a.id, "dare": 1, "avere": 0}, {"account_id": b.id, "dare": 0, "avere": 1}],
                economic_subject_id=99999)


def test_reversal_swaps_lines_and_cannot_be_reversed_twice(app, account):
    with app.app_context():
        original = post_journal_entry("SA", "10", None, "manual", [
            {"account_id": account("180000").id, "dare": 12, "avere": 0},
            {"account_id": account("310000").id, "dare": 0, "avere": 12}], created_by_id=1)
        original_id = original.id
        reversal = reverse_journal_entry(original_id, created_by_id=1)
        assert original.is_reversed and original.reversed_by_id == reversal.id
        assert reversal.reverses_id == original.id
        assert [(x.dare, x.avere) for x in reversal.lines] == [(Decimal("0.00"), Decimal("12.00")), (Decimal("12.00"), Decimal("0.00"))]
        with pytest.raises(ValueError, match="già stato stornato"):
            reverse_journal_entry(original_id)


def test_operational_document_reversal_is_blocked(app, account):
    with app.app_context():
        e = post_journal_entry("GR", "50", None, "receipt", [
            {"account_id": account("180000").id, "dare": 1, "avere": 0},
            {"account_id": account("210000").id, "dare": 0, "avere": 1}], source_module="MAGAZZINO")
        with pytest.raises(ValueError, match="modulo operativo"):
            reverse_journal_entry(e.id)


def test_every_created_entry_remains_balanced(app, account):
    with app.app_context():
        for amount in ("0.01", "12.34", "999999.99"):
            post_journal_entry("SA", "10", None, "batch", [
                {"account_id": account("180000").id, "dare": amount, "avere": 0},
                {"account_id": account("310000").id, "dare": 0, "avere": amount}])
        assert all(e.total_dare == e.total_avere for e in JournalEntry.query.all())
