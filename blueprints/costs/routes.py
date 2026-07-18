from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from extensions import db
from models import CostCenter, JournalLine, Account

costs_bp = Blueprint("costs", __name__, template_folder="../../templates/costs")


@costs_bp.route("/")
@login_required
def cost_centers():
    centers = CostCenter.query.filter_by(active=True).order_by(CostCenter.code).all()
    return render_template("costs/cost_centers.html", centers=centers)


@costs_bp.route("/new", methods=["POST"])
@login_required
def cost_center_new():
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    if not code or not name:
        flash("Codice e nome Centro di costo obbligatori.", "danger")
        return redirect(url_for("costs.cost_centers"))
    if CostCenter.query.filter_by(code=code).first():
        flash(f"Il Centro di costo {code} esiste già.", "danger")
        return redirect(url_for("costs.cost_centers"))
    db.session.add(CostCenter(code=code, name=name))
    db.session.commit()
    flash(f"Centro di costo {code} — {name} creato (Centri di costo).", "success")
    return redirect(url_for("costs.cost_centers"))


@costs_bp.route("/report")
@login_required
def report():
    """
    Report costi in tempo reale: NON è una tabella separata riempita a
    mano — sono le righe di Prima Nota (JournalLine) sui conti marcati
    cost_relevant=True, aggregate per Centro di costo. Esattamente lo stesso
    principio del vecchio "postFI" del prototipo browser: ogni scrittura
    su un conto economico con un Centro di costo genera automaticamente
    la vista costi, senza doppia registrazione.
    """
    lines = (JournalLine.query
             .join(Account)
             .filter(Account.cost_relevant == True)  # noqa: E712
             .order_by(JournalLine.id.desc())
             .limit(200)
             .all())

    by_center = {}
    for l in lines:
        key = l.cost_center.code if l.cost_center else "— Non Assegnato —"
        by_center.setdefault(key, {"name": l.cost_center.name if l.cost_center else "Nessun Centro di costo", "total": 0, "lines": []})
        amount = float(l.dare or 0) if l.account.cost_relevant_type == "COST" else float(l.avere or 0)
        by_center[key]["total"] += amount
        by_center[key]["lines"].append(l)

    return render_template("costs/report.html", by_center=by_center, lines=lines)
