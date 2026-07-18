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
