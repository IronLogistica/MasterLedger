from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from extensions import db
from models import OperatingSite, WarehouseArea, Account

warehouse_bp = Blueprint("warehouse", __name__, template_folder="../../templates/warehouse")

# Mappa tipo-area -> codice conto di magazzino predefinito (creato dal seed).
# È la stessa logica di determinazione conti già vista in magazzino, qui come
# vera anagrafica organizzativa Contabilità: ogni area di stoccaggio ha un preciso
# conto G/L, per studiare (e verificare) il corretto stoccaggio per tipo.
DEFAULT_ACCOUNT_BY_TYPE = {
    "ROH":   "150000",  # Magazzino Materie Prime e Merci
    "FERT":  "160000",  # Magazzino Prodotti Finiti
    "HALB":  "155000",  # Magazzino Semilavorati
    "QUAL":  "152000",  # Magazzino Blocco Qualità
    "SCRAP": "590000",  # Perdite su Magazzino (Scarti)
    "TRANS": None,       # Area di transito — nessun conto proprio
}


@warehouse_bp.route("/")
@login_required
def setup():
    sites = OperatingSite.query.filter_by(active=True).order_by(OperatingSite.code).all()
    return render_template("warehouse/setup.html", sites=sites, area_types=WarehouseArea.AREA_TYPES)


@warehouse_bp.route("/sites/new", methods=["POST"])
@login_required
def site_new():
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    city = request.form.get("city", "").strip()
    region = request.form.get("region", "").strip()

    if not code or not name:
        flash("Codice e nome della sede operativa sono obbligatori.", "danger")
        return redirect(url_for("warehouse.setup"))

    if OperatingSite.query.filter_by(code=code).first():
        flash(f"La sede operativa {code} esiste già.", "danger")
        return redirect(url_for("warehouse.setup"))

    site = OperatingSite(code=code, name=name, city=city, region=region)
    db.session.add(site)
    db.session.commit()
    flash(f"Sede operativa {code} — {name} creata.", "success")
    return redirect(url_for("warehouse.setup"))


@warehouse_bp.route("/sites/<int:site_id>/delete", methods=["POST"])
@login_required
def site_delete(site_id):
    site = OperatingSite.query.get_or_404(site_id)
    if site.warehouse_areas:
        flash("Impossibile eliminare: la sede operativa ha aree di magazzino assegnate. Rimuovile prima.", "danger")
        return redirect(url_for("warehouse.setup"))
    db.session.delete(site)
    db.session.commit()
    flash(f"Sede operativa {site.code} eliminata.", "info")
    return redirect(url_for("warehouse.setup"))


@warehouse_bp.route("/areas/new", methods=["POST"])
@login_required
def area_new():
    site_id = request.form.get("site_id", type=int)
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    area_type = request.form.get("area_type", "ROH")

    if not site_id or not code:
        flash("Sede operativa e codice area di magazzino sono obbligatori.", "danger")
        return redirect(url_for("warehouse.setup"))

    if WarehouseArea.query.filter_by(site_id=site_id, code=code).first():
        flash(f"L'ubicazione {code} esiste già su questa sede operativa.", "danger")
        return redirect(url_for("warehouse.setup"))

    account_code = DEFAULT_ACCOUNT_BY_TYPE.get(area_type)
    account = Account.query.filter_by(code=account_code).first() if account_code else None

    sloc = WarehouseArea(
        site_id=site_id, code=code, name=name or f"Ubicazione {code}",
        area_type=area_type, account_id=account.id if account else None,
    )
    db.session.add(sloc)
    db.session.commit()

    account_desc = f"{account.code} — {account.name}" if account else "nessuno (area di transito)"
    flash(f"Area di magazzino {code} creata — collegata al conto {account_desc}.", "success")
    return redirect(url_for("warehouse.setup"))


@warehouse_bp.route("/areas/<int:sloc_id>/delete", methods=["POST"])
@login_required
def area_delete(sloc_id):
    sloc = WarehouseArea.query.get_or_404(sloc_id)
    db.session.delete(sloc)
    db.session.commit()
    flash("Area di magazzino eliminata.", "info")
    return redirect(url_for("warehouse.setup"))
