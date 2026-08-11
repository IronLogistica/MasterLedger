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
from datetime import datetime, date
from decimal import Decimal
import calendar

from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify
from flask_login import login_required, current_user

from extensions import db
from models import (Account, Material, ProductionEntry, DocumentSequence, Delivery, DeliveryLine,
                     ProductionOverheadItem, OverheadAdjustment, JournalEntry, JournalLine, StandardCost,
                     ProductCostTarget, WorkCenter)
from services.posting import post_journal_entry, UnbalancedEntryError
from services.warehouse import bom_components, post_stock_movement, WarehouseError
from services.product_cost import money, variance, variance_pct, classification
from services.routing_cost import calcola_costo_pieno_da_routing, active_routing

production_bp = Blueprint("production", __name__, template_folder="../../templates/production")


def _acc(code):
    a = Account.query.filter_by(code=code).first()
    if a is None:
        raise ValueError(f"Conto {code} mancante nel Piano dei Conti — lancia il seed "
                          f"(flask --app app seed) per crearlo.")
    return a


def _calcola_materie_prime_da_bom(material, qty_produced):
    """
    Legge la distinta base di 'material' dal magazzino interno (BillOfMaterial)
    e calcola il costo delle materie prime necessarie per produrre qty_produced
    unità, valorizzando ogni componente al suo standard_cost.

    Ritorna (totale_costo: Decimal, dettaglio: list[dict]). Se un componente
    della BOM non ha un costo standard valorizzato, viene segnalato nel
    dettaglio con costo_unitario=None invece di essere ignorato in silenzio.
    """
    figli = bom_components(material.id)
    dettaglio = []
    totale = Decimal("0")
    for f in figli:
        componente = f.component_material
        qty_necessaria = Decimal(str(f.qty_per)) * qty_produced * (Decimal("1") + Decimal(str(f.scrap_pct or 0)) / 100)
        costo_unitario = Decimal(str(componente.standard_cost)) if componente and componente.standard_cost else None
        costo_totale = (qty_necessaria * costo_unitario) if costo_unitario is not None else None
        if costo_totale is not None:
            totale += costo_totale
        dettaglio.append({
            "codice": componente.code if componente else "?",
            "material_id": f.component_material_id,
            "descrizione": componente.description if componente else "",
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
    delle materie prime dalla distinta base interna. Usato dal pulsante
    "Calcola da distinta base" nel form — NON scrive nulla, è solo un'anteprima.
    """
    material_id = request.args.get("material_id", type=int)
    qty = request.args.get("qty", type=float) or 0
    material = Material.query.get(material_id)
    if material is None:
        return jsonify({"error": "Prodotto finito non valido."}), 400
    if qty <= 0:
        return jsonify({"error": "Inserisci prima la quantità prodotta."}), 400

    totale, dettaglio = _calcola_materie_prime_da_bom(material, Decimal(str(qty)))

    if not dettaglio:
        return jsonify({"error": f"Nessuna distinta base trovata per {material.code} — creala in Distinta Base."}), 400

    mancanti = [d["codice"] for d in dettaglio if d["costo_unitario"] is None]
    return jsonify({
        "totale": float(totale),
        "dettaglio": dettaglio,
        "avviso": (f"Componenti senza costo standard valorizzato (costo ignorato): "
                   f"{', '.join(mancanti)}") if mancanti else None,
    })


def _pool_reparto_mese(anno, mese):
    """Somma delle voci del pool di reparto (Livello 1) per un dato anno/mese."""
    righe = ProductionOverheadItem.query.filter_by(year=anno, month=mese).all()
    totale = sum((Decimal(str(r.amount or 0)) for r in righe), Decimal("0"))
    return totale, righe


def _costo_primo_mensile_per_materiale(mese_riferimento):
    """
    Per ogni materiale con attività nel mese, calcola il "costo primo"
    (materie prime + manodopera diretta, PRIMA di qualunque overhead):
    - se il materiale è stato fabbricato quel mese (ha una o più
      ProductionEntry), usa la somma reale di raw_material_cost + direct_labor_cost;
    - altrimenti (materiale rivenduto, non fabbricato quel mese), usa
      qty_venduta × standard_cost come proxy del costo primo.

    Ritorna {material_id: Decimal}.
    """
    primo_giorno = mese_riferimento.replace(day=1)
    ultimo_giorno = mese_riferimento.replace(day=calendar.monthrange(mese_riferimento.year, mese_riferimento.month)[1])

    costo_primo = {}

    prodotti = (
        db.session.query(
            ProductionEntry.material_id,
            db.func.sum(ProductionEntry.raw_material_cost + ProductionEntry.direct_labor_cost).label("costo_primo")
        )
        .filter(ProductionEntry.doc_date >= primo_giorno, ProductionEntry.doc_date <= ultimo_giorno)
        .group_by(ProductionEntry.material_id)
        .all()
    )
    for p in prodotti:
        costo_primo[p.material_id] = Decimal(str(p.costo_primo or 0))

    venduti = (
        db.session.query(
            Material.id, Material.standard_cost, db.func.sum(DeliveryLine.qty).label("qty_venduta")
        )
        .join(DeliveryLine, DeliveryLine.material_id == Material.id)
        .join(Delivery, Delivery.id == DeliveryLine.delivery_id)
        .filter(Delivery.doc_date >= primo_giorno, Delivery.doc_date <= ultimo_giorno)
        .group_by(Material.id, Material.standard_cost)
        .all()
    )
    for v in venduti:
        if v.id not in costo_primo:  # non fabbricato questo mese: usa costo standard × quantità come proxy
            costo_primo[v.id] = Decimal(str(v.qty_venduta or 0)) * Decimal(str(v.standard_cost or 0))

    return costo_primo


def _calcola_overhead_da_fatturato(material, mese_riferimento, peso_fatturato_pct=100):
    """
    Calcola la quota di costi indiretti di REPARTO (Livello 1: pool mensile
    già inserito voce per voce nella pagina "Pool Overhead Reparto") da
    attribuire a 'material', tra i soli articoli "carpenteria propria"
    (is_carpenteria_propria=True) — un prodotto comprato e rivenduto così
    com'è non entra in questo calcolo, perché non passa dal reparto la cui
    spesa fissa stiamo spalmando.

    peso_fatturato_pct: 0-100, il peso dato alla quota-fatturato nel mix; il
    resto (100-peso_fatturato_pct) va alla quota-costo primo (materie prime +
    manodopera diretta). peso_fatturato_pct=100 (default) = solo fatturato,
    com'era prima; peso_fatturato_pct=50 = mix 50/50; peso_fatturato_pct=0 =
    solo costo primo.

    Ritorna (quota: Decimal, dettaglio: list[dict], pool_totale: Decimal, avviso: str|None).
    """
    primo_giorno = mese_riferimento.replace(day=1)
    ultimo_giorno = mese_riferimento.replace(day=calendar.monthrange(mese_riferimento.year, mese_riferimento.month)[1])

    pool_totale, _ = _pool_reparto_mese(mese_riferimento.year, mese_riferimento.month)

    righe = (
        db.session.query(
            Material.id, Material.code, Material.description,
            db.func.sum(DeliveryLine.qty * DeliveryLine.price).label("fatturato")
        )
        .join(DeliveryLine, DeliveryLine.material_id == Material.id)
        .join(Delivery, Delivery.id == DeliveryLine.delivery_id)
        .filter(Material.is_carpenteria_propria.is_(True))
        .filter(Delivery.doc_date >= primo_giorno, Delivery.doc_date <= ultimo_giorno)
        .group_by(Material.id, Material.code, Material.description)
        .all()
    )

    fatturato_totale = sum((Decimal(str(r.fatturato or 0)) for r in righe), Decimal("0"))
    costo_primo_map = _costo_primo_mensile_per_materiale(mese_riferimento)
    # Il costo primo totale, ai fini del mix, va calcolato SOLO sugli stessi
    # articoli "carpenteria propria" che concorrono a questo pool (coerenza
    # con la base "fatturato", altrimenti le due percentuali non sono
    # confrontabili).
    costo_primo_totale = sum((costo_primo_map.get(r.id, Decimal("0")) for r in righe), Decimal("0"))

    peso_fatt = Decimal(str(peso_fatturato_pct)) / 100
    peso_cp = 1 - peso_fatt
    avviso_rinormalizzazione = None
    # Se una delle due basi del mix non ha dati per questo mese, il suo peso
    # non può restare "silenzioso": senza rinormalizzare, la somma delle
    # quote sui prodotti non arriverebbe mai al 100% del pool (una parte del
    # costo indiretto sparirebbe senza essere assegnata a nessuno).
    if fatturato_totale <= 0 and costo_primo_totale > 0 and peso_fatt > 0:
        avviso_rinormalizzazione = (
            f"Nessun fatturato nel mese per gli articoli di carpenteria propria: il mix richiesto "
            f"({peso_fatturato_pct:.0f}% fatturato / {100 - peso_fatturato_pct:.0f}% costo primo) è stato "
            f"rinormalizzato al 100% costo primo, altrimenti la quota-fatturato (senza base) sarebbe "
            f"rimasta non assegnata a nessun prodotto."
        )
        peso_fatt, peso_cp = Decimal("0"), Decimal("1")
    elif costo_primo_totale <= 0 and fatturato_totale > 0 and peso_cp > 0:
        avviso_rinormalizzazione = (
            f"Nessun costo primo calcolabile nel mese per gli articoli di carpenteria propria: il mix "
            f"richiesto ({peso_fatturato_pct:.0f}% fatturato / {100 - peso_fatturato_pct:.0f}% costo primo) "
            f"è stato rinormalizzato al 100% fatturato, altrimenti la quota-costo-primo (senza base) "
            f"sarebbe rimasta non assegnata a nessun prodotto."
        )
        peso_fatt, peso_cp = Decimal("1"), Decimal("0")

    dettaglio = []
    for r in righe:
        quota_fatt_pct = (Decimal(str(r.fatturato or 0)) / fatturato_totale * 100) if fatturato_totale > 0 else Decimal("0")
        cp_materiale = costo_primo_map.get(r.id, Decimal("0"))
        quota_cp_pct = (cp_materiale / costo_primo_totale * 100) if costo_primo_totale > 0 else Decimal("0")
        quota_mix_pct = peso_fatt * quota_fatt_pct + peso_cp * quota_cp_pct
        dettaglio.append({
            "codice": r.code, "descrizione": r.description,
            "fatturato": float(r.fatturato or 0), "quota_fatturato_pct": float(quota_fatt_pct),
            "costo_primo": float(cp_materiale), "quota_costo_primo_pct": float(quota_cp_pct),
            "quota_pct": float(quota_mix_pct),
        })

    if pool_totale <= 0:
        return Decimal("0"), dettaglio, pool_totale, (
            f"Nessun pool di costi indiretti di reparto inserito per {mese_riferimento.strftime('%m/%Y')} — "
            f'vai su "Pool Overhead Reparto" e inserisci le voci del mese prima di calcolare.'
        )

    if not material.is_carpenteria_propria:
        return Decimal("0"), dettaglio, pool_totale, (
            f'"{material.code}" non è marcato come "Carpenteria propria" in Anagrafica Articoli — '
            f"non dovrebbe ricevere quota di questo costo indiretto. Marcalo prima, se è un errore."
        )

    if fatturato_totale <= 0 and costo_primo_totale <= 0:
        return Decimal("0"), dettaglio, pool_totale, (
            f"Nessuna consegna trovata nel mese {mese_riferimento.strftime('%m/%Y')} per articoli "
            f'marcati "Carpenteria propria" — impossibile calcolare una quota (mancano sia fatturato che costo primo su cui basarla).'
        )

    riga_materiale = next((d for d in dettaglio if d["codice"] == material.code), None)
    quota_pct_materiale = Decimal(str(riga_materiale["quota_pct"])) if riga_materiale else Decimal("0")
    quota = pool_totale * (quota_pct_materiale / 100)
    return quota, dettaglio, pool_totale, avviso_rinormalizzazione


@production_bp.route("/calcola-overhead")
@login_required
def calcola_overhead():
    """
    Endpoint AJAX: calcola la quota di costi indiretti di reparto spettante a
    un prodotto, in proporzione al suo fatturato reale del mese (tra i soli
    prodotti di carpenteria propria), leggendo il pool già inserito voce per
    voce per quel mese. Solo anteprima, non scrive nulla.
    """
    material_id = request.args.get("material_id", type=int)
    mese = request.args.get("mese", "").strip()  # formato YYYY-MM da <input type="month">
    peso_fatturato = request.args.get("peso_fatturato", type=float)
    if peso_fatturato is None:
        peso_fatturato = 100  # default: comportamento precedente, solo fatturato
    peso_fatturato = max(0, min(100, peso_fatturato))

    material = Material.query.get(material_id)
    if material is None:
        return jsonify({"error": "Prodotto finito non valido."}), 400
    try:
        anno, mese_num = (int(x) for x in mese.split("-"))
        mese_riferimento = date(anno, mese_num, 1)
    except Exception:
        return jsonify({"error": "Seleziona il mese di riferimento."}), 400

    quota, dettaglio, pool_totale, avviso = _calcola_overhead_da_fatturato(material, mese_riferimento, peso_fatturato)
    if avviso and quota == 0:
        return jsonify({"error": avviso, "dettaglio": dettaglio, "pool_totale": float(pool_totale)}), 400

    return jsonify({"quota": float(quota), "dettaglio": dettaglio, "pool_totale": float(pool_totale), "avviso": avviso})


@production_bp.route("/calcola-costo-pieno")
@login_required
def calcola_costo_pieno():
    """
    Endpoint AJAX: costo pieno standard di un prodotto calcolato col metodo
    SAP — materiali dalla Distinta Base (BOM), manodopera diretta + overhead
    dal Ciclo di Lavorazione (Routing) con tariffa oraria di centro di
    lavoro. Sostituisce, per gli articoli che hanno un ciclo attivo, la
    quota-fatturato approssimata di /calcola-overhead. Solo anteprima, non
    scrive nulla — usato per innestare/verificare BOM+Routing+overhead prima
    di decidere se azzerare il metodo precedente.
    """
    material_id = request.args.get("material_id", type=int)
    mese = request.args.get("mese", "").strip()
    material = Material.query.get(material_id)
    if material is None:
        return jsonify({"error": "Prodotto finito non valido."}), 400
    try:
        anno, mese_num = (int(x) for x in mese.split("-"))
    except Exception:
        return jsonify({"error": "Seleziona il mese di riferimento."}), 400

    totale_materiali, dettaglio_materiali = _calcola_materie_prime_da_bom(material, Decimal("1"))
    totale_manodopera, totale_overhead, dettaglio_routing, avvisi = calcola_costo_pieno_da_routing(
        material, anno, mese_num)

    if not dettaglio_materiali:
        avvisi = [f'Nessuna distinta base trovata per {material.code} — crealo in "Distinta Base".'] + avvisi

    return jsonify({
        "costo_materiali": float(totale_materiali),
        "costo_manodopera": float(totale_manodopera),
        "costo_overhead": float(totale_overhead),
        "costo_pieno_unitario": float(totale_materiali + totale_manodopera + totale_overhead),
        "dettaglio_materiali": dettaglio_materiali,
        "dettaglio_routing": dettaglio_routing,
        "avvisi": avvisi,
    })


def _trova_standard_applicabile(material_id, anno, mese):
    """
    Trova il Costo Standard applicabile per un materiale in un dato anno/mese:
    l'ultimo standard fissato con (year, month) <= (anno, mese) — cioè
    "valido da quel mese in poi, finché non arriva uno standard più recente".
    Ritorna None se non esiste nessuno standard applicabile (in quel caso si
    capitalizza al consuntivo come sempre, senza varianze).
    """
    candidati = (StandardCost.query
                 .filter_by(material_id=material_id)
                 .filter(db.or_(StandardCost.year < anno,
                                db.and_(StandardCost.year == anno, StandardCost.month <= mese)))
                 .order_by(StandardCost.year.desc(), StandardCost.month.desc())
                 .first())
    return candidati


@production_bp.route("/costo-standard", methods=["GET", "POST"])
@login_required
def costo_standard():
    """
    Gestione del Costo Standard di ogni prodotto finito — FISSATO IN ANTICIPO
    (es. a inizio mese/anno), il prerequisito per calcolare le varianze di
    produzione (materiali, manodopera, overhead) alla SAP. Una volta fissato
    per un materiale/periodo, resta valido finché non ne inserisci uno più
    recente per lo stesso materiale.
    """
    materiali_finiti = Material.query.filter_by(active=True).order_by(Material.code).all()

    if request.method == "POST":
        try:
            material_id = request.form.get("material_id", type=int)
            anno = request.form.get("year", type=int)
            mese = request.form.get("month", type=int)
            mat_cost = Decimal(str(request.form.get("standard_material_cost", "0")).replace(",", "."))
            lab_cost = Decimal(str(request.form.get("standard_labor_cost", "0")).replace(",", "."))
            oh_cost = Decimal(str(request.form.get("standard_overhead_cost", "0")).replace(",", "."))
        except Exception:
            flash("Controlla i valori inseriti.", "danger")
            return redirect(url_for("production.costo_standard"))

        if not material_id or not anno or not mese:
            flash("Prodotto, anno e mese sono obbligatori.", "danger")
            return redirect(url_for("production.costo_standard"))

        standard = StandardCost(
            material_id=material_id, year=anno, month=mese,
            standard_material_cost=mat_cost, standard_labor_cost=lab_cost, standard_overhead_cost=oh_cost,
            notes=request.form.get("notes", "").strip(), created_by_id=current_user.id,
        )
        db.session.add(standard)
        # Lo stesso standard unitario è quello che SD userà al PGI/DDT per
        # Dare COGS / Avere PF. Così COGM e COGS restano riconciliati.
        material = Material.query.get(material_id)
        if material is not None:
            material.standard_cost = mat_cost + lab_cost + oh_cost
        db.session.commit()
        flash("Costo Standard salvato e allineato al costo standard usato dal PGI/DDT.", "success")
        return redirect(url_for("production.costo_standard"))

    tutti = StandardCost.query.order_by(StandardCost.year.desc(), StandardCost.month.desc(),
                                        StandardCost.material_id).all()
    return render_template("production/costo_standard.html", tutti=tutti, materiali_finiti=materiali_finiti)


@production_bp.route("/costo-standard/<int:sc_id>/elimina", methods=["POST"])
@login_required
def elimina_costo_standard(sc_id):
    sc = StandardCost.query.get_or_404(sc_id)
    db.session.delete(sc)
    db.session.commit()
    flash("Costo Standard eliminato.", "success")
    return redirect(url_for("production.costo_standard"))


@production_bp.route("/pool-reparto", methods=["GET", "POST"])
@login_required
def pool_reparto():
    """
    Gestione del pool di costi indiretti di REPARTO (Livello 1: taglio,
    foratura, assemblaggio, confezionamento) — voce per voce, UNA VOLTA AL
    MESE. Es. "Ammortamento macchina taglio: 300€", "Energia reparto: 150€".
    La somma di queste voci è il pool da cui ogni prodotto di carpenteria
    propria riceve la propria quota in Produzione Completata.
    """
    if request.method == "POST":
        try:
            anno = request.form.get("year", type=int)
            mese = request.form.get("month", type=int)
            importo = Decimal(str(request.form.get("amount", "0")).replace(",", "."))
            descrizione = request.form.get("description", "").strip()
        except Exception:
            flash("Controlla i valori inseriti.", "danger")
            return redirect(url_for("production.pool_reparto"))

        if not descrizione or not anno or not mese:
            flash("Descrizione, anno e mese sono obbligatori.", "danger")
            return redirect(url_for("production.pool_reparto"))

        db.session.add(ProductionOverheadItem(
            year=anno, month=mese, description=descrizione, amount=importo,
            work_center_id=request.form.get("work_center_id", type=int) or None,
            created_by_id=current_user.id))
        db.session.commit()
        flash("Voce salvata.", "success")
        return redirect(url_for("production.pool_reparto"))

    tutte = ProductionOverheadItem.query.order_by(
        ProductionOverheadItem.year.desc(), ProductionOverheadItem.month.desc()).all()
    # Raggruppa per anno/mese, con il totale del pool, per la vista
    gruppi = {}
    for v in tutte:
        chiave = (v.year, v.month)
        gruppi.setdefault(chiave, {"voci": [], "totale": Decimal("0")})
        gruppi[chiave]["voci"].append(v)
        gruppi[chiave]["totale"] += Decimal(str(v.amount or 0))
    centri = WorkCenter.query.filter_by(active=True).order_by(WorkCenter.code).all()
    return render_template("production/pool_reparto.html", gruppi=gruppi, centri=centri)


@production_bp.route("/pool-reparto/<int:voce_id>/elimina", methods=["POST"])
@login_required
def elimina_voce_pool(voce_id):
    v = ProductionOverheadItem.query.get_or_404(voce_id)
    db.session.delete(v)
    db.session.commit()
    flash("Voce eliminata.", "success")
    return redirect(url_for("production.pool_reparto"))


# ══════════════════════════════════════════════════════════════
# OVERHEAD GENERALE AZIENDALE (Livello 2) — per il "costo pieno" gestionale
# (BOM + Routing + Overhead, alla SAP), usato SOLO per l'analisi di
# redditività/margine — NON entra mai nel COGM né si capitalizza a magazzino
# (i costi generali/amministrativi restano sempre costo di periodo).
#
#   Overhead generale del mese = (somma di TUTTI i conti di costo nel libro
#                                  giornale per il mese)
#                                - (somma delle voci di overhead di REPARTO
#                                   già imputate in Produzione Completata nello
#                                   stesso mese — per non contarle due volte)
#                                + (rettifiche manuali per ratei/risconti)
# ══════════════════════════════════════════════════════════════
def _calcola_overhead_generale(anno, mese):
    """
    Ritorna un dict con il dettaglio del calcolo dell'overhead generale
    aziendale per anno/mese: {costi_totali_ge, overhead_reparto_dedotto,
    rettifiche, totale}.
    """
    primo_giorno = date(anno, mese, 1)
    ultimo_giorno = date(anno, mese, calendar.monthrange(anno, mese)[1])

    costi_totali = (
        db.session.query(db.func.coalesce(db.func.sum(JournalLine.dare - JournalLine.avere), 0))
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .join(Account, Account.id == JournalLine.account_id)
        .filter(Account.account_type == "costo")
        .filter(JournalEntry.doc_date >= primo_giorno, JournalEntry.doc_date <= ultimo_giorno)
        .scalar()
    )
    costi_totali = Decimal(str(costi_totali or 0))

    overhead_reparto, _ = _pool_reparto_mese(anno, mese)

    rettifiche_rows = OverheadAdjustment.query.filter_by(year=anno, month=mese).all()
    rettifiche_totale = sum((Decimal(str(r.amount or 0)) for r in rettifiche_rows), Decimal("0"))

    totale = costi_totali - overhead_reparto + rettifiche_totale
    return {
        "costi_totali_ge": float(costi_totali),
        "overhead_reparto_dedotto": float(overhead_reparto),
        "rettifiche": float(rettifiche_totale),
        "rettifiche_dettaglio": [{"descrizione": r.description, "importo": float(r.amount)} for r in rettifiche_rows],
        "totale": float(totale),
    }


@production_bp.route("/overhead-generale")
@login_required
def overhead_generale():
    """Endpoint AJAX: mostra il calcolo del Livello 2 per un dato mese (sola lettura)."""
    mese = request.args.get("mese", "").strip()
    try:
        anno, mese_num = (int(x) for x in mese.split("-"))
    except Exception:
        return jsonify({"error": "Seleziona un mese valido."}), 400
    return jsonify(_calcola_overhead_generale(anno, mese_num))


@production_bp.route("/rettifiche", methods=["GET", "POST"])
@login_required
def rettifiche():
    """
    Gestione delle correzioni manuali (ratei/risconti) al calcolo automatico
    dell'overhead generale aziendale (Livello 2) — es. un costo di competenza
    del mese ma non ancora registrato in contabilità (rateo, + ), o un costo
    già registrato ma di competenza futura (risconto, -).
    """
    if request.method == "POST":
        try:
            anno = request.form.get("year", type=int)
            mese = request.form.get("month", type=int)
            importo = Decimal(str(request.form.get("amount", "0")).replace(",", "."))
            descrizione = request.form.get("description", "").strip()
        except Exception:
            flash("Controlla i valori inseriti.", "danger")
            return redirect(url_for("production.rettifiche"))

        if not descrizione or not anno or not mese:
            flash("Descrizione, anno e mese sono obbligatori.", "danger")
            return redirect(url_for("production.rettifiche"))

        db.session.add(OverheadAdjustment(year=anno, month=mese, description=descrizione,
                                           amount=importo, created_by_id=current_user.id))
        db.session.commit()
        flash("Rettifica salvata.", "success")
        return redirect(url_for("production.rettifiche"))

    tutte = OverheadAdjustment.query.order_by(OverheadAdjustment.year.desc(), OverheadAdjustment.month.desc()).all()
    return render_template("production/rettifiche.html", rettifiche=tutte)


@production_bp.route("/rettifiche/<int:adj_id>/elimina", methods=["POST"])
@login_required
def elimina_rettifica(adj_id):
    r = OverheadAdjustment.query.get_or_404(adj_id)
    db.session.delete(r)
    db.session.commit()
    flash("Rettifica eliminata.", "success")
    return redirect(url_for("production.rettifiche"))


@production_bp.route("/margine")
@login_required
def margine():
    """
    Report GESTIONALE (sola lettura, nessuna scrittura contabile): per ogni
    prodotto venduto nel mese, calcola il "costo pieno" alla SAP —
    Costi BOM/manodopera/overhead di reparto (dal COGM, se il prodotto è
    stato fabbricato quel mese) + quota di overhead generale aziendale
    (Livello 2, spalmato sul fatturato di TUTTI i prodotti del mese, non solo
    carpenteria propria) — confrontato col prezzo di vendita per il margine.
    """
    mese = request.args.get("mese", "").strip()
    if not mese:
        oggi = date.today()
        mese = f"{oggi.year:04d}-{oggi.month:02d}"
    try:
        anno, mese_num = (int(x) for x in mese.split("-"))
    except Exception:
        anno, mese_num = date.today().year, date.today().month
        mese = f"{anno:04d}-{mese_num:02d}"

    peso_fatturato = request.args.get("peso_fatturato", type=float)
    if peso_fatturato is None:
        peso_fatturato = 100
    peso_fatturato = max(0, min(100, peso_fatturato))
    peso_fatt = Decimal(str(peso_fatturato)) / 100
    peso_cp = 1 - peso_fatt
    primo_giorno = date(anno, mese_num, 1)
    ultimo_giorno = date(anno, mese_num, calendar.monthrange(anno, mese_num)[1])

    livello2 = _calcola_overhead_generale(anno, mese_num)
    overhead_generale_totale = Decimal(str(livello2["totale"]))

    # Fatturato e quantità vendute nel mese, per TUTTI i prodotti (base di
    # riparto dell'overhead generale — Livello 2, su tutto il fatturato/costo primo).
    righe_vendute = (
        db.session.query(
            Material.id, Material.code, Material.description, Material.sales_price, Material.standard_cost,
            db.func.sum(DeliveryLine.qty).label("qty_venduta"),
            db.func.sum(DeliveryLine.qty * DeliveryLine.price).label("fatturato"),
        )
        .join(DeliveryLine, DeliveryLine.material_id == Material.id)
        .join(Delivery, Delivery.id == DeliveryLine.delivery_id)
        .filter(Delivery.doc_date >= primo_giorno, Delivery.doc_date <= ultimo_giorno)
        .group_by(Material.id, Material.code, Material.description, Material.sales_price, Material.standard_cost)
        .all()
    )
    fatturato_totale_mese = sum((Decimal(str(r.fatturato or 0)) for r in righe_vendute), Decimal("0"))
    costo_primo_map = _costo_primo_mensile_per_materiale(primo_giorno)
    costo_primo_totale_mese = sum((costo_primo_map.get(r.id, Decimal("0")) for r in righe_vendute), Decimal("0"))
    # Stessa rinormalizzazione di _calcola_overhead_da_fatturato: se una delle
    # due basi del mix è a zero questo mese, il suo peso non può restare
    # silenzioso — altrimenti la quota di overhead generale spalmata sui
    # prodotti non arriva mai al 100% del pool (Livello 2).
    avviso_mix = None
    if fatturato_totale_mese <= 0 and costo_primo_totale_mese > 0 and peso_fatt > 0:
        avviso_mix = ("Nessun fatturato nel mese: il mix richiesto è stato rinormalizzato "
                     "al 100% costo primo.")
        peso_fatt, peso_cp = Decimal("0"), Decimal("1")
    elif costo_primo_totale_mese <= 0 and fatturato_totale_mese > 0 and peso_cp > 0:
        avviso_mix = ("Nessun costo primo calcolabile nel mese: il mix richiesto è stato "
                     "rinormalizzato al 100% fatturato.")
        peso_fatt, peso_cp = Decimal("1"), Decimal("0")

    # COGM del mese per i prodotti effettivamente fabbricati (Livello 1 già incluso)
    produzioni_mese = (
        db.session.query(
            ProductionEntry.material_id,
            db.func.sum(ProductionEntry.qty_produced).label("qty_prodotta"),
            db.func.sum(ProductionEntry.raw_material_cost + ProductionEntry.direct_labor_cost
                        + ProductionEntry.overhead_cost).label("cogm_totale"),
        )
        .filter(ProductionEntry.doc_date >= primo_giorno, ProductionEntry.doc_date <= ultimo_giorno)
        .group_by(ProductionEntry.material_id)
        .all()
    )
    cogm_per_materiale = {p.material_id: (Decimal(str(p.qty_prodotta or 0)), Decimal(str(p.cogm_totale or 0)))
                          for p in produzioni_mese}

    righe = []
    for r in righe_vendute:
        qty_venduta = Decimal(str(r.qty_venduta or 0))
        fatturato = Decimal(str(r.fatturato or 0))
        if qty_venduta <= 0:
            continue

        qty_prodotta, cogm_totale = cogm_per_materiale.get(r.id, (None, None))
        if cogm_totale is not None and qty_prodotta and qty_prodotta > 0:
            costo_base_unitario = cogm_totale / qty_prodotta
            fonte_costo = "COGM del mese (materie prime + manodopera + overhead di reparto)"
        else:
            costo_base_unitario = Decimal(str(r.standard_cost or 0))
            fonte_costo = "Costo standard (nessuna produzione registrata questo mese)"

        quota_fatt_pct = (fatturato / fatturato_totale_mese) if fatturato_totale_mese > 0 else Decimal("0")
        cp_materiale = costo_primo_map.get(r.id, Decimal("0"))
        quota_cp_pct = (cp_materiale / costo_primo_totale_mese) if costo_primo_totale_mese > 0 else Decimal("0")
        quota_mix_pct = peso_fatt * quota_fatt_pct + peso_cp * quota_cp_pct

        quota_overhead_generale_unitaria = Decimal("0")
        if overhead_generale_totale != 0:
            quota_overhead_generale = overhead_generale_totale * quota_mix_pct
            quota_overhead_generale_unitaria = quota_overhead_generale / qty_venduta

        costo_pieno_unitario = costo_base_unitario + quota_overhead_generale_unitaria
        prezzo = Decimal(str(r.sales_price or 0))
        margine_unitario = prezzo - costo_pieno_unitario
        margine_pct = (margine_unitario / prezzo * 100) if prezzo > 0 else None

        righe.append({
            "codice": r.code, "descrizione": r.description,
            "qty_venduta": float(qty_venduta), "fatturato": float(fatturato),
            "costo_base_unitario": float(costo_base_unitario), "fonte_costo": fonte_costo,
            "quota_overhead_generale_unitaria": float(quota_overhead_generale_unitaria),
            "costo_pieno_unitario": float(costo_pieno_unitario),
            "prezzo_vendita": float(prezzo),
            "margine_unitario": float(margine_unitario),
            "margine_pct": float(margine_pct) if margine_pct is not None else None,
        })

    righe.sort(key=lambda x: x["fatturato"], reverse=True)
    return render_template("production/margine.html", righe=righe, mese=mese, peso_fatturato=peso_fatturato,
                           livello2=livello2, fatturato_totale_mese=float(fatturato_totale_mese),
                           costo_primo_totale_mese=float(costo_primo_totale_mese), avviso_mix=avviso_mix)


# ══════════════════════════════════════════════════════════════
# ANALISI COSTO PRODOTTO / COSTO TARGET / VARIANZE
# I consuntivi provengono esclusivamente da ProductionEntry: nessun dato viene
# stimato. Gli standard sono quelli agganciati alla registrazione, o (per le
# registrazioni legacy) lo standard applicabile alla data della registrazione.
# ══════════════════════════════════════════════════════════════
def _target_applicabile(material_id, reference_date):
    return (ProductCostTarget.query
            .filter(ProductCostTarget.material_id == material_id,
                    ProductCostTarget.effective_date <= reference_date)
            .order_by(ProductCostTarget.effective_date.desc(), ProductCostTarget.id.desc())
            .first())


def _as_float(value):
    return float(value) if value is not None else None


def _riga_benchmark(label, actual, actual_comparabile_standard, standard, target):
    """'actual' è il totale effettivo (mostrato e usato per il confronto target);
    'actual_comparabile_standard' è il sottoinsieme con standard agganciato,
    usato SOLO per la varianza vs standard — mai mescolato col totale.
    None per entrambi = nessuna registrazione comparabile allo standard: la
    varianza resta esplicitamente non disponibile, mai un finto zero."""
    actual = Decimal(str(actual))
    ha_standard = actual_comparabile_standard is not None and standard is not None
    actual_std = Decimal(str(actual_comparabile_standard)) if ha_standard else None
    standard_dec = Decimal(str(standard)) if ha_standard else None
    target_value = Decimal(str(target)) if target is not None else None
    return {
        "voce": label, "effettivo": _as_float(money(actual)),
        "standard": _as_float(money(standard_dec)) if standard_dec is not None else None,
        "target": _as_float(money(target_value)) if target_value is not None else None,
        "varianza_standard": _as_float(money(variance(actual_std, standard_dec))) if ha_standard else None,
        "varianza_standard_pct": _as_float(variance_pct(actual_std, standard_dec)) if ha_standard else None,
        "varianza_target": _as_float(money(variance(actual, target_value))) if target_value is not None else None,
        "varianza_target_pct": _as_float(variance_pct(actual, target_value)) if target_value is not None else None,
        "classificazione_standard": classification(variance(actual_std, standard_dec)) if ha_standard else None,
        "classificazione_target": classification(variance(actual, target_value)) if target_value is not None else None,
    }


def _analisi_costo_prodotto(material, data_da=None, data_a=None):
    """Build a transparent actual-vs-standard-vs-target report for one SKU.

    IMPORTANTE: le registrazioni senza uno standard agganciabile pesano nel
    totale EFFETTIVO (sono spesa vera) ma sono escluse dal confronto standard
    su ENTRAMBI i lati (effettivo comparabile e standard) — sommare il loro
    costo reale contro uno standard fittizio a zero gonfierebbe la varianza
    in modo scorretto. Il totale escluso resta sempre dichiarato esplicitamente."""
    data_da = data_da or date.min
    data_a = data_a or date.today()
    entries = (ProductionEntry.query
               .filter(ProductionEntry.material_id == material.id,
                       ProductionEntry.doc_date >= data_da,
                       ProductionEntry.doc_date <= data_a)
               .order_by(ProductionEntry.doc_date, ProductionEntry.id).all())
    limits = []
    actual = {"materiali": Decimal("0"), "manodopera": Decimal("0"), "overhead": Decimal("0")}
    actual_comparabile = {"materiali": Decimal("0"), "manodopera": Decimal("0"), "overhead": Decimal("0")}
    standard = {"materiali": Decimal("0"), "manodopera": Decimal("0"), "overhead": Decimal("0")}
    qty = Decimal("0")
    qty_comparabile = Decimal("0")
    n_senza_standard = 0
    effettivo_escluso_dal_confronto = Decimal("0")
    detail = []
    for entry in entries:
        quantity = Decimal(str(entry.qty_produced or 0))
        qty += quantity
        actual_values = (Decimal(str(entry.raw_material_cost or 0)),
                         Decimal(str(entry.direct_labor_cost or 0)),
                         Decimal(str(entry.overhead_cost or 0)))
        for key, value in zip(actual, actual_values):
            actual[key] += value
        sc = entry.standard_cost or _trova_standard_applicabile(
            entry.material_id, entry.doc_date.year, entry.doc_date.month)
        if sc is None:
            n_senza_standard += 1
            effettivo_escluso_dal_confronto += sum(actual_values)
            std_values = (Decimal("0"), Decimal("0"), Decimal("0"))
            standard_available = False
        else:
            std_values = (Decimal(str(sc.standard_material_cost or 0)) * quantity,
                          Decimal(str(sc.standard_labor_cost or 0)) * quantity,
                          Decimal(str(sc.standard_overhead_cost or 0)) * quantity)
            standard_available = True
            for key, value in zip(actual_comparabile, actual_values):
                actual_comparabile[key] += value
            for key, value in zip(standard, std_values):
                standard[key] += value
            qty_comparabile += quantity
        detail.append({
            "numero": entry.doc_number, "data": entry.doc_date.isoformat(),
            "quantita": _as_float(quantity), "standard_disponibile": standard_available,
            "effettivo": {"materiali": _as_float(money(actual_values[0])), "manodopera": _as_float(money(actual_values[1])), "overhead": _as_float(money(actual_values[2]))},
            "standard": {"materiali": _as_float(money(std_values[0])), "manodopera": _as_float(money(std_values[1])), "overhead": _as_float(money(std_values[2]))},
        })
    if n_senza_standard:
        limits.append(
            f"{n_senza_standard} registrazione/i su {len(entries)} senza costo standard agganciabile: "
            f"€ {float(money(effettivo_escluso_dal_confronto)):.2f} di costo effettivo reale sono esclusi "
            f"dal confronto standard su ENTRAMBI i lati (non solo dallo standard), per non gonfiare la "
            f"varianza confrontandoli con uno standard fittizio a zero. Restano inclusi nel totale effettivo.")
    if not entries:
        limits.append("Nessuna Produzione Completata nel periodo: non viene inventato alcun costo effettivo.")
    target = _target_applicabile(material.id, data_a)
    target_values = None
    if target is None:
        limits.append("Nessun costo target valido alla fine del periodo selezionato: il confronto target non è disponibile.")
    else:
        target_values = {"materiali": Decimal(str(target.target_material_cost or 0)) * qty,
                         "manodopera": Decimal(str(target.target_labor_cost or 0)) * qty,
                         "overhead": Decimal(str(target.target_overhead_cost or 0)) * qty}
    actual["totale"] = sum(actual.values())
    actual_comparabile["totale"] = sum(actual_comparabile.values())
    standard["totale"] = sum(standard.values())
    if target_values is not None:
        target_values["totale"] = sum(target_values.values())
    labels = {"materiali": "Materiali", "manodopera": "Manodopera diretta", "overhead": "Overhead", "totale": "TOTALE"}
    ha_confronto_standard = n_senza_standard < len(entries) and entries
    rows = [_riga_benchmark(
                labels[key], actual[key],
                actual_comparabile[key] if ha_confronto_standard else None,
                standard[key] if ha_confronto_standard else None,
                target_values[key] if target_values else None)
            for key in ("materiali", "manodopera", "overhead", "totale")]
    unit = None if qty == 0 else {
        "effettivo": _as_float(money(actual["totale"] / qty)),
        "standard": _as_float(money(standard["totale"] / qty_comparabile)) if ha_confronto_standard and qty_comparabile else None,
        "target": _as_float(money(target_values["totale"] / qty)) if target_values else None,
    }
    total_row = rows[-1]
    summary = []
    if qty:
        summary.append(f"Prodotte {float(qty):g} unità: costo effettivo unitario € {unit['effettivo']:.2f}.")
    if total_row["varianza_standard"] is not None:
        summary.append(f"Varianza vs standard: {'+' if total_row['varianza_standard'] > 0 else ''}€ {total_row['varianza_standard']:.2f} ({total_row['classificazione_standard']}).")
    if total_row["varianza_target"] is not None:
        summary.append(f"Varianza vs target: {'+' if total_row['varianza_target'] > 0 else ''}€ {total_row['varianza_target']:.2f} ({total_row['classificazione_target']}).")
    return {"ok": True, "materiale": {"id": material.id, "codice": material.code, "descrizione": material.description},
            "periodo": {"da": data_da.isoformat() if data_da != date.min else None, "a": data_a.isoformat()},
            "quantita_prodotta": _as_float(qty), "righe": rows, "costo_unitario": unit,
            "target": ({"id": target.id, "decorrenza": target.effective_date.isoformat(),
                        "note": target.notes or ""} if target else None),
            "registrazioni": detail, "limiti": limits, "riepilogo": summary}


@production_bp.route("/analisi-costo-prodotto", methods=["GET", "POST"])
@login_required
def analisi_costo_prodotto():
    if request.method == "POST":
        try:
            material_id = int(request.form.get("material_id", ""))
            effective_date = datetime.strptime(request.form.get("effective_date", ""), "%Y-%m-%d").date()
            material = Material.query.get(material_id)
            if material is None:
                raise ValueError("Articolo non valido.")
            if ProductCostTarget.query.filter_by(material_id=material_id, effective_date=effective_date).first():
                raise ValueError(f"Esiste già un costo target per {material.code} con decorrenza "
                                 f"{effective_date.strftime('%d/%m/%Y')}: scegli un'altra data o modifica quello esistente.")
            target = ProductCostTarget(material_id=material_id, effective_date=effective_date,
                target_material_cost=Decimal(request.form.get("target_material_cost") or "0"),
                target_labor_cost=Decimal(request.form.get("target_labor_cost") or "0"),
                target_overhead_cost=Decimal(request.form.get("target_overhead_cost") or "0"),
                notes=(request.form.get("notes") or "").strip() or None, created_by_id=current_user.id)
            db.session.add(target); db.session.commit()
            flash("Costo target salvato come nuova versione.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        except Exception:
            db.session.rollback()
            flash("Impossibile salvare il costo target: controlla i valori inseriti.", "danger")
        return redirect(url_for("production.analisi_costo_prodotto", material_id=request.form.get("material_id", "")))
    materials = Material.query.filter_by(active=True).order_by(Material.code).all()
    selected_id = request.args.get("material_id", type=int)
    targets = ProductCostTarget.query.order_by(ProductCostTarget.effective_date.desc(), ProductCostTarget.id.desc()).all()
    return render_template("production/analisi_costo_prodotto.html", materials=materials,
                           selected_id=selected_id, targets=targets, today=date.today().isoformat())



@production_bp.get("/api/analisi-costo-prodotto")
@login_required
def api_analisi_costo_prodotto():
    material_id = request.args.get("material_id", type=int)
    material = Material.query.get(material_id)
    if material is None:
        return jsonify(ok=False, error="Seleziona un articolo valido."), 400
    def parse_date(name):
        raw = (request.args.get(name) or "").strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(f"Data {name} non valida (usa AAAA-MM-GG).")
    try:
        start, end = parse_date("da"), parse_date("a")
        if start and end and start > end:
            raise ValueError("La data iniziale non può essere successiva alla data finale.")
        return jsonify(_analisi_costo_prodotto(material, start, end))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400


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

            # ── Costo Standard: se esiste uno standard applicabile per questo
            # materiale/periodo, il magazzino si capitalizza ALLO STANDARD e la
            # differenza col consuntivo va a varianza (Materiali/Manodopera/
            # Overhead) invece che al consuntivo puro come prima. ──
            mese_produzione = request.form.get("mese_produzione", "").strip()  # YYYY-MM
            standard = None
            if mese_produzione:
                try:
                    anno_prod, mese_prod_num = (int(x) for x in mese_produzione.split("-"))
                    standard = _trova_standard_applicabile(material.id, anno_prod, mese_prod_num)
                except Exception:
                    standard = None

            # Se il form ha usato "Calcola da distinta base", conosciamo ESATTAMENTE
            # quali componenti sono stati consumati e quanto — li scarichiamo
            # davvero su MasterLogistic-WMS. Se l'importo è stato inserito a mano,
            # non sappiamo quali SKU scaricare: lo segnaliamo invece di indovinare.
            usa_bom = request.form.get("bom_usata") == "1"
            componenti_da_scaricare = []
            if usa_bom:
                _, dettaglio_bom = _calcola_materie_prime_da_bom(material, qty_produced)
                componenti_da_scaricare = [d for d in dettaglio_bom if d["quantita_necessaria"] > 0]

            # ── Carico del prodotto finito: il magazzino è interno, quindi
            # questo passaggio non dipende più da un sistema esterno
            # raggiungibile o meno — è la stessa transazione DB di tutto
            # il resto (contabilità inclusa nello stesso commit più sotto).
            post_stock_movement(
                material_id=material.id, qty=qty_produced, movement_type="production_receipt",
                source_type="production_entry", source_id=None,
                unit_cost=(totale_cogm / qty_produced).quantize(Decimal("0.0001")),
                notes="Produzione completata", created_by_id=current_user.id,
            )

            journal_lines = []
            variance_materiali = Decimal("0")
            variance_manodopera = Decimal("0")
            variance_overhead = Decimal("0")

            if standard is not None:
                # ── Capitalizzazione ALLO STANDARD + varianze sul consuntivo ──
                std_mat = standard.standard_material_cost * qty_produced
                std_lab = standard.standard_labor_cost * qty_produced
                std_oh = standard.standard_overhead_cost * qty_produced
                std_totale = std_mat + std_lab + std_oh

                journal_lines.append({
                    "account_id": fert_acc.id, "dare": std_totale, "avere": 0,
                    "description": f"Produzione completata {material.code} × {float(qty_produced):.0f} "
                                    f"(a costo STANDARD — {standard.year}/{standard.month:02d})",
                })
                if raw_cost > 0:
                    journal_lines.append({"account_id": roh_acc.id, "dare": 0, "avere": raw_cost,
                                          "description": "Consumo materie prime da produzione (consuntivo)"})
                if (labor_cost + overhead_cost) > 0:
                    journal_lines.append({"account_id": variazione_acc.id, "dare": 0, "avere": labor_cost + overhead_cost,
                                          "description": "Manodopera diretta e costi indiretti capitalizzati (consuntivo)"})

                # Varianza = CONSUNTIVO - STANDARD. Positiva=sfavorevole (Dare
                # sul conto varianza, si è speso più del previsto); negativa =
                # favorevole (Avere, si è speso meno del previsto).
                variance_materiali = (raw_cost - std_mat).quantize(Decimal("0.01"))
                variance_manodopera = (labor_cost - std_lab).quantize(Decimal("0.01"))
                variance_overhead = (overhead_cost - std_oh).quantize(Decimal("0.01"))

                for varianza, codice_conto, nome in (
                    (variance_materiali, "461000", "Materiali"),
                    (variance_manodopera, "462000", "Manodopera"),
                    (variance_overhead, "463000", "Overhead"),
                ):
                    if varianza == 0:
                        continue
                    acc_var = _acc(codice_conto)
                    if varianza > 0:
                        journal_lines.append({"account_id": acc_var.id, "dare": varianza, "avere": 0,
                                              "description": f"Varianza {nome} sfavorevole — {material.code}"})
                    else:
                        journal_lines.append({"account_id": acc_var.id, "dare": 0, "avere": -varianza,
                                              "description": f"Varianza {nome} favorevole — {material.code}"})
            else:
                # ── Nessuno standard applicabile: capitalizzazione al CONSUNTIVO, come prima ──
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
                standard_cost_id=standard.id if standard is not None else None,
                variance_materiali=variance_materiali,
                variance_manodopera=variance_manodopera,
                variance_overhead=variance_overhead,
                created_by_id=current_user.id,
            )
            db.session.add(pe)

            # Scarico componenti da BOM: stessa transazione di tutto il resto
            # (magazzino interno, non più un sistema esterno da richiamare in
            # "best effort") — se un componente non basta, blocca l'intera
            # registrazione invece di lasciare un carico FERT senza scarico.
            for comp in componenti_da_scaricare:
                post_stock_movement(
                    material_id=comp["material_id"], qty=-Decimal(str(comp["quantita_necessaria"])),
                    movement_type="production_issue", source_type="production_entry", source_id=None,
                    unit_cost=comp.get("costo_unitario"), notes=f"Produzione completata {material.code}",
                    created_by_id=current_user.id,
                )

            db.session.commit()
            msg = (f"Produzione registrata: {entry.doc_number} — "
                   f"€ {float(totale_cogm):.2f} capitalizzati a magazzino Prodotti Finiti "
                   f"(caricati {float(qty_produced):.0f} {material.code}).")
            if usa_bom:
                msg += " Componenti scaricati da distinta base."
            elif raw_cost > 0:
                msg += (" ATTENZIONE: materie prime inserite a mano — la giacenza delle "
                        "materie prime NON è stata scaricata (usa \"Calcola da distinta base\" "
                        "per scaricarla automaticamente).")
            flash(msg, "success")
            return redirect(url_for("production.completata"))

        except UnbalancedEntryError as e:
            db.session.rollback()
            flash(str(e), "danger")
        except (ValueError, WarehouseError) as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("production/completata.html", materiali_finiti=materiali_finiti, ultime=ultime)
