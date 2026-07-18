"""
blueprints/materials/routes.py — Anagrafica Articoli (Material Master, MM01).

Ogni articolo porta con sé le due informazioni che fanno funzionare i cicli:
  - costo standard  → usato dal PGI per il Costo del Venduto (SD) e come
                      prezzo proposto negli ordini d'acquisto (MM)
  - prezzo vendita  → proposto in preventivi/ordini cliente
"""
from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required

from extensions import db
from models import Material

materials_bp = Blueprint("materials", __name__, template_folder="../../templates/materials")


@materials_bp.route("/", methods=["GET", "POST"])
@login_required
def material_list():
    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        description = request.form.get("description", "").strip()
        if not code or not description:
            flash("Codice e descrizione sono obbligatori.", "danger")
        elif Material.query.filter_by(code=code).first():
            flash(f"Esiste già un articolo con codice {code}.", "danger")
        else:
            m = Material(
                code=code, description=description,
                material_type=request.form.get("material_type", "FERT"),
                uom=request.form.get("uom", "PZ").strip() or "PZ",
                standard_cost=request.form.get("standard_cost", type=float) or 0,
                sales_price=request.form.get("sales_price", type=float) or 0,
                vat_rate=request.form.get("vat_rate", type=float) or 22,
                qty_on_hand=request.form.get("qty_on_hand", type=float) or 0,
            )
            db.session.add(m)
            db.session.commit()
            flash(f"Articolo {code} creato.", "success")
            return redirect(url_for("materials.material_list"))

    mats = Material.query.order_by(Material.code).all()
    return render_template("materials/list.html", materials=mats,
                           type_labels=Material.TYPE_LABELS)


@materials_bp.route("/<int:mat_id>/update", methods=["POST"])
@login_required
def material_update(mat_id):
    m = Material.query.get_or_404(mat_id)
    m.standard_cost = request.form.get("standard_cost", type=float) or 0
    m.sales_price = request.form.get("sales_price", type=float) or 0
    m.vat_rate = request.form.get("vat_rate", type=float) or 22
    db.session.commit()
    flash(f"Articolo {m.code} aggiornato (costo standard {float(m.standard_cost):.4f} €, "
          f"prezzo {float(m.sales_price):.2f} €).", "success")
    return redirect(url_for("materials.material_list"))
