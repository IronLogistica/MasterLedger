"""
seed.py — Popola il database con i dati minimi per partire subito.

Uso:
    flask --app app seed

Crea:
  - Piano dei Conti essenziale (patrimoniali + economici, con cost_relevant
    impostato correttamente sui conti di costo/ricavo)
  - Due utenti demo: uno 'operatore', uno 'commercialista'
    (username/password stampati a schermo — CAMBIALI subito in produzione)
  - Una sede operativa e 3 aree di magazzino di esempio (il Setup Magazzini già pronto)
  - Un paio di fornitori/clienti e un centro di costo, per poter provare
    subito Fattura fornitore/Fattura cliente/Prima nota senza dover compilare tutto da zero
"""
from extensions import db
from models import (
    Account, User, OperatingSite, WarehouseArea, Vendor, Customer, CostCenter,
    DocumentSequence, FiscalParameter,
)


ACCOUNTS = [
    # code,     name,                                   type,                    cost_relevant, co_type
    ("150000", "Magazzino Materie Prime e Merci",        "patrimoniale_attivo",   False, None),
    ("152000", "Magazzino Blocco Qualità",                "patrimoniale_attivo",   False, None),
    ("155000", "Magazzino Semilavorati",                  "patrimoniale_attivo",   False, None),
    ("160000", "Magazzino Prodotti Finiti",                "patrimoniale_attivo",   False, None),
    ("140000", "Crediti v/Clienti (AR)",                   "patrimoniale_attivo",   False, None),
    ("154000", "IVA a Credito",                             "patrimoniale_attivo",   False, None),
    ("180000", "Banca c/c",                                 "patrimoniale_attivo",   False, None),
    ("200000", "Cespiti — Impianti e Macchinari",           "patrimoniale_attivo",   False, None),
    ("210000", "Debiti v/Fornitori (AP)",                   "patrimoniale_passivo",  False, None),
    ("170000", "IVA a Debito",                              "patrimoniale_passivo",  False, None),
    ("018000", "Fondo Ammortamento",                        "patrimoniale_passivo",  False, None),
    ("590000", "Perdite su Magazzino (Scarti)",              "costo",                 True,  "COST"),
    ("400000", "Costi per Materie Prime e Consumo",          "costo",                 True,  "COST"),
    ("520000", "Ammortamenti",                               "costo",                 True,  "COST"),
    ("620000", "Costi di Manutenzione",                      "costo",                 True,  "COST"),
    ("640000", "Costi di Trasporto",                          "costo",                 True,  "COST"),
    ("4000",   "Ricavi di Vendita Prodotti",                 "ricavo",                True,  "REVENUE"),
]


