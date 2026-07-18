"""Anagrafica unica dei soggetti economici."""
from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required
from extensions import db
from models import EconomicSubject

parties_bp = Blueprint("parties", __name__, template_folder="../../templates/parties")


def _save_subject(subject=None):
    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()
    is_customer = request.form.get("is_customer") == "on"
    is_supplier = request.form.get("is_supplier") == "on"
    if not code or not name:
        return None, "Codice e denominazione sono obbligatori."
    if not is_customer and not is_supplier:
        return None, "Seleziona almeno un ruolo: cliente o fornitore."
    duplicate = EconomicSubject.query.filter(EconomicSubject.code == code)
    if subject:
        duplicate = duplicate.filter(EconomicSubject.id != subject.id)
    if duplicate.first():
        return None, f"Esiste già il soggetto {code}."
    if subject is None:
        subject = EconomicSubject(code=code, name=name)
        db.session.add(subject)
    for field in ("name", "subject_type", "piva", "codice_fiscale", "indirizzo", "cap", "comune",
                  "provincia", "nazione", "email", "pec", "telefono", "codice_destinatario",
                  "payment_terms", "iban"):
        value = request.form.get(field, "").strip()
        setattr(subject, field, value or None)
    subject.code = code
    subject.nazione = subject.nazione or "IT"
    subject.codice_destinatario = (subject.codice_destinatario or "0000000").upper()
    subject.payment_terms = subject.payment_terms or "Netto 30gg"
    subject.is_customer = is_customer
    subject.is_supplier = is_supplier
    subject.active = request.form.get("active") == "on"
    return subject, None


@parties_bp.route("/", methods=["GET", "POST"])
@login_required
def party_list():
    if request.method == "POST":
        party, error = _save_subject()
        if error:
            flash(error, "danger")
        else:
            db.session.commit()
            flash(f"Soggetto economico {party.code} creato.", "success")
            return redirect(url_for("parties.party_list"))
    parties = EconomicSubject.query.order_by(EconomicSubject.name).all()
    return render_template("parties/list.html", parties=parties)


@parties_bp.route("/<int:party_id>", methods=["GET", "POST"])
@login_required
def party_edit(party_id):
    party = EconomicSubject.query.get_or_404(party_id)
    if request.method == "POST":
        party, error = _save_subject(party)
        if error:
            flash(error, "danger")
        else:
            db.session.commit()
            flash("Soggetto economico aggiornato.", "success")
            return redirect(url_for("parties.party_list"))
    return render_template("parties/edit.html", party=party)
