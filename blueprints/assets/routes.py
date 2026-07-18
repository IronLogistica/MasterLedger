from datetime import datetime, date
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from extensions import db
from models import Account, Asset, FiscalParameter
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
        value = request.form.get("value", type=float) or 0
        vat_rate = request.form.get("vat_rate", type=float) or 22
        useful_life = request.form.get("useful_life", type=int) or 10
        acq_date_str = request.form.get("acquisition_date")

        if not description or not value:
            flash("Descrizione e valore sono obbligatori.", "danger")
            return render_template("assets/asset_create.html", default_rate=default_rate_impianti)

        vat = round(value * vat_rate / 100, 2)
        acq_date = datetime.strptime(acq_date_str, "%Y-%m-%d").date() if acq_date_str else date.today()

        asset_code = f"A-{Asset.query.count() + 1:05d}"
        asset = Asset(
            code=asset_code, description=description, acquisition_value=value,
            acquisition_date=acq_date, asset_category=category, useful_life_years=useful_life,
        )
        db.session.add(asset)
        db.session.flush()

        try:
            asset_account = _get_account_by_code("200000")  # Cespiti
            vat_account = _get_account_by_code("154000")    # IVA a Credito
            ap_account = _get_account_by_code("210000")     # Debiti v/Fornitori

            lines = [
                {"account_id": asset_account.id, "dare": value, "avere": 0},
                {"account_id": vat_account.id, "dare": vat, "avere": 0},
                {"account_id": ap_account.id, "dare": 0, "avere": value + vat},
            ]
            entry = post_journal_entry(
                doc_type="Cespiti", prefix="20",
                doc_date=acq_date, description=f"Capitalizzazione Cespite {asset_code} — {description}",
                lines=lines, source_module="LEDGER", reference=asset_code, created_by_id=current_user.id,
            )
            flash(f"Cespite {asset_code} capitalizzato. Doc. {entry.doc_number} — Valore {value:.2f} €.", "success")
            return redirect(url_for("assets.asset_list"))
        except (UnbalancedEntryError, ValueError) as e:
            flash(str(e), "danger")

    return render_template("assets/asset_create.html", default_rate=default_rate_impianti)


@assets_bp.route("/depreciation", methods=["GET", "POST"])
@login_required
def depreciation():
    """Ammortamenti — Ammortamento periodico su tutti i cespiti attivi."""
    assets = Asset.query.filter_by(active=True).all()

    if request.method == "POST":
        period = request.form.get("period", "12")
        year = request.form.get("year", str(date.today().year))

        total_dep = 0
        for a in assets:
            annual_dep = float(a.acquisition_value) / max(a.useful_life_years, 1)
            total_dep += annual_dep
            a.accumulated_depreciation = float(a.accumulated_depreciation or 0) + annual_dep

        if total_dep <= 0:
            flash("Nessun cespite su cui calcolare l'ammortamento.", "warning")
            return redirect(url_for("assets.depreciation"))

        try:
            dep_account = _get_account_by_code("520000")   # Ammortamenti (costo)
            fund_account = _get_account_by_code("018000")  # Fondo Ammortamento

            lines = [
                {"account_id": dep_account.id, "dare": total_dep, "avere": 0},
                {"account_id": fund_account.id, "dare": 0, "avere": total_dep},
            ]
            entry = post_journal_entry(
                doc_type="AF", prefix="21",
                doc_date=None, description=f"Ammortamento periodico — Periodo {period}/{year}",
                lines=lines, source_module="LEDGER", reference=f"{period}/{year}", created_by_id=current_user.id,
            )
            db.session.commit()
            flash(f"Ammortamenti completato. Doc. {entry.doc_number} — Ammortamento totale {total_dep:.2f} € su {len(assets)} cespiti.", "success")
            return redirect(url_for("gl.entry_detail", entry_id=entry.id))
        except (UnbalancedEntryError, ValueError) as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("assets/depreciation.html", assets=assets)
