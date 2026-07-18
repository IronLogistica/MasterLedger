from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from extensions import db
from models import Account, CostCenter, JournalEntry
from services.posting import post_journal_entry, reverse_journal_entry, UnbalancedEntryError

gl_bp = Blueprint("gl", __name__, template_folder="../../templates/gl")


@gl_bp.route("/")
@login_required
def journal_list():
    """Il 'Giornale' — lista cronologica di TUTTI i documenti (equivalente del
    vecchio 'Giornale Integrato' del simulatore, qui però è il vero libro
    giornale con numerazione progressiva reale)."""
    page = request.args.get("page", 1, type=int)
    entries = (JournalEntry.query
               .order_by(JournalEntry.created_at.desc())
               .paginate(page=page, per_page=25, error_out=False))
    return render_template("gl/journal_list.html", entries=entries)


@gl_bp.route("/entry/<int:entry_id>")
@login_required
def entry_detail(entry_id):
    entry = JournalEntry.query.get_or_404(entry_id)
    return render_template("gl/entry_detail.html", entry=entry)


@gl_bp.route("/entry/<int:entry_id>/reverse", methods=["POST"])
@login_required
def entry_reverse(entry_id):
    try:
        new_entry = reverse_journal_entry(entry_id, created_by_id=current_user.id)
        flash(f"Documento stornato correttamente. Nuovo documento: {new_entry.doc_number}.", "success")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(url_for("gl.entry_detail", entry_id=entry_id))


@gl_bp.route("/journal_entry", methods=["GET", "POST"])
@login_required
def journal_entry():
    """Prima nota — Registrazione manuale in Prima Nota (General Journal Entry)."""
    accounts = Account.query.filter_by(active=True).order_by(Account.code).all()
    cost_centers = CostCenter.query.filter_by(active=True).order_by(CostCenter.code).all()

    if request.method == "POST":
        doc_date_str = request.form.get("doc_date")
        description = request.form.get("description", "").strip()
        account_ids = request.form.getlist("account_id[]")
        pks = request.form.getlist("pk[]")           # '40' = Dare, '50' = Avere
        amounts = request.form.getlist("amount[]")
        cost_centers_sel = request.form.getlist("cost_center_id[]")

        lines = []
        for acc_id, pk, amt, cc in zip(account_ids, pks, amounts, cost_centers_sel):
            if not acc_id or not amt:
                continue
            amount = float(amt.replace(",", "."))
            lines.append({
                "account_id": int(acc_id),
                "dare": amount if pk == "40" else 0,
                "avere": amount if pk == "50" else 0,
                "cost_center_id": int(cc) if cc else None,
            })

        if len(lines) < 2:
            flash("Servono almeno due righe (una in Dare e una in Avere).", "danger")
            return render_template("gl/journal_entry.html", accounts=accounts, cost_centers=cost_centers)

        try:
            doc_date = datetime.strptime(doc_date_str, "%Y-%m-%d").date() if doc_date_str else None
            entry = post_journal_entry(
                doc_type="SA", prefix="10",
                doc_date=doc_date, description=description or "Prima Nota Manuale",
                lines=lines, source_module="LEDGER", created_by_id=current_user.id,
            )
            flash(f"Documento {entry.doc_number} registrato correttamente in Prima Nota.", "success")
            return redirect(url_for("gl.entry_detail", entry_id=entry.id))
        except UnbalancedEntryError as e:
            flash(str(e), "danger")

    return render_template("gl/journal_entry.html", accounts=accounts, cost_centers=cost_centers)
