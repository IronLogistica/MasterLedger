from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file
from flask_login import login_required, current_user
import io

from extensions import db
from models import OperatingSite, WarehouseArea, StorageLocation, Account

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

# Colori usati sulla mappa a schermo, per tipo di blocco / stato ubicazione —
# stessa palette del resto dell'app (var CSS), niente hardcoded fuori posto.
COLORE_PER_AREA_TYPE = {
    "ROH": "#3b82c4", "FERT": "#2a9d5c", "HALB": "#c9973b",
    "QUAL": "#b8860b", "SCRAP": "#c0392b", "TRANS": "#666",
}
COLORE_PER_STATO = {
    "libero": "#2a9d5c", "occupato": "#c0392b",
    "manutenzione": "#c9973b", "bloccato": "#666",
}


def _toggle_bool(form, name):
    return form.get(name) == "1"


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


@warehouse_bp.route("/sites/<int:site_id>/planimetria/carica", methods=["POST"])
@login_required
def floor_plan_upload(site_id):
    """Carica la planimetria della sede — SOLO in memoria/database, mai su
    disco (il filesystem del container non è persistente). Accetta PNG o
    JPEG, verificati dai byte magici (stesso principio già usato per i PDF
    paghe: mai fidarsi solo dell'estensione del nome file)."""
    site = OperatingSite.query.get_or_404(site_id)
    f = request.files.get("planimetria")
    if not f or not f.filename:
        flash("Seleziona un file immagine.", "danger")
        return redirect(url_for("warehouse.site_map", site_id=site_id))

    payload = f.read()
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        mimetype = "image/png"
    elif payload.startswith(b"\xff\xd8\xff"):
        mimetype = "image/jpeg"
    else:
        flash("Formato non riconosciuto — carica un PNG o un JPEG.", "danger")
        return redirect(url_for("warehouse.site_map", site_id=site_id))
    if len(payload) > 8 * 1024 * 1024:
        flash("Immagine troppo grande (limite 8MB) — comprimila prima di caricarla.", "danger")
        return redirect(url_for("warehouse.site_map", site_id=site_id))

    def _float(name):
        v = request.form.get(name, "").strip()
        try:
            return float(v) if v else None
        except ValueError:
            return None

    site.floor_plan_image = payload
    site.floor_plan_mimetype = mimetype
    # Dimensioni naturali lette dal browser (JS, prima dell'invio) — evita di
    # aggiungere una dipendenza server solo per leggere l'header dell'immagine.
    site.floor_plan_width = _float("width") or 1000
    site.floor_plan_height = _float("height") or 700
    db.session.commit()
    flash("Planimetria caricata.", "success")
    return redirect(url_for("warehouse.site_map", site_id=site_id))


@warehouse_bp.route("/sites/<int:site_id>/planimetria/immagine")
@login_required
def floor_plan_image(site_id):
    site = OperatingSite.query.get_or_404(site_id)
    if not site.ha_planimetria:
        return "", 404
    return send_file(io.BytesIO(site.floor_plan_image), mimetype=site.floor_plan_mimetype)


@warehouse_bp.route("/sites/<int:site_id>/planimetria/rimuovi", methods=["POST"])
@login_required
def floor_plan_remove(site_id):
    site = OperatingSite.query.get_or_404(site_id)
    site.floor_plan_image = None
    site.floor_plan_mimetype = None
    site.floor_plan_width = None
    site.floor_plan_height = None
    db.session.commit()
    flash("Planimetria rimossa.", "info")
    return redirect(url_for("warehouse.site_map", site_id=site_id))


@warehouse_bp.route("/areas/<int:area_id>/posiziona", methods=["POST"])
@login_required
def area_posiziona(area_id):
    """Endpoint minimale usato dal disegno a schermo: aggiorna SOLO
    posizione/ingombro del blocco (mai la struttura), così disegnare un
    rettangolo non rischia di azzerare per sbaglio i toggle già impostati."""
    area = WarehouseArea.query.get_or_404(area_id)
    try:
        pos_x = float(request.form["pos_x"]); pos_y = float(request.form["pos_y"])
        dim_x = float(request.form["dim_x"]); dim_y = float(request.form["dim_y"])
    except (KeyError, ValueError):
        return jsonify({"ok": False, "error": "Coordinate non valide."}), 400
    if dim_x <= 0 or dim_y <= 0:
        return jsonify({"ok": False, "error": "Il rettangolo disegnato è troppo piccolo."}), 400
    area.pos_x, area.pos_y, area.dim_x, area.dim_y = pos_x, pos_y, dim_x, dim_y
    db.session.commit()
    return jsonify({"ok": True, "code": area.code})