def run_seed():
    # ── Piano dei Conti ──────────────────────────────────────────
    for code, name, acc_type, co_rel, co_type in ACCOUNTS:
        if not Account.query.filter_by(code=code).first():
            db.session.add(Account(
                code=code, name=name, account_type=acc_type,
                cost_relevant=co_rel, cost_relevant_type=co_type,
            ))

    # ── Numerazione documenti ────────────────────────────────────
    sequences = [
        ("SA", "10"), ("KR", "19"), ("DR", "14"), ("KZ", "15"),
        ("DZ", "16"), ("Cespiti", "20"), ("AF", "21"),
    ]
    for doc_type, prefix in sequences:
        if not DocumentSequence.query.filter_by(doc_type=doc_type).first():
            db.session.add(DocumentSequence(doc_type=doc_type, prefix=prefix, current_number=0))

    # ── Utenti demo (DA CAMBIARE prima di un uso reale) ─────────
    if not User.query.filter_by(username="operatore").first():
        u1 = User(username="operatore", full_name="Mario Rossi (Operatore)", role="operatore")
        u1.set_password("operatore123")
        db.session.add(u1)

    if not User.query.filter_by(username="commercialista").first():
        u2 = User(username="commercialista", full_name="Dott.ssa Bianchi (Commercialista)", role="commercialista")
        u2.set_password("commercialista123")
        db.session.add(u2)

    # ── Sede operativa e aree di magazzino di esempio (Setup Magazzini) ───
    db.session.flush()
    if not OperatingSite.query.filter_by(code="1000").first():
        site = OperatingSite(code="1000", name="Stabilimento Milano (Sede)", city="Milano", region="Lombardia, IT")
        db.session.add(site)
        db.session.flush()

        acc_roh = Account.query.filter_by(code="150000").first()
        acc_fert = Account.query.filter_by(code="160000").first()
        acc_qual = Account.query.filter_by(code="152000").first()

        db.session.add_all([
            WarehouseArea(site_id=site.id, code="0001", name="Magazzino Materie Prime", area_type="ROH", account_id=acc_roh.id if acc_roh else None),
            WarehouseArea(site_id=site.id, code="0002", name="Magazzino Prodotti Finiti", area_type="FERT", account_id=acc_fert.id if acc_fert else None),
            WarehouseArea(site_id=site.id, code="0003", name="Blocco Qualità", area_type="QUAL", account_id=acc_qual.id if acc_qual else None),
        ])

    # ── Fornitori / Clienti / Centro di costo demo ──────────────
    if not Vendor.query.filter_by(code="FORN-001").first():
        db.session.add(Vendor(code="FORN-001", name="Acciai Lombardi SpA", piva="02345678901", payment_terms="Netto 30gg"))
    if not Customer.query.filter_by(code="CUST-001").first():
        db.session.add(Customer(
            code="CUST-001", name="Ferrari Meccanica SpA", piva="03456789012", payment_terms="Netto 30gg",
            # Dati fiscali di esempio, così Fattura cliente → XML FatturaPA funziona
            # subito in demo (CAMBIA con i dati reali del cliente).
            codice_fiscale="03456789012", indirizzo="Via dell'Industria 45", cap="41100",
            comune="Modena", provincia="MO", nazione="IT",
            codice_destinatario="0000000", pec_destinatario="amministrazione@pec.ferrarimeccanica.it",
        ))
    if not CostCenter.query.filter_by(code="CC-AMM-01").first():
        db.session.add(CostCenter(code="CC-AMM-01", name="Amministrazione"))
    if not CostCenter.query.filter_by(code="CC-PROD-01").first():
        db.session.add(CostCenter(code="CC-PROD-01", name="Produzione"))

    # ── Profilo Cedente/Prestatore per XML FatturaPA — dati di ESEMPIO ──
    # (vedi services/fatturapa.py e dashboard/routes.py). Da correggere
    # in Configurazione Fiscale → "Fatturazione elettronica" con i dati
    # reali dell'azienda prima di un uso reale. Riusiamo le descrizioni
    # ufficiali da dashboard/routes.py per non tenerle duplicate qui.
    from blueprints.dashboard.routes import FISCAL_PARAM_DEFAULTS

    FE_DEMO_VALUES = {
        "fe_denominazione": "AlfaMeccanica SpA",
        "fe_piva": "01234567890",
        "fe_codice_fiscale": "01234567890",
        "fe_regime_fiscale": "RF01",
        "fe_indirizzo": "Via delle Officine 1",
        "fe_cap": "20100",
        "fe_comune": "Milano",
        "fe_provincia": "MI",
        "fe_nazione": "IT",
    }
    for key, default_value, desc, category in FISCAL_PARAM_DEFAULTS:
        if category != "fatturazione elettronica":
            continue
        if not FiscalParameter.query.filter_by(key=key).first():
            db.session.add(FiscalParameter(
                key=key, value=FE_DEMO_VALUES.get(key, default_value),
                description=desc, category=category,
            ))

    db.session.commit()

    print("\n── Utenti demo creati (CAMBIA LE PASSWORD prima di un uso reale) ──")
    print("  Operatore:      username=operatore      password=operatore123")
    print("  Commercialista: username=commercialista  password=commercialista123\n")
