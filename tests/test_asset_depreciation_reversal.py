"""Copre il gap trovato in audit: prima di services/reversals.reverse_depreciation
un ammortamento contabilizzato con periodo o importo sbagliato non poteva
essere corretto in alcun modo (post_journal_entry esclude esplicitamente
"AF"/"Cespiti" dallo storno generico da Prima Nota, e non esisteva un
percorso di storno di dominio dedicato)."""
from datetime import date
from decimal import Decimal

import pytest

from extensions import db
from models import Account, Asset, AssetDepreciationLine, JournalEntry
from services.reversals import reverse_depreciation, ReversalError


def _make_asset(app, code="CESP-01", value="12000.00", years=10, acquired=date(2025, 1, 1)):
    with app.app_context():
        a = Asset(code=code, description="Cespite di test", acquisition_value=Decimal(value),
                  acquisition_date=acquired, useful_life_years=years, accumulated_depreciation=Decimal("0"))
        db.session.add(a)
        db.session.commit()
        return a.id


def _post_depreciation(client, period, year):
    return client.post("/assets/depreciation", data={"period": str(period), "year": str(year)},
                       follow_redirects=True)


def test_depreciation_reversal_restores_asset_and_gl(app, client, login):
    asset_id = _make_asset(app)

    resp = _post_depreciation(client, 6, 2026)
    assert resp.status_code == 200

    with app.app_context():
        asset = db.session.get(Asset, asset_id)
        # 12000 / (10*12) = 100.00 al mese
        assert asset.accumulated_depreciation == Decimal("100.00")
        entry = JournalEntry.query.filter_by(doc_type="AF", reference="06/2026").one()
        dep_lines = AssetDepreciationLine.query.filter_by(entry_id=entry.id).all()
        assert len(dep_lines) == 1
        assert dep_lines[0].asset_id == asset_id
        assert dep_lines[0].amount == Decimal("100.00")

        new_entry = reverse_depreciation(entry.id, "Periodo sbagliato, da rifare", created_by_id=1)
        db.session.commit()

        db.session.refresh(asset)
        assert asset.accumulated_depreciation == Decimal("0.00")

        original = db.session.get(JournalEntry, entry.id)
        assert original.is_reversed is True
        assert original.reversed_by_id == new_entry.id
        assert new_entry.reverses_id == original.id
        assert new_entry.total_dare == original.total_avere
        assert new_entry.total_avere == original.total_dare


def test_cannot_reverse_depreciation_twice(app, client, login):
    _make_asset(app)
    _post_depreciation(client, 6, 2026)

    with app.app_context():
        entry = JournalEntry.query.filter_by(doc_type="AF", reference="06/2026").one()
        reverse_depreciation(entry.id, "primo storno", created_by_id=1)
        db.session.commit()
        with pytest.raises(ReversalError, match="già stato stornato"):
            reverse_depreciation(entry.id, "secondo tentativo", created_by_id=1)


def test_cannot_reverse_older_run_while_newer_untouched(app, client, login):
    """Se un cespite ha già un ammortamento SUCCESSIVO non stornato, non si
    può stornare quello più vecchio senza prima stornare quello nuovo —
    altrimenti l'accumulato resterebbe incoerente con quanto già calcolato
    sopra di esso."""
    asset_id = _make_asset(app)
    _post_depreciation(client, 6, 2026)
    _post_depreciation(client, 7, 2026)

    with app.app_context():
        june_entry = JournalEntry.query.filter_by(doc_type="AF", reference="06/2026").one()
        with pytest.raises(ReversalError, match="CESP-01"):
            reverse_depreciation(june_entry.id, "voglio stornare solo giugno", created_by_id=1)

        # Ma stornare il più recente (luglio) resta permesso...
        july_entry = JournalEntry.query.filter_by(doc_type="AF", reference="07/2026").one()
        reverse_depreciation(july_entry.id, "storno luglio", created_by_id=1)
        db.session.commit()

        # ...e ORA anche giugno si può stornare senza conflitti.
        reverse_depreciation(june_entry.id, "ora si può", created_by_id=1)
        db.session.commit()

        asset = db.session.get(Asset, asset_id)
        assert asset.accumulated_depreciation == Decimal("0.00")


def test_reverse_depreciation_rejects_non_af_entry(app, client, login):
    from services.posting import post_journal_entry
    with app.app_context():
        dare_acc = Account.query.filter_by(code="410000").first()
        avere_acc = Account.query.filter_by(code="180000").first()
        entry = post_journal_entry(
            doc_type="SA", prefix="00", doc_date=date(2026, 6, 1), description="Scrittura manuale",
            lines=[{"account_id": dare_acc.id, "dare": 50, "avere": 0},
                   {"account_id": avere_acc.id, "dare": 0, "avere": 50}],
        )
        with pytest.raises(ReversalError, match="non è un ammortamento"):
            reverse_depreciation(entry.id, "motivo", created_by_id=1)


def test_reverse_depreciation_requires_reason(app, client, login):
    _make_asset(app)
    _post_depreciation(client, 6, 2026)
    with app.app_context():
        entry = JournalEntry.query.filter_by(doc_type="AF", reference="06/2026").one()
        with pytest.raises(ReversalError, match="obbligatorio"):
            reverse_depreciation(entry.id, "   ", created_by_id=1)
