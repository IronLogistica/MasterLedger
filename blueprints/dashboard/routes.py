from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from extensions import db
from models import JournalEntry, FiscalParameter
from blueprints.decorators import commercialista_required

dashboard_bp = Blueprint("dashboard", __name__, template_folder="../../templates/dashboard")


@dashboard_bp.route("/")
@login_required
def home():
    recent_entries = JournalEntry.query.order_by(JournalEntry.created_at.desc()).limit(10).all()
    totale_dare = sum(e.total_dare for e in JournalEntry.query.all())
    totale_avere = sum(e.total_avere for e in JournalEntry.query.all())
    return render_template(
        "dashboard/home.html",
        recent_entries=recent_entries,
        totale_dare=totale_dare,
        totale_avere=totale_avere,
    )


# ══════════════════════════════════════════════════════════════
# PANNELLO CONFIGURAZIONE FISCALE — solo ruolo 'commercialista'
# ══════════════════════════════════════════════════════════════
FISCAL_PARAM_DEFAULTS = [
    # key, default value, description, category
    ("ammortamento_metodo", "lineare", "Metodo di ammortamento predefinito (lineare / accelerato)", "ammortamenti"),
    ("ammortamento_aliquota_impianti", "10", "Aliquota % ammortamento impianti e macchinari", "ammortamenti"),
    ("ammortamento_aliquota_attrezzature", "15", "Aliquota % ammortamento attrezzature", "ammortamenti"),
    ("magazzino_metodo_valutazione", "costo_medio_ponderato", "Metodo di valutazione rimanenze (FIFO / LIFO / costo_medio_ponderato)", "magazzino"),
    ("crediti_svalutazione_forfettaria", "0.5", "% svalutazione forfettaria crediti (default civilistico)", "crediti"),
    ("iva_aliquota_ordinaria", "22", "Aliquota IVA ordinaria %", "iva"),
    ("iva_split_payment_attivo", "no", "Split payment attivo per PA (si/no)", "iva"),

    # ── Dati Cedente/Prestatore per la generazione dell'XML FatturaPA
    # (services/fatturapa.py). Vanno compilati una sola volta: da qui in
    # poi ogni fattura Fattura cliente può generare il proprio XML pronto per
    # l'upload su Aruba (o altro intermediario) per firma e invio.
    ("fe_denominazione", "", "Ragione sociale (Cedente/Prestatore)", "fatturazione elettronica"),
    ("fe_piva", "", "Partita IVA (senza prefisso IT)", "fatturazione elettronica"),
    ("fe_codice_fiscale", "", "Codice Fiscale (se diverso dalla P.IVA)", "fatturazione elettronica"),
    ("fe_regime_fiscale", "RF01", "Regime Fiscale (es. RF01 = Ordinario, RF19 = Forfettario)", "fatturazione elettronica"),
    ("fe_indirizzo", "", "Indirizzo sede legale", "fatturazione elettronica"),
    ("fe_cap", "", "CAP sede legale", "fatturazione elettronica"),
    ("fe_comune", "", "Comune sede legale", "fatturazione elettronica"),
    ("fe_provincia", "", "Provincia sede legale (sigla, es. PG)", "fatturazione elettronica"),
    ("fe_nazione", "IT", "Nazione sede legale (codice ISO, es. IT)", "fatturazione elettronica"),
    ("fe_iban", "", "IBAN per DatiPagamento in fattura (opzionale)", "fatturazione elettronica"),
]


@dashboard_bp.route("/config-fiscale", methods=["GET", "POST"])
@login_required
@commercialista_required
def config_fiscale():
    # Assicura che tutti i parametri di default esistano (idempotente)
    for key, default_value, desc, category in FISCAL_PARAM_DEFAULTS:
        if not FiscalParameter.query.filter_by(key=key).first():
            db.session.add(FiscalParameter(key=key, value=default_value, description=desc, category=category))
    db.session.commit()

    if request.method == "POST":
        params = FiscalParameter.query.all()
        for p in params:
            new_value = request.form.get(p.key)
            if new_value is not None:
                p.value = new_value
                p.updated_by_id = current_user.id
        db.session.commit()
        flash("Parametri fiscali aggiornati. Le prossime registrazioni li useranno automaticamente.", "success")
        return redirect(url_for("dashboard.config_fiscale"))

    params_by_category = {}
    for p in FiscalParameter.query.order_by(FiscalParameter.category).all():
        params_by_category.setdefault(p.category, []).append(p)

    return render_template("dashboard/config_fiscale.html", params_by_category=params_by_category)


# ══════════════════════════════════════════════════════════════
# AMBIENTE DI TEST SdI — cliente finto + prodotto finto + cedente di prova
# ══════════════════════════════════════════════════════════════
# Un click prepara tutto quello che serve per generare un XML FatturaPA
# da collaudare con il software di controllo dell'Agenzia delle Entrate
# (https://fatturapa.gov.it → "Controlla la Fattura"), SENZA usare dati veri.
#
# Le P.IVA di test superano l'algoritmo di CHECKSUM ufficiale (altrimenti
# il validatore scarterebbe il file ancora prima di guardare il tracciato):
#   - Cedente:  12345678903  (la classica P.IVA di collaudo)
#   - Cliente:  99999999990
# Nessuna delle due corrisponde a un'azienda reale.
#
# ATTENZIONE: il file generato con questi dati serve SOLO per la validazione
# formale — NON va mai firmato né trasmesso allo SdI.