@warehouse_bp.route("/sites/<int:site_id>/mappa")
@login_required
def site_map(site_id):
    """Mappa a schermo della sede: ogni Area di Magazzino ("blocco") come
    rettangolo colorato per tipo, posizionato secondo pos_x/pos_y/dim_x/dim_y.
    Solo i blocchi con posizione impostata compaiono sulla mappa."""
    site = OperatingSite.query.get_or_404(site_id)
    blocchi = [a for a in site.warehouse_areas if a.ha_mappa_posizionata]
    non_posizionati = [a for a in site.warehouse_areas if not a.ha_mappa_posizionata]
    return render_template("warehouse/site_map.html", site=site, blocchi=blocchi,
                           non_posizionati=non_posizionati, colori=COLORE_PER_AREA_TYPE,
                           area_types=WarehouseArea.AREA_TYPES)


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

    def _float(name):
        v = request.form.get(name, "").strip().replace(",", ".")
        try:
            return float(v) if v else None
        except ValueError:
            return None

    area_a_terra = _toggle_bool(request.form, "area_a_terra")
    sloc = WarehouseArea(
        site_id=site_id, code=code, name=name or f"Ubicazione {code}",
        area_type=area_type, account_id=account.id if account else None,
        pos_x=_float("pos_x"), pos_y=_float("pos_y"), dim_x=_float("dim_x"), dim_y=_float("dim_y"),
        # Un'area a terra, per definizione, non ha corsie/scaffali/ripiani/cantilever
        # propri — sono mutuamente esclusivi, non serve che l'utente li spenga a mano.
        usa_corsie=False if area_a_terra else _toggle_bool(request.form, "usa_corsie"),
        usa_scaffali=False if area_a_terra else _toggle_bool(request.form, "usa_scaffali"),
        usa_ripiani=False if area_a_terra else _toggle_bool(request.form, "usa_ripiani"),
        usa_cassette=_toggle_bool(request.form, "usa_cassette"),
        usa_cantilever=False if area_a_terra else _toggle_bool(request.form, "usa_cantilever"),
        area_a_terra=area_a_terra,
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


@warehouse_bp.route("/areas/<int:area_id>/struttura", methods=["POST"])
@login_required
def area_struttura(area_id):
    """Aggiorna, in modo indipendente per QUESTO blocco, quali livelli di
    ubicazione usa e la sua posizione/ingombro sulla mappa della sede."""
    area = WarehouseArea.query.get_or_404(area_id)

    def _float(name):
        v = request.form.get(name, "").strip().replace(",", ".")
        try:
            return float(v) if v else None
        except ValueError:
            return None

    area_a_terra = _toggle_bool(request.form, "area_a_terra")
    area.area_a_terra = area_a_terra
    area.usa_corsie = False if area_a_terra else _toggle_bool(request.form, "usa_corsie")
    area.usa_scaffali = False if area_a_terra else _toggle_bool(request.form, "usa_scaffali")
    area.usa_ripiani = False if area_a_terra else _toggle_bool(request.form, "usa_ripiani")
    area.usa_cassette = _toggle_bool(request.form, "usa_cassette")
    area.usa_cantilever = False if area_a_terra else _toggle_bool(request.form, "usa_cantilever")
    area.pos_x, area.pos_y = _float("pos_x"), _float("pos_y")
    area.dim_x, area.dim_y = _float("dim_x"), _float("dim_y")
    db.session.commit()
    flash(f"Struttura del blocco {area.code} aggiornata.", "success")
    return redirect(url_for("warehouse.area_detail", area_id=area.id))


@warehouse_bp.route("/areas/<int:area_id>")
@login_required
def area_detail(area_id):
    """Pagina di gestione di UN blocco: struttura attivabile indipendente,
    ubicazioni al suo interno, mappa a schermo delle sue ubicazioni."""
    area = WarehouseArea.query.get_or_404(area_id)
    ubicazioni = (StorageLocation.query.filter_by(warehouse_area_id=area.id, active=True)
                 .order_by(StorageLocation.codice).all())
    return render_template("warehouse/area_detail.html", area=area, ubicazioni=ubicazioni,
                           colori=COLORE_PER_STATO, stati=StorageLocation.STATI,
                           tipi=StorageLocation.TIPI_STOCCAGGIO)


def _componi_codice(area, corridoio, scaffale, ripiano, cassetta, posizione_libera):
    if area.area_a_terra:
        return posizione_libera.strip().upper() or "P01"
    parti = []
    if area.usa_corsie and corridoio:
        parti.append(f"C{corridoio.strip().upper()}")
    if area.usa_scaffali and scaffale:
        parti.append(f"S{scaffale.strip().upper()}")
    if area.usa_ripiani and ripiano:
        parti.append(f"L{ripiano.strip().upper()}")
    if area.usa_cassette and cassetta:
        parti.append(f"K{cassetta.strip().upper()}")
    return "-".join(parti)


@warehouse_bp.route("/areas/<int:area_id>/ubicazioni/new", methods=["POST"])
@login_required
def location_new(area_id):
    area = WarehouseArea.query.get_or_404(area_id)

    corridoio = request.form.get("corridoio", "").strip()
    scaffale = request.form.get("scaffale", "").strip()
    ripiano = request.form.get("ripiano", "").strip()
    cassetta = request.form.get("cassetta", "").strip()
    posizione_libera = request.form.get("posizione_libera", "").strip()
    tipo_stoccaggio = request.form.get("tipo_stoccaggio", "SCAFFALE")

    # Un blocco accetta solo i tipi di stoccaggio coerenti con la SUA
    # struttura — mai un cantilever in un blocco che non lo prevede.
    if tipo_stoccaggio == "CANTILEVER" and not area.usa_cantilever:
        flash(f"Il blocco {area.code} non ha attivato lo stoccaggio a cantilever — attivalo prima nella struttura.", "danger")
        return redirect(url_for("warehouse.area_detail", area_id=area.id))
    if area.area_a_terra and tipo_stoccaggio not in ("AREA_TERRA", "PALLET"):
        flash(f"Il blocco {area.code} è un'area a terra: solo tipo 'Area a terra' o 'Postazione pallet'.", "danger")
        return redirect(url_for("warehouse.area_detail", area_id=area.id))

    codice = _componi_codice(area, corridoio, scaffale, ripiano, cassetta, posizione_libera)
    if not codice:
        flash("Compila almeno un livello di posizione attivo per questo blocco (o la posizione, per un'area a terra).", "danger")
        return redirect(url_for("warehouse.area_detail", area_id=area.id))
    if StorageLocation.query.filter_by(warehouse_area_id=area.id, codice=codice).first():
        flash(f"L'ubicazione {codice} esiste già in questo blocco.", "danger")
        return redirect(url_for("warehouse.area_detail", area_id=area.id))

    def _float(name, default):
        v = request.form.get(name, "").strip().replace(",", ".")
        try:
            return float(v) if v else default
        except ValueError:
            return default

    loc = StorageLocation(
        warehouse_area_id=area.id, codice=codice,
        corridoio=corridoio or None, scaffale=scaffale or None,
        ripiano=ripiano or None, cassetta=cassetta or None,
        tipo_stoccaggio=tipo_stoccaggio, stato=request.form.get("stato", "libero"),
        pos_x=_float("pos_x", 0), pos_y=_float("pos_y", 0),
        dim_x=_float("dim_x", 100), dim_y=_float("dim_y", 100),
        peso_max_kg=_float("peso_max_kg", None),
        note=request.form.get("note", "").strip() or None,
        created_by_id=current_user.id,
    )
    db.session.add(loc)
    db.session.commit()
    flash(f"Ubicazione {codice} creata nel blocco {area.code}.", "success")
    return redirect(url_for("warehouse.area_detail", area_id=area.id))


@warehouse_bp.route("/ubicazioni/<int:loc_id>/stato", methods=["POST"])
@login_required
def location_stato(loc_id):
    loc = StorageLocation.query.get_or_404(loc_id)
    nuovo_stato = request.form.get("stato", "")
    if nuovo_stato not in StorageLocation.STATI:
        flash("Stato non valido.", "danger")
        return redirect(url_for("warehouse.area_detail", area_id=loc.warehouse_area_id))
    loc.stato = nuovo_stato
    db.session.commit()
    flash(f"Ubicazione {loc.codice} → {loc.stato_label}.", "success")
    return redirect(url_for("warehouse.area_detail", area_id=loc.warehouse_area_id))


@warehouse_bp.route("/ubicazioni/<int:loc_id>/delete", methods=["POST"])
@login_required
def location_delete(loc_id):
    loc = StorageLocation.query.get_or_404(loc_id)
    area_id = loc.warehouse_area_id
    db.session.delete(loc)
    db.session.commit()
    flash("Ubicazione eliminata.", "info")
    return redirect(url_for("warehouse.area_detail", area_id=area_id))


@warehouse_bp.route("/areas/<int:area_id>/ubicazioni/genera-griglia", methods=["POST"])
@login_required
def location_genera_griglia(area_id):
    """Genera in automatico una griglia di ubicazioni (corsie x scaffali x
    ripiani), posizionandole in sequenza sulla mappa del blocco — comodo per
    partire, MAI obbligatorio: le singole ubicazioni restano poi modificabili
    o eliminabili una per una."""
    area = WarehouseArea.query.get_or_404(area_id)
    if area.area_a_terra:
        flash("La generazione a griglia è per blocchi a scaffalatura — un'area a terra si popola a mano (posizioni libere).", "danger")
        return redirect(url_for("warehouse.area_detail", area_id=area.id))

    try:
        n_corsie = max(1, int(request.form.get("n_corsie", "1")))
        n_scaffali = max(1, int(request.form.get("n_scaffali", "1")))
        n_ripiani = max(1, int(request.form.get("n_ripiani", "1"))) if area.usa_ripiani else 1
        larghezza = float(request.form.get("larghezza", "100").replace(",", "."))
        profondita = float(request.form.get("profondita", "80").replace(",", "."))
        corridoio_larghezza = float(request.form.get("corridoio_larghezza", "150").replace(",", "."))
    except ValueError:
        flash("Valori numerici non validi per la griglia.", "danger")
        return redirect(url_for("warehouse.area_detail", area_id=area.id))

    if n_corsie * n_scaffali * n_ripiani > 500:
        flash("Griglia troppo grande in un colpo solo (limite 500 ubicazioni) — generane una parte, poi ripeti.", "danger")
        return redirect(url_for("warehouse.area_detail", area_id=area.id))

    creati, saltati = 0, 0
    for c in range(1, n_corsie + 1):
        x_base = c * (profondita + corridoio_larghezza)
        for s in range(1, n_scaffali + 1):
            y_base = s * (larghezza + 20.0)
            for r in range(1, n_ripiani + 1):
                corridoio = f"{c:02d}"
                scaffale = f"{s:02d}"
                ripiano = f"{r:02d}" if area.usa_ripiani else None
                codice = _componi_codice(area, corridoio, scaffale, ripiano, "", "")
                if StorageLocation.query.filter_by(warehouse_area_id=area.id, codice=codice).first():
                    saltati += 1
                    continue
                db.session.add(StorageLocation(
                    warehouse_area_id=area.id, codice=codice,
                    corridoio=corridoio if area.usa_corsie else None,
                    scaffale=scaffale if area.usa_scaffali else None,
                    ripiano=ripiano, tipo_stoccaggio="SCAFFALE", stato="libero",
                    pos_x=x_base, pos_y=y_base, dim_x=profondita, dim_y=larghezza,
                    created_by_id=current_user.id,
                ))
                creati += 1
    db.session.commit()
    msg = f"Griglia generata: {creati} ubicazioni create."
    if saltati:
        msg += f" {saltati} già esistenti, saltate."
    flash(msg, "success")
    return redirect(url_for("warehouse.area_detail", area_id=area.id))
