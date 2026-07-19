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

from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify
from flask_login import login_required, current_user

from extensions import db
from models import Account, Material, ProductionEntry, DocumentSequence
from services.posting import post_journal_entry, UnbalancedEntryError
from services.logistic_client import get_bom, sposta_stock, LogisticError

production_bp = Blueprint("production", __name__, template_folder="../../templates/production")


def _acc(code):
    a = Account.query.filter_by(code=code).first()
    if a is None:
        raise ValueError(f"Conto {code} mancante nel Piano dei Conti — lancia il seed "
                          f"(flask --app app seed) per crearlo.")
    return a


def _calcola_materie_prime_da_bom(material, qty_produced):
    """
    Legge la distinta base di 'material' da MasterLogistic-WMS e calcola il
    costo delle materie prime necessarie per produrre qty_produced unità,
    valorizzando ogni componente al suo standard_cost SU MASTERLEDGER (la
    distinta base dà le QUANTITÀ, il costo lo sappiamo solo qui).

    Ritorna (totale_costo: Decimal, dettaglio: list[dict]). Se un componente
    della BOM non esiste come Material in MasterLedger, viene segnalato nel
    dettaglio con costo_unitario=None invece di essere ignorato in silenzio.
    """
    figli = get_bom(material.code)
    dettaglio = []
    totale = Decimal("0")
    for f in figli:
        codice = f["codice_figlio"]
        qty_necessaria = Decimal(str(f["quantita"])) * qty_produced
        componente = Material.query.filter_by(code=codice).first()
        costo_unitario = Decimal(str(componente.standard_cost)) if componente else None
        costo_totale = (qty_necessaria * costo_unitario) if costo_unitario is not None else None
        if costo_totale is not None:
            totale += costo_totale
        dettaglio.append({
            "codice": codice,
            "descrizione": f.get("desc_figlio") or "",
            "quantita_necessaria": float(qty_necessaria),
            "costo_unitario": float(costo_unitario) if costo_unitario is not None else None,
            "costo_totale": float(costo_totale) if costo_totale is not None else None,
        })
    return totale, dettaglio


@production_bp.route("/calcola-materie-prime")
@login_required
def calcola_materie_prime():
    """
    Endpoint AJAX: dato un prodotto finito e una quantità, calcola il costo
    delle materie prime dalla distinta base di MasterLogistic-WMS. Usato dal
    pulsante "Calcola da distinta base" nel form — NON scrive nulla, è solo
    un'anteprima.
    """
    material_id = request.args.get("material_id", type=int)
    qty = request.args.get("qty", type=float) or 0
    material = Material.query.get(material_id)
    if material is None:
        return jsonify({"error": "Prodotto finito non valido."}), 400
    if qty <= 0:
        return jsonify({"error": "Inserisci prima la quantità prodotta."}), 400

    try:
        totale, dettaglio = _calcola_materie_prime_da_bom(material, Decimal(str(qty)))
    except LogisticError as e:
        return jsonify({"error": str(e)}), 400

    if not dettaglio:
        return jsonify({"error": f"Nessuna distinta base trovata su MasterLogistic-WMS per {material.code}."}), 400

    mancanti = [d["codice"] for d in dettaglio if d["costo_unitario"] is None]
    return jsonify({
        "totale": float(totale),
        "dettaglio": dettaglio,
        "avviso": (f"Componenti non trovati come Material in MasterLedger (costo ignorato): "
                   f"{', '.join(mancanti)}") if mancanti else None,
    })


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

            # Se il form ha usato "Calcola da distinta base", conosciamo ESATTAMENTE
            # quali componenti sono stati consumati e quanto — li scarichiamo
            # davvero su MasterLogistic-WMS. Se l'importo è stato inserito a mano,
            # non sappiamo quali SKU scaricare: lo segnaliamo invece di indovinare.
            usa_bom = request.form.get("bom_usata") == "1"
            componenti_da_scaricare = []
            if usa_bom:
                _, dettaglio_bom = _calcola_materie_prime_da_bom(material, qty_produced)
                componenti_da_scaricare = [d for d in dettaglio_bom if d["quantita_necessaria"] > 0]

            # ── ECCEZIONE DECISA CON MAURI (produzione non ha nessun'altra via
            # per arrivare a MasterLogistic-WMS, a differenza di acquisti/
            # consegne che passano dal caricamento DDT su MasterLogistic
            # stesso): carichiamo il prodotto finito PRIMA di scrivere la
            # contabilità — se MasterLogistic non è raggiungibile, blocchiamo
            # tutto invece di salvare una scrittura senza il carico fisico. ──
            sposta_stock(material.code, float(qty_produced))

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

            # Scarico componenti da BOM: "best effort" — se uno fallisce lo
            # segnaliamo, ma non blocchiamo tutto (il carico FERT e la
            # contabilità sono già a posto a questo punto).
            componenti_non_scaricati = []
            for comp in componenti_da_scaricare:
                try:
                    sposta_stock(comp["codice"], -comp["quantita_necessaria"])
                except LogisticError as e:
                    componenti_non_scaricati.append(f'{comp["codice"]}: {e}')

            db.session.commit()
            msg = (f"Produzione registrata: {entry.doc_number} — "
                   f"€ {float(totale_cogm):.2f} capitalizzati a magazzino Prodotti Finiti "
                   f"(caricati {float(qty_produced):.0f} {material.code} su MasterLogistic-WMS).")
            if usa_bom and not componenti_non_scaricati:
                msg += " Componenti scaricati da distinta base."
            elif not usa_bom and raw_cost > 0:
                msg += (" ATTENZIONE: materie prime inserite a mano — la giacenza delle "
                        "materie prime su MasterLogistic-WMS NON è stata aggiornata.")
            flash(msg, "success" if not componenti_non_scaricati else "warning")
            if componenti_non_scaricati:
                flash("Componenti non scaricati (verificare a mano su MasterLogistic-WMS): "
                      + "; ".join(componenti_non_scaricati), "warning")
            return redirect(url_for("production.completata"))

        except UnbalancedEntryError as e:
            db.session.rollback()
            flash(str(e), "danger")
        except (ValueError, LogisticError) as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("production/completata.html", materiali_finiti=materiali_finiti, ultime=ultime)
