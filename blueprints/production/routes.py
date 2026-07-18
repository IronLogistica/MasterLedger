"""
blueprints/production/routes.py — "Produzione Completata" (COGM), soluzione
PONTE finché MasterProduction non è pronto e non può fornire dati precisi
per ordine di produzione.

Registrazione periodica (tipicamente mensile), a costo standard:

    Dare  Magazzino Prodotti Finiti (160000)   = materie prime + manodopera + costi indiretti
        Avere  Magazzino Materie Prime (150000) = materie prime consumate (movimento reale)
        Avere  Variazione Rimanenze PF (430000) = manodopera diretta + costi indiretti capitalizzati

La manodopera diretta oggi si legge a mano da MasterWork (pagina "Analisi
Tempi" — colonna Totale Fatturato/ore). I costi indiretti di produzione
(es. taglio e foratura, oggi gestiti da MasterProduction non ancora pronto)
si trattano come pool di costo FISSO mensile, spalmato sulla produzione del
periodo — non è un costo unitario preciso, è una stima ragionevole finché
non arrivano dati macchina reali.

Quando MasterProduction sarà pronto, questa rotta (o una nuova, equivalente)
riceverà i tre importi in automatico invece che a mano — la struttura
contabile sotto NON cambia.
"""
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user

from extensions import db
from models import Account, Material, ProductionEntry, DocumentSequence
from services.posting import post_journal_entry, UnbalancedEntryError

production_bp = Blueprint("production", __name__, template_folder="../../templates/production")


def _acc(code):
    a = Account.query.filter_by(code=code).first()
    if a is None:
        raise ValueError(f"Conto {code} mancante nel Piano dei Conti — lancia il seed "
                          f"(flask --app app seed) per crearlo.")
    return a


@production_bp.route("/completata", methods=["GET", "POST"])
@login_required
def completata():
    materiali_finiti = Material.query.filter_by(material_type="FERT", active=True).order_by(Material.code).all()
    ultime = ProductionEntry.query.order_by(ProductionEntry.created_at.desc()).limit(15).all()

    if request.method == "POST":
        material_id = request.form.get("material_id", type=int)
        material = Material.query.get(material_id)
        if material is None:
            flash("Seleziona un prodotto finito valido.", "danger")
            return redirect(url_for("production.completata"))

        try:
            qty_produced = Decimal(str(request.form.get("qty_produced", "0")).replace(",", "."))
            raw_cost = Decimal(str(request.form.get("raw_material_cost", "0")).replace(",", "."))
            labor_cost = Decimal(str(request.form.get("direct_labor_cost", "0")).replace(",", "."))
            overhead_cost = Decimal(str(request.form.get("overhead_cost", "0")).replace(",", "."))
        except Exception:
            flash("Controlla i valori numerici inseriti (quantità e importi).", "danger")
            return redirect(url_for("production.completata"))

        if qty_produced <= 0:
            flash("La quantità prodotta deve essere maggiore di zero.", "danger")
            return redirect(url_for("production.completata"))

        totale_cogm = raw_cost + labor_cost + overhead_cost
        if totale_cogm <= 0:
            flash("Inserisci almeno un valore di costo (materie prime, manodopera o costi indiretti).", "danger")
            return redirect(url_for("production.completata"))

        try:
            fert_acc = _acc(material.inventory_account_code)   # 160000, o quello del tipo articolo scelto
            roh_acc = _acc("150000")
            variazione_acc = _acc("430000")

            journal_lines = []
            journal_lines.append({
                "account_id": fert_acc.id, "dare": totale_cogm, "avere": 0,
                "description": f"Produzione completata {material.code} × {float(qty_produced):.0f}",
            })
            if raw_cost > 0:
                journal_lines.append({
                    "account_id": roh_acc.id, "dare": 0, "avere": raw_cost,
                    "description": "Consumo materie prime da produzione",
                })
            if (labor_cost + overhead_cost) > 0:
                journal_lines.append({
                    "account_id": variazione_acc.id, "dare": 0, "avere": labor_cost + overhead_cost,
                    "description": "Manodopera diretta e costi indiretti capitalizzati a magazzino",
                })

            pr_doc_number = DocumentSequence.next_number("PR", "40")

            entry = post_journal_entry(
                doc_type="SA", prefix="10", doc_date=None,
                description=f"Produzione Completata {pr_doc_number} — {material.code} "
                            f"({request.form.get('period_label', '').strip() or 'periodo corrente'})",
                lines=journal_lines, source_module="PRODUZIONE",
                reference=pr_doc_number, created_by_id=current_user.id,
            )

            pe = ProductionEntry(
                doc_number=pr_doc_number,
                material_id=material.id,
                qty_produced=qty_produced,
                raw_material_cost=raw_cost,
                direct_labor_cost=labor_cost,
                overhead_cost=overhead_cost,
                period_label=request.form.get("period_label", "").strip(),
                notes=request.form.get("notes", "").strip(),
                journal_entry_id=entry.id,
                created_by_id=current_user.id,
            )
            db.session.add(pe)

            # Carico effettivo a magazzino del prodotto finito
            material.qty_on_hand = Decimal(str(material.qty_on_hand)) + qty_produced

            db.session.commit()
            flash(f"Produzione registrata: {entry.doc_number} — "
                  f"€ {float(totale_cogm):.2f} capitalizzati a magazzino Prodotti Finiti.", "success")
            return redirect(url_for("production.completata"))

        except UnbalancedEntryError as e:
            db.session.rollback()
            flash(str(e), "danger")
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("production/completata.html", materiali_finiti=materiali_finiti, ultime=ultime)
