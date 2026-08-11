"""
blueprints/materials/routes.py — Anagrafica Articoli (Material Master, MM01)
+ Distinta Base (BOM, magazzino interno).

Ogni articolo porta con sé le due informazioni che fanno funzionare i cicli:
  - costo standard  → usato dal PGI per il Costo del Venduto (SD) e come
                      prezzo proposto negli ordini d'acquisto (MM)
  - prezzo vendita  → proposto in preventivi/ordini cliente
"""
from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user

from extensions import db
from models import Material, BillOfMaterial, BOMComponent
from services.warehouse import post_stock_movement, explode_bom, WarehouseError

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
            qty_iniziale = Decimal(str(request.form.get("qty_on_hand", type=float) or 0))
            m = Material(
                code=code, description=description,
                material_type=request.form.get("material_type", "FERT"),
                uom=request.form.get("uom", "PZ").strip() or "PZ",
                standard_cost=request.form.get("standard_cost", type=float) or 0,
                sales_price=request.form.get("sales_price", type=float) or 0,
                vat_rate=request.form.get("vat_rate", type=float) or 22,
                qty_on_hand=0,
                is_carpenteria_propria=request.form.get("is_carpenteria_propria") == "1",
            )
            db.session.add(m)
            db.session.flush()
            if qty_iniziale > 0:
                # Anche la giacenza di apertura passa dal ledger, mai un
                # numero scritto a fianco senza traccia: così current_stock()
                # (somma dei movimenti) e Material.qty_on_hand (cache) non
                # divergono mai, nemmeno per il primissimo caricamento.
                post_stock_movement(
                    material_id=m.id, qty=qty_iniziale, movement_type="adjustment",
                    source_type="material_opening_balance", source_id=m.id,
                    notes="Giacenza iniziale all'anagrafica articolo", created_by_id=current_user.id,
                )
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
    m.is_carpenteria_propria = request.form.get("is_carpenteria_propria") == "1"
    db.session.commit()
    flash(f"Articolo {m.code} aggiornato (costo standard {float(m.standard_cost):.4f} €, "
          f"prezzo {float(m.sales_price):.2f} €).", "success")
    return redirect(url_for("materials.material_list"))


# ══════════════════════════════════════════════════════════════
# DISTINTA BASE (BOM) — CS01/CS02 semplificato
# ══════════════════════════════════════════════════════════════
@materials_bp.route("/bom", methods=["GET", "POST"])
@login_required
def bom_list():
    parents = Material.query.filter(Material.material_type.in_(["FERT", "HALB"])).order_by(Material.code).all()
    components_available = Material.query.order_by(Material.code).all()

    if request.method == "POST":
        parent_id = request.form.get("parent_material_id", type=int)
        parent = Material.query.get(parent_id)
        if parent is None:
            flash("Seleziona un prodotto padre valido.", "danger")
            return redirect(url_for("materials.bom_list"))

        rows = []
        for i in range(20):
            comp_id = request.form.get(f"comp_{i}", type=int)
            qty_per = request.form.get(f"qty_{i}", "").strip()
            scrap = request.form.get(f"scrap_{i}", "").strip()
            if not comp_id or not qty_per:
                continue
            try:
                qty_per_dec = Decimal(qty_per.replace(",", "."))
                scrap_dec = Decimal(scrap.replace(",", ".")) if scrap else Decimal("0")
            except InvalidOperation:
                flash(f"Riga {i+1}: quantità o scarto non validi.", "danger")
                return redirect(url_for("materials.bom_list"))
            if qty_per_dec <= 0:
                flash(f"Riga {i+1}: la quantità per unità deve essere positiva.", "danger")
                return redirect(url_for("materials.bom_list"))
            if comp_id == parent_id:
                flash(f"Riga {i+1}: un articolo non può essere componente di sé stesso.", "danger")
                return redirect(url_for("materials.bom_list"))
            rows.append((comp_id, qty_per_dec, scrap_dec))

        if not rows:
            flash("Aggiungi almeno un componente.", "danger")
            return redirect(url_for("materials.bom_list"))

        # Nuova versione: la BOM precedente (se attiva) resta per storico/
        # riconciliazione dei consuntivi già registrati, ma non è più quella
        # usata dai nuovi calcoli — stesso principio dei Costi Standard mensili.
        existing = BillOfMaterial.query.filter_by(parent_material_id=parent_id, active=True).all()
        for e in existing:
            e.active = False
        next_version = str(len(BillOfMaterial.query.filter_by(parent_material_id=parent_id).all()) + 1)

        bom = BillOfMaterial(parent_material_id=parent_id, version=next_version, active=True,
                             notes=request.form.get("notes", "").strip(), created_by_id=current_user.id)
        db.session.add(bom)
        db.session.flush()
        for comp_id, qty_per_dec, scrap_dec in rows:
            db.session.add(BOMComponent(bom_id=bom.id, component_material_id=comp_id,
                                        qty_per=qty_per_dec, scrap_pct=scrap_dec))
        db.session.commit()
        flash(f"Distinta base v{next_version} creata per {parent.code} ({len(rows)} componenti).", "success")
        return redirect(url_for("materials.bom_list"))

    boms = (BillOfMaterial.query.filter_by(active=True)
            .join(Material, BillOfMaterial.parent_material_id == Material.id)
            .order_by(Material.code).all())
    return render_template("materials/bom.html", boms=boms, parents=parents,
                           components_available=components_available)


@materials_bp.route("/bom/<int:bom_id>/esplosione")
@login_required
def bom_esplosione(bom_id):
    """Anteprima esplosione multilivello per 1 unità del padre — verifica visiva
    che la BOM (anche annidata su semilavorati) risolva correttamente."""
    bom = BillOfMaterial.query.get_or_404(bom_id)
    try:
        fabbisogno = explode_bom(bom.parent_material_id, Decimal("1"))
    except WarehouseError as e:
        flash(str(e), "danger")
        return redirect(url_for("materials.bom_list"))
    materiali = {m.id: m for m in Material.query.filter(Material.id.in_(fabbisogno.keys())).all()}
    righe = sorted(
        [{"material": materiali[mid], "qty": float(q)} for mid, q in fabbisogno.items()],
        key=lambda r: r["material"].code,
    )
    return render_template("materials/bom_esplosione.html", bom=bom, righe=righe)