# Valori segnaposto del seed da considerare "vuoti": la P.IVA 01234567890
# ha il CHECKSUM SBAGLIATO (cifra di controllo attesa: 7) — lo SdI la
# scarterebbe con errore 00301 ancora prima del contenuto.
PLACEHOLDER_VALUES = {"01234567890"}

TEST_CEDENTE_VALUES = {
    "fe_denominazione": "IRON SEGNALETICA (AMBIENTE TEST)",
    "fe_piva": "12345678903",
    "fe_codice_fiscale": "12345678903",
    "fe_regime_fiscale": "RF01",
    "fe_indirizzo": "Via del Collaudo, 1",
    "fe_cap": "06012",
    "fe_comune": "Citta di Castello",
    "fe_provincia": "PG",
    "fe_nazione": "IT",
    "fe_iban": "IT60X0542811101000000123456",
}


@dashboard_bp.route("/ambiente-test", methods=["POST"])
@login_required
@commercialista_required
def ambiente_test():
    from models import EconomicSubject, Material

    azioni = []

    # 1) Cedente: riempi SOLO i parametri fe_* ancora vuoti (mai sovrascrivere
    #    dati reali già inseriti — se ci sono, l'XML esce con quelli).
    riempiti, lasciati = [], []
    for key, test_value in TEST_CEDENTE_VALUES.items():
        p = FiscalParameter.query.filter_by(key=key).first()
        if p is None:
            defaults = {k: (d, c) for k, v, d, c in FISCAL_PARAM_DEFAULTS}
            desc, cat = defaults.get(key, ("", "fatturazione elettronica"))
            p = FiscalParameter(key=key, value=test_value, description=desc, category=cat)
            db.session.add(p)
            riempiti.append(key)
        elif (not (p.value or "").strip()
              or p.value.strip() in PLACEHOLDER_VALUES
              or (p.value == "RF01" and key == "fe_regime_fiscale")):
            p.value = test_value
            p.updated_by_id = current_user.id
            if key != "fe_regime_fiscale":
                riempiti.append(key)
        else:
            lasciati.append(key)
    if riempiti:
        azioni.append(f"Cedente di prova: compilati {len(riempiti)} parametri vuoti")
    if lasciati:
        azioni.append(f"{len(lasciati)} parametri cedente già compilati e NON toccati")

    # 2) Cliente finto (P.IVA con checksum valido, SDI 0000000)
    test_cli = EconomicSubject.query.filter_by(piva="99999999990").first()
    if test_cli is None:
        code = "TEST001"
        if EconomicSubject.query.filter_by(code=code).first():
            code = f"TEST{EconomicSubject.query.count() + 1:03d}"
        test_cli = EconomicSubject(
            code=code, name="CLIENTE TEST SRL", subject_type="azienda",
            is_customer=True, piva="99999999990", codice_fiscale="99999999990",
            indirizzo="Via delle Prove, 1", cap="20145", comune="Milano",
            provincia="MI", nazione="IT", codice_destinatario="0000000",
            payment_terms="Netto 30gg",
        )
        db.session.add(test_cli)
        azioni.append(f"Cliente finto {code} — CLIENTE TEST SRL (P.IVA 99999999990) creato")
    else:
        test_cli.is_customer = True
        test_cli.active = True
        azioni.append(f"Cliente finto {test_cli.code} già presente")

    # 3) Prodotto finto con giacenza abbondante (per DDT senza pensieri)
    test_mat = Material.query.filter_by(code="SEGN-TEST").first()
    if test_mat is None:
        test_mat = Material(
            code="SEGN-TEST", description="Cartello di prova (ambiente test)",
            material_type="FERT", uom="PZ", standard_cost=10, sales_price=25,
            vat_rate=22, qty_on_hand=999,
        )
        db.session.add(test_mat)
        azioni.append("Prodotto finto SEGN-TEST creato (costo 10, prezzo 25, giacenza 999)")
    else:
        if float(test_mat.qty_on_hand or 0) < 100:
            test_mat.qty_on_hand = 999
        azioni.append("Prodotto finto SEGN-TEST già presente (giacenza ripristinata)")

    db.session.commit()
    for a in azioni:
        flash(a, "success")
    flash("AMBIENTE TEST PRONTO. Percorso consigliato per il collaudo XML (non tocca il magazzino WMS): "
          "Ciclo Cliente → Fattura cliente → seleziona CLIENTE TEST SRL, una riga "
          "'Cartello di prova SEGN-TEST', imponibile 250, IVA 22 → registra → dal dettaglio scarica l'XML → "
          "caricalo su 'fatturapa.gov.it → Controlla la Fattura'. In alternativa la catena Preventivo→DDT→Fattura "
          "funziona solo se SEGN-TEST ha giacenza su MasterLogistic-WMS. "
          "NON firmare né trasmettere: è un file di collaudo.", "warning")
    return redirect(url_for("dashboard.config_fiscale"))
