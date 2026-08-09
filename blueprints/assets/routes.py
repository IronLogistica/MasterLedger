from datetime import datetime, date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from calendar import monthrange
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from extensions import db
from models import Account, AccountMapping, Asset, FiscalParameter, JournalEntry
from services.posting import post_journal_entry, UnbalancedEntryError

assets_bp = Blueprint("assets", __name__, template_folder="../../templates/assets")


def _get_account_by_code(code):
    acc = Account.query.filter_by(code=code).first()
    if acc is None:
        raise ValueError(f"Conto {code} non trovato. Esegui 'flask seed' prima di continuare.")
    return acc


def _fiscal_param(key, default=None):
    p = FiscalParameter.query.filter_by(key=key).first()
    return p.value if p else default


@assets_bp.route("/")
@login_required
def asset_list():
    assets = Asset.query.filter_by(active=True).order_by(Asset.code).all()
    return render_template("assets/asset_list.html", assets=assets)


@assets_bp.route("/asset_create", methods=["GET", "POST"])
@login_required
def asset_create():
    """Capitalizzazione cespite — Creazione anagrafica cespite e capitalizzazione."""
    # L'aliquota di default proposta viene dal pannello del Commercialista —
    # l'operatore la vede precompilata ma può cambiarla per il singolo cespite
    # se il Commercialista ha indicato un caso diverso (giudizio professionale).
    default_rate_impianti = _fiscal_param("ammortamento_aliquota_impianti", "10")

    if request.method == "POST":
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "Impianti e Macchinari")
        try:
            value = Decimal(request.form.get("value", "0").replace(",", "."))
            vat_rate = Decimal(request.form.get("vat_rate", "22").replace(",", "."))
            useful_life = int(request.form.get("useful_life", "10"))
            acq_date_str = request.form.get("acquisition_date")
            acq_date = datetime.strptime(acq_date_str, "%Y-%m-%d").date() if acq_date_str else date.today()
        except (InvalidOperation, ValueError, TypeError):
            flash("Valore, aliquota, vita utile o data non validi.", "danger")
            return render_template("assets/asset_create.html", default_rate=default_rate_impianti)

        if not description or not value.is_finite() or value <= 0:
            flash("Descrizione e valore positivo sono obbligatori.", "danger")
            return render_template("assets/asset_create.html", default_rate=default_rate_impianti)
        if not vat_rate.is_finite() or vat_rate < 0 or vat_rate > 100 or useful_life <= 0:
            flash("Aliquota IVA (0-100) e vita utile positiva non validi.", "danger")
            return render_template("assets/asset_create.html", default_rate=default_rate_impianti)

        value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        vat = (value * vat_rate / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        asset_code = f"A-{Asset.query.count() + 1:05d}"
        asset = Asset(
            code=asset_code, description=description, acquisition_value=value,
            acquisition_date=acq_date, asset_category=category, useful_life_years=useful_life,
        )
        db.session.add(asset)
        db.session.flush()

        try:
            asset_account = AccountMapping.get_or_error("cespiti_impianti")
            vat_account = AccountMapping.get_or_error("iva_credito")
            ap_account = AccountMapping.get_or_error("debiti_fornitori")

            lines = [
                {"account_id": asset_account.id, "dare": value, "avere": 0},
                {"account_id": vat_account.id, "dare": vat, "avere": 0},
                {"account_id": ap_account.id, "dare": 0, "avere": value + vat},
            ]
            entry = post_journal_entry(
                doc_type="Cespiti", prefix="20",
                doc_date=acq_date, description=f"Capitalizzazione Cespite {asset_code} — {description}",
                lines=lines, source_module="LEDGER", reference=asset_code, created_by_id=current_user.id,
                commit=False,
            )
            db.session.commit()
            flash(f"Cespite {asset_code} capitalizzato. Doc. {entry.doc_number} — Valore {value:.2f} €.", "success")
            return redirect(url_for("assets.asset_list"))
        except (UnbalancedEntryError, ValueError) as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("assets/asset_create.html", default_rate=default_rate_impianti)


@assets_bp.route("/depreciation", methods=["GET", "POST"])
@login_required
def depreciation():
    """Ammortamenti — Ammortamento periodico su tutti i cespiti attivi."""
    assets = Asset.query.filter_by(active=True).all()

    if request.method == "POST":
        try:
            period = int(request.form.get("period", "12"))
            year = int(request.form.get("year", str(date.today().year)))
            if period < 1 or period > 12 or year < 1900 or year > 9999:
                raise ValueError
        except (TypeError, ValueError):
            flash("Periodo o esercizio non valido.", "danger")
            return redirect(url_for("assets.depreciation"))

        reference = f"{period:02d}/{year}"
        if JournalEntry.query.filter_by(doc_type="AF", reference=reference, is_reversed=False).first():
            flash(f"L'ammortamento del periodo {reference} è già stato contabilizzato.", "warning")
            return redirect(url_for("assets.depreciation"))

        period_end = date(year, period, monthrange(year, period)[1])
        total_dep = Decimal("0")
        affected = 0
        for a in assets:
            if a.acquisition_date and a.acquisition_date > period_end:
                continue
            value = Decimal(str(a.acquisition_value or 0))
            accumulated = Decimal(str(a.accumulated_depreciation or 0))
            remaining = max(Decimal("0"), value - accumulated)
            if remaining == 0:
                continue
            monthly_dep = (value / (max(a.useful_life_years, 1) * 12)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            dep = min(monthly_dep, remaining)
            if dep <= 0:
                continue
            total_dep += dep
            a.accumulated_depreciation = accumulated + dep
            affected += 1

        if total_dep <= 0:
            flash("Nessun cespite ammortizzabile nel periodo selezionato.", "warning")
            return redirect(url_for("assets.depreciation"))

        try:
            dep_account = AccountMapping.get_or_error("ammortamenti_costo")
            fund_account = AccountMapping.get_or_error("fondo_ammortamento")
            lines = [
                {"account_id": dep_account.id, "dare": total_dep, "avere": 0},
                {"account_id": fund_account.id, "dare": 0, "avere": total_dep},
            ]
            entry = post_journal_entry(
                doc_type="AF", prefix="21", doc_date=period_end,
                description=f"Ammortamento mensile — Periodo {reference}",
                lines=lines, source_module="LEDGER", reference=reference,
                created_by_id=current_user.id, commit=False,
            )
            db.session.commit()
            flash(f"Ammortamento {reference} completato. Doc. {entry.doc_number} — "
                  f"totale {total_dep:.2f} € su {affected} cespiti.", "success")
            return redirect(url_for("gl.entry_detail", entry_id=entry.id))
        except (UnbalancedEntryError, ValueError) as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("assets/depreciation.html", assets=assets, current_year=date.today().year)
