"""
seed.py — Popola il database con i dati minimi per partire subito.

Uso:
    flask --app app seed

Crea:
  - Piano dei Conti essenziale (patrimoniali + economici, con cost_relevant
    impostato correttamente sui conti di costo/ricavo)
  - Due utenti: Angelo (operatore) e Maurizio (commercialista)
  - Una sede operativa e 3 aree di magazzino di esempio (il Setup Magazzini già pronto)
  - Un paio di fornitori/clienti e un centro di costo, per poter provare
    subito Fattura fornitore/Fattura cliente/Prima nota senza dover compilare tutto da zero
"""
from extensions import db
from models import (
    Account, User, OperatingSite, WarehouseArea, EconomicSubject, CostCenter,
    DocumentSequence, FiscalParameter, PayrollAccountConfig,
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
    # Rinominato (era "Ricavi di Vendita Prodotti"): Iron Appalti fattura
    # lavorazioni in SUBAPPALTO per conto di un appaltatore principale — il
    # CODICE resta 4000 (referenziato da blueprints/sd/routes.py), cambia
    # solo l'etichetta. Il ricavo da AFFIDAMENTO DIRETTO di grandi
    # committenti (senza appaltatore intermedio) va invece sul conto 4001
    # qui sotto — la scelta tra i due è automatica in base al campo
    # EconomicSubject.revenue_channel del cliente (vedi blueprints/parties).
    ("4000",   "Ricavi per Lavorazioni in Subappalto",   "ricavo",                True,  "REVENUE"),
    ("4001",   "Ricavi per Lavorazioni in Affidamento Diretto (Grandi Committenti)", "ricavo", True, "REVENUE"),
    ("450000", "Costo del Venduto",                           "costo",                 True,  "COST"),
    ("165000", "Ricevimenti da fatturare (EM/RF)",            "patrimoniale_passivo",  False, None),
    ("430000", "Variazione Rimanenze Prodotti Finiti",         "ricavo",                True,  "REVENUE"),
    ("460000", "Varianza Prezzo Materiali",                    "costo",                 True,  "COST"),
    ("461000", "Varianza Materiali (Produzione)",               "costo",                 True,  "COST"),
    ("462000", "Varianza Manodopera (Produzione)",              "costo",                 True,  "COST"),
    ("463000", "Varianza Overhead (Produzione)",                "costo",                 True,  "COST"),
    # I 4 conti seguenti esistevano già nel DB (inseriti dalla migrazione
    # 6g7h8i9j0k1l_commesse_wip_rfq.py con INSERT diretto, non da questo
    # seed) — li esplicito anche qui perché seed.py resti la fonte di
    # verità COMPLETA del piano dei conti: sono quelli che reggono il ciclo
    # MIGO (Entrata Merci)/MIRO (Verifica Fattura) e il costo standard con
    # varianze di produzione (vedi blueprints/mm, blueprints/operations).
    ("157000", "Produzione in Corso (WIP)",                     "patrimoniale_attivo",  False, None),
    ("464000", "Varianza di Produzione",                         "costo",                True,  "COST"),
    ("472000", "Manodopera Diretta Assorbita",                   "ricavo",                True,  "REVENUE"),
    ("473000", "Overhead Industriali Assorbiti",                 "ricavo",                True,  "REVENUE"),

    # ── Estensione piano dei conti "standard IRON APPALTI" ──────────
    # Aggiunta a supporto di: 1) Prima Nota manuale/AI per i documenti
    # che NON passano da un ciclo automatico (utenze, notule, rimborsi,
    # assestamenti di fine anno); 2) modulo Paghe (vedi PayrollAccountConfig
    # più sotto, obbligatorio perché il modulo Paghe funzioni).
    ("100000", "Cassa Contanti",                              "patrimoniale_attivo",   False, None),
    ("190000", "Crediti v/Erario c/IVA (credito da riportare)", "patrimoniale_attivo", False, None),
    ("191000", "Risconti Attivi",                              "patrimoniale_attivo",   False, None),
    ("192000", "Fatture da Emettere",                          "patrimoniale_attivo",   False, None),

    ("220000", "Debiti v/Professionisti",                      "patrimoniale_passivo",  False, None),
    ("221000", "Debiti v/Erario c/Ritenute su Compensi Professionali", "patrimoniale_passivo", False, None),
    ("222000", "Debiti v/Dipendenti c/Retribuzioni",           "patrimoniale_passivo",  False, None),
    ("223000", "Debiti v/INPS",                                "patrimoniale_passivo",  False, None),
    ("224000", "Debiti Tributari — Ritenute Lavoro Dipendente", "patrimoniale_passivo", False, None),
    ("225000", "Debiti Tributari per Imposte dell'Esercizio (IRES/IRAP)", "patrimoniale_passivo", False, None),
    ("226000", "TFR — Fondo Trattamento di Fine Rapporto",     "patrimoniale_passivo",  False, None),
    ("227000", "Ratei Passivi",                                "patrimoniale_passivo",  False, None),
    ("228000", "Fatture da Ricevere",                          "patrimoniale_passivo",  False, None),
    ("229000", "Fondo Svalutazione Crediti",                   "patrimoniale_passivo",  False, None),

    ("300000", "Capitale Sociale",                             "patrimoniale_passivo",  False, None),
    ("301000", "Riserva Legale",                               "patrimoniale_passivo",  False, None),
    ("302000", "Utili Portati a Nuovo",                        "patrimoniale_passivo",  False, None),
    ("303000", "Utile d'Esercizio",                             "patrimoniale_passivo",  False, None),
    ("304000", "Perdita d'Esercizio",                           "patrimoniale_attivo",   False, None),

    ("610000", "Costi per Lavorazioni e Subappalti",            "costo",                 True,  "COST"),
    ("645000", "Costi Telefonici e Trasmissione Dati",           "costo",                 True,  "COST"),
    ("646000", "Costi per Energia Elettrica",                    "costo",                 True,  "COST"),
    ("647000", "Premi Assicurativi",                             "costo",                 True,  "COST"),
    ("648000", "Interessi Passivi Bancari",                      "costo",                 True,  "COST"),
    ("650000", "Compensi a Professionisti",                      "costo",                 True,  "COST"),
    ("651000", "Rimborsi Spese a Dipendenti e Collaboratori",     "costo",                 True,  "COST"),
    ("660000", "Salari e Stipendi",                               "costo",                 True,  "COST"),
    ("661000", "Oneri Sociali (INPS/INAIL a carico azienda)",     "costo",                 True,  "COST"),
    ("662000", "Accantonamento TFR",                             "costo",                 True,  "COST"),
    ("663000", "Svalutazione Crediti",                           "costo",                 True,  "COST"),
    ("664000", "Imposte sul Reddito d'Esercizio (IRES/IRAP)",     "costo",                 True,  "COST"),

    # ── Estensione: conti degli Schemi 1-13 (Prima Nota libera / Sezione 13
    # Rettifiche) — services/classificazione_operazioni.py e
    # services/rettifiche_operazioni.py referenziano questi codici, presi dal
    # piano dei conti REALE del commercialista (~795 conti), mai importato
    # finora in questo seed. ATTENZIONE: qui sotto tipo/nome sono stati
    # DEDOTTI automaticamente dal nome e dal verso (Dare/Avere) usati negli
    # schemi, NON confermati riga per riga dal commercialista — usarli come
    # base di lavoro, non come piano dei conti definitivo. Verificare prima
    # di un vero go-live con dati reali.
    ("013203",   "Costi per migliorie beni di terzi",       "costo",                  True, "COST"),
    ("014003",   "Macchinari specifici",                    "patrimoniale_attivo",    False, None),
    ("014303",   "F.do amm. macchinari specifici",          "patrimoniale_passivo",   False, None),
    ("014401",   "F.do sval. impianti generici (o coppia coerente col bene)","patrimoniale_attivo",    False, None),
    ("015700",   "Crediti v/controllate entro es.",         "patrimoniale_attivo",    False, None),
    ("015750",   "Crediti v/controllate oltre es.",         "patrimoniale_attivo",    False, None),
    ("016301",   "Depositi cauz. a fornitori",              "costo",                  True, "COST"),
    ("017001",   "Rim. materie prime (finali, per categoria)","costo",                  True, "COST"),
    ("017301",   "Rim. lavori in corso su ordin.",          "costo",                  True, "COST"),
    ("017401",   "Rim. merci per la vendita (o categoria verificata)","costo",                  True, "COST"),
    ("030001",   "F.do sval.cred.v/clienti/breve",          "costo",                  True, "COST"),
    ("030354",   "Cred. erario c/iva da compens. (saldo, se iva a credito)","patrimoniale_passivo",   False, None),
    ("030393",   "Ires da compensare (o il credito usato: 030364 inps, 030354 iva...)","patrimoniale_attivo",    False, None),
    ("030552",   "Forn. nota accr. da ricevere",            "costo",                  True, "COST"),
    ("030553",   "Crediti commerciali diversi (solo se è il credito in valuta specifico)","patrimoniale_attivo",    False, None),
    ("032003",   "Intesa San Paolo c/c",                    "patrimoniale_attivo",    False, None),
    ("032004",   "PayPal",                                  "patrimoniale_attivo",    False, None),
    ("032601",   "Cassa Contanti (Iron Appalti)",           "patrimoniale_attivo",    False, None),
    ("032701",   "Ratei attivi",                            "costo",                  True, "COST"),
    ("032801",   "Risconti attivi",                         "costo",                  True, "COST"),
    ("032802",   "Canoni anticipati leasing",               "patrimoniale_attivo",    False, None),
    ("032804",   "Clienti c/fatture da emettere",           "patrimoniale_attivo",    False, None),
    ("033301",   "Riserva legale (o 033501 riserva straordinaria)","patrimoniale_passivo",   False, None),
    ("033502",   "Versam.in conto aumen.capitale (o 033503 copertura perdite)","patrimoniale_passivo",   False, None),
    ("033621",   "Utili esercizi precedenti",               "costo",                  True, "COST"),
    ("033651",   "Utile d'esercizio",                       "patrimoniale_passivo",   False, None),
    ("034102",   "F.do imposte differite",                  "costo",                  True, "COST"),
    ("034205",   "F.do controversie legali (utilizzo)",     "costo",                  True, "COST"),
    ("034301",   "F.do tratt.fine rapp. tfr",               "patrimoniale_passivo",   False, None),
    ("044001",   "Fatture da ricevere a breve",             "patrimoniale_passivo",   False, None),
    ("044202",   "Effetti Passivi",                         "patrimoniale_passivo",   False, None),
    ("044401",   "Debiti a breve v/controllate",            "patrimoniale_passivo",   False, None),
    ("044430",   "Debiti a lungo v/controllate",            "patrimoniale_passivo",   False, None),
    ("044604",   "Debito iva da versare (saldo, se iva a debito)","patrimoniale_passivo",   False, None),
    ("044610",   "Debito irap a saldo",                     "patrimoniale_passivo",   False, None),
    ("044614",   "Debito ires a saldo",                     "patrimoniale_passivo",   False, None),
    ("044692",   "Debiti v/INPS c/contributi",              "patrimoniale_passivo",   False, None),
    ("044902",   "Debiti v/inail",                          "patrimoniale_passivo",   False, None),
    ("044915",   "Debiti v/Cassa Edile",                    "patrimoniale_passivo",   False, None),
    ("044960",   "Debiti v/Fondo TFR Alleanza Previdenza",  "patrimoniale_passivo",   False, None),
    ("045001",   "Debiti v/Erario c/IVA",                   "patrimoniale_passivo",   False, None),
    ("045005",   "Iva acquisti",                            "costo",                  True, "COST"),
    ("045006",   "Iva vendite",                             "costo",                  True, "COST"),
    ("045202",   "Clienti nota accr. da emettere (totale)", "costo",                  True, "COST"),
    ("045501",   "Ratei passivi",                           "costo",                  True, "COST"),
    ("045551",   "Risconti passivi",                        "costo",                  True, "COST"),
    ("050501",   "Rim.fin.in corso su ordinaz.",            "costo",                  True, "COST"),
    ("050702",   "Affitti attivi",                          "costo",                  True, "COST"),
    ("051609",   "Resi su vendite (o 057039 sconti su vendite, per sconti)","costo",                  True, "COST"),
    ("051802",   "Prestazioni di servizi",                  "costo",                  True, "COST"),
    ("054002",   "Merci c/acquisti (imponibile)",           "costo",                  True, "COST"),
    ("054607",   "Multe e Sanzioni (indeducibile)",         "costo",                  True, "COST"),
    ("055007",   "Manut.e rip. su beni di prop.",           "costo",                  True, "COST"),
    ("055305",   "Costo di competenza documentato",         "costo",                  True, "COST"),
    ("055307",   "Altre utenze (o il conto errato effettivamente usato)","costo",                  True, "COST"),
    ("056002",   "Affitti passivi (o costo pluriperiodale coerente)","costo",                  True, "COST"),
    ("056003",   "Canoni leasing iva deducibile",           "costo",                  True, "COST"),
    ("056201",   "Contributi inps (costi - oneri sociali)", "costo",                  True, "COST"),
    ("056202",   "Oneri per contributi inail",              "costo",                  True, "COST"),
    ("056242",   "Accant. tfr dell'anno",                   "patrimoniale_passivo",   False, None),
    ("056550",   "Svalutaz. beni ammortizzabili",           "costo",                  True, "COST"),
    ("056602",   "Altre svalutazioni",                      "costo",                  True, "COST"),
    ("056801",   "Rim. fin. materie prime",                 "costo",                  True, "COST"),
    ("057019",   "Imposte di Bollo",                        "costo",                  True, "COST"),
    ("057036",   "Iva indetr. pro-rata (o il costo originario pertinente)","costo",                  True, "COST"),
    ("065401",   "Arrotondamenti attivi",                   "costo",                  True, "COST"),
    ("065404",   "Interessi attivi c/c bancari",            "patrimoniale_attivo",    False, None),
    ("070005",   "Interessi passivi c/c bancari",           "patrimoniale_attivo",    False, None),
    ("070009",   "Arrotondamenti passivi",                  "costo",                  True, "COST"),
    ("070010",   "Perdite su cambi (se perdita)",           "costo",                  True, "COST"),
    ("070018",   "Commissioni e Oneri Bancari",             "costo",                  True, "COST"),
    ("070500",   "Interessi Passivi su Pagamenti Rateali",  "costo",                  True, "COST"),
    ("072551",   "Rival.crediti attivo circol.",            "patrimoniale_attivo",    False, None),
    ("075000",   "Plusv. da alien. immobilizz. (se plusvalenza)","patrimoniale_attivo",    False, None),
    ("080000",   "Minusval.alienazione immobil. (se minusvalenza)","patrimoniale_attivo",    False, None),
    ("090005",   "Ires dell'esercizio",                     "costo",                  True, "COST"),
    ("090006",   "Irap dell'esercizio",                     "costo",                  True, "COST"),
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
        ("QT", "30"), ("OR", "31"), ("DL", "32"), ("OA", "33"), ("GR", "34"),
        ("RFQ", "35"), ("PG", "22"),
    ]
    for doc_type, prefix in sequences:
        if not DocumentSequence.query.filter_by(doc_type=doc_type).first():
            db.session.add(DocumentSequence(doc_type=doc_type, prefix=prefix, current_number=0))

    # ── Utenti demo (DA CAMBIARE prima di un uso reale) ─────────
    if not User.query.filter_by(username="Angelo").first():
        u1 = User(username="Angelo", full_name="Angelo", role="operatore")
        u1.set_password("Angelo1234")
        db.session.add(u1)

    if not User.query.filter_by(username="Maurizio").first():
        u2 = User(username="Maurizio", full_name="Maurizio", role="commercialista")
        u2.set_password("Maurizio1234")
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
    if not EconomicSubject.query.filter_by(code="FORN-001").first():
        db.session.add(EconomicSubject(code="FORN-001", name="Acciai Lombardi SpA", piva="02345678901", payment_terms="Netto 30gg", is_supplier=True))
    if not EconomicSubject.query.filter_by(code="CUST-001").first():
        db.session.add(EconomicSubject(
            code="CUST-001", name="Ferrari Meccanica SpA", piva="03456789012", payment_terms="Netto 30gg", is_customer=True,
            # revenue_channel di esempio: 'affidamento_diretto' perché qui è il
            # grande committente stesso a commissionare la lavorazione, senza
            # un appaltatore principale in mezzo — quando aggiungi un cliente
            # che invece ti subappalta lavori, imposta 'subappalto'.
            revenue_channel="affidamento_diretto",
            # Dati fiscali di esempio, così Fattura cliente → XML FatturaPA
            # funziona subito in demo (CAMBIA con i dati reali del cliente).
            codice_fiscale="03456789012", indirizzo="Via dell'Industria 45", cap="41100",
            comune="Modena", provincia="MO", nazione="IT",
            codice_destinatario="0000000", pec_destinatario="amministrazione@pec.ferrarimeccanica.it",
        ))
    if not CostCenter.query.filter_by(code="CC-AMM-01").first():
        db.session.add(CostCenter(code="CC-AMM-01", name="Amministrazione"))
    if not CostCenter.query.filter_by(code="CC-PROD-01").first():
        db.session.add(CostCenter(code="CC-PROD-01", name="Produzione"))

    # ── Articoli demo — SENZA questi, Ordini Cliente/Fornitore, DDT ed
    # Entrata Merci hanno il menu articoli vuoto e non si può testare nulla
    # del ciclo attivo/passivo. Codici in linea con MasterLogistic-WMS per
    # il controllo giacenza in DDT (vedi services/logistic_client.py) — se
    # gli SKU reali sono diversi, aggiornali qui prima di un uso reale.
    from models import Material
    MATERIALS_DEMO = [
        # code,        description,                     type,   uom, standard_cost, sales_price, vat_rate
        ("RM-LAM-001", "Lamiera acciaio S235 (materia prima)", "ROH",  "KG", 1.35, 0,     22),
        ("SL-TRAN-01", "Transenna parapedonale (semilavorato)", "HALB", "PZ", 42.00, 0,    22),
        ("FP-CART-01", "Cartello stradale assemblato (prodotto finito)", "FERT", "PZ", 28.50, 65.00, 22),
    ]
    for code, desc, mtype, uom, std_cost, sales_price, vat in MATERIALS_DEMO:
        if not Material.query.filter_by(code=code).first():
            db.session.add(Material(code=code, description=desc, material_type=mtype, uom=uom,
                                    standard_cost=std_cost, sales_price=sales_price, vat_rate=vat,
                                    qty_on_hand=0, active=True))

    # ── Profilo Cedente/Prestatore per XML FatturaPA — dati di ESEMPIO ──
    # (vedi services/fatturapa.py e dashboard/routes.py). Da correggere
    # in Configurazione Fiscale → "Fatturazione elettronica" con i dati
    # reali dell'azienda prima di un uso reale. Riusiamo le descrizioni
    # ufficiali da dashboard/routes.py per non tenerle duplicate qui.
    from blueprints.dashboard.routes import FISCAL_PARAM_DEFAULTS

    FE_DEMO_VALUES = {
        "fe_denominazione": "IRON SEGNALETICA",
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

    # ── Configurazione conti Paghe (obbligatoria: senza questa riga il
    # modulo Paghe si rifiuta di contabilizzare, vedi services/payroll.py
    # ensure_config()). Punta ai conti "personale" appena creati sopra. ──
    db.session.flush()
    if not PayrollAccountConfig.query.first():
        def _acc_id(code):
            a = Account.query.filter_by(code=code).first()
            return a.id if a else None

        db.session.add(PayrollAccountConfig(
            wage_expense_account_id=_acc_id("660000"),               # Salari e Stipendi (costo)
            employer_burden_account_id=_acc_id("661000"),             # Oneri Sociali a carico azienda (costo)
            net_salary_payable_account_id=_acc_id("222000"),          # Debiti v/Dipendenti c/Retribuzioni
            inps_payable_account_id=_acc_id("223000"),                # Debiti v/INPS
            withholding_payable_account_id=_acc_id("224000"),         # Debiti Tributari - Ritenute Lav. Dip.
            bank_account_id=_acc_id("180000"),                        # Banca c/c
            imu_expense_account_id=_acc_id("620000"),                 # Costi di Manutenzione (fallback IMU immobili strumentali)
            accrued_holiday_expense_account_id=_acc_id("660000"),     # Ratei ferie: stesso costo del personale
            accrued_permission_expense_account_id=_acc_id("660000"),  # Ratei permessi: idem
            accrued_thirteenth_expense_account_id=_acc_id("660000"),  # Rateo tredicesima: idem
            accrued_payable_account_id=_acc_id("227000"),             # Ratei Passivi
            tfr_expense_account_id=_acc_id("662000"),                 # Accantonamento TFR (costo)
            tfr_fund_account_id=_acc_id("226000"),                    # TFR - Fondo Trattamento Fine Rapporto
        ))

    db.session.commit()

