from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from sqlalchemy import and_

from extensions import db
from models import CostCenter, JournalEntry, JournalLine, Account

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


def _parse_date(value, field_name):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{field_name}: usa il formato data valido.")


@costs_bp.route("/report")
@login_required
def report():
    """Report CO actual: righe FI su elementi di costo/ricavo, senza doppio ledger."""
    centers = CostCenter.query.filter_by(active=True).order_by(CostCenter.code).all()
    accounts = (Account.query.filter_by(active=True)
                .filter(Account.cost_relevant == True)  # noqa: E712
                .order_by(Account.code).all())
    filters = {
        "from_date": request.args.get("from_date", ""),
        "to_date": request.args.get("to_date", ""),
        "cost_center_id": request.args.get("cost_center_id", type=int),
        "account_id": request.args.get("account_id", type=int),
        "kind": request.args.get("kind", "COST").upper(),
    }
    if filters["kind"] not in ("COST", "REVENUE", "ALL"):
        filters["kind"] = "COST"
    try:
        date_from = _parse_date(filters["from_date"], "Data da")
        date_to = _parse_date(filters["to_date"], "Data a")
        if date_from and date_to and date_from > date_to:
            raise ValueError("La data iniziale non può essere successiva alla data finale.")
    except ValueError as exc:
        flash(str(exc), "danger")
        date_from = date_to = None

    query = (JournalLine.query.join(Account).join(JournalEntry)
             .filter(Account.cost_relevant == True))  # noqa: E712
    if date_from:
        query = query.filter(JournalEntry.doc_date >= date_from)
    if date_to:
        query = query.filter(JournalEntry.doc_date <= date_to)
    if filters["cost_center_id"]:
        query = query.filter(JournalLine.cost_center_id == filters["cost_center_id"])
    if filters["account_id"]:
        query = query.filter(JournalLine.account_id == filters["account_id"])
    if filters["kind"] != "ALL":
        query = query.filter(Account.cost_relevant_type == filters["kind"])

    lines = query.order_by(JournalEntry.doc_date.desc(), JournalLine.id.desc()).all()
    by_center, by_element, grand_total = {}, {}, 0.0
    for line in lines:
        # Costi: Dare-Avere; ricavi: Avere-Dare. Gli storni riducono il totale CO.
        amount = (float(line.dare or 0) - float(line.avere or 0)
                  if line.account.cost_relevant_type == "COST"
                  else float(line.avere or 0) - float(line.dare or 0))
        cc_code = line.cost_center.code if line.cost_center else "— Non assegnato —"
        cc_name = line.cost_center.name if line.cost_center else "Anomalia CO / storico senza oggetto"
        by_center.setdefault(cc_code, {"name": cc_name, "total": 0.0, "lines": []})
        by_center[cc_code]["total"] += amount
        by_center[cc_code]["lines"].append((line, amount))
        element_key = (cc_code, line.account.code)
        by_element.setdefault(element_key, {
            "cost_center": cc_code, "account": line.account,
            "total": 0.0,
        })["total"] += amount
        grand_total += amount

    return render_template("costs/report.html", by_center=by_center,
                           by_element=sorted(by_element.values(), key=lambda x: (x["cost_center"], x["account"].code)),
                           grand_total=grand_total, lines=lines, centers=centers,
                           accounts=accounts, filters=filters)
