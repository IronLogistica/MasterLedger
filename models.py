"""
Modelli del database — MasterLedger (Flask + SQLAlchemy)

Principi seguiti, coerenti con quanto deciso nel piano di trasformazione:
  - Ogni JournalEntry è IMMUTABILE una volta creato: non si modifica, si storna
    (vedi JournalEntry.reversed_by_id). Rispetta il principio di integrità
    documentale richiesto dalla normativa fiscale italiana.
  - La numerazione dei documenti è progressiva e sequenziale per tipo
    documento (vedi DocumentSequence) — niente "buchi" nella numerazione.
  - I parametri fiscali "di giudizio professionale" (aliquote ammortamento,
    metodo valutazione magazzino, % svalutazione crediti) vivono in
    FiscalParameter: sono DATI, non codice — il Commercialista li modifica
    dal proprio pannello, senza bisogno di toccare l'applicazione.
"""
from datetime import datetime
from decimal import Decimal
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db


# ══════════════════════════════════════════════════════════════
# UTENTI E RUOLI
# ══════════════════════════════════════════════════════════════
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    # Ruoli: 'operatore' (Prima Nota/Fatture quotidiane) vs 'commercialista'
    # (unico ruolo che può modificare i Parametri Fiscali — vedi blueprints/warehouse
    # e la sezione Config Fiscale in dashboard).
    role = db.Column(db.String(20), nullable=False, default="operatore")
    is_active_flag = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    @property
    def is_commercialista(self):
        return self.role == "commercialista"

    def __repr__(self):
        return f"<User {self.username} ({self.role})>"


# ══════════════════════════════════════════════════════════════
# PIANO DEI CONTI
# ══════════════════════════════════════════════════════════════
class Account(db.Model):
    __tablename__ = "accounts"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(10), unique=True, nullable=False)   # es. "150000"
    name = db.Column(db.String(120), nullable=False)
    account_type = db.Column(db.String(20), nullable=False)
    # account_type: 'patrimoniale_attivo' | 'patrimoniale_passivo' | 'costo' | 'ricavo'

    # Se True, le righe su questo conto generano un movimento Costi collegato
    # (esattamente come "coRelevant" nel simulatore JS) — solo i conti di
    # Conto Economico devono essere marcati così.
    cost_relevant = db.Column(db.Boolean, default=False)
    cost_relevant_type = db.Column(db.String(10))  # 'COST' | 'REVENUE' | None

    active = db.Column(db.Boolean, default=True)

    def __repr__(self):
        return f"<Account {self.code} {self.name}>"


# ══════════════════════════════════════════════════════════════
# NUMERAZIONE PROGRESSIVA DOCUMENTI (per tipo documento)
# ══════════════════════════════════════════════════════════════
class DocumentSequence(db.Model):
    """
    Un contatore per ogni tipo di documento (SA=Prima Nota, KR=Fattura
    Fornitore, DR=Fattura Cliente, KZ=Pagamento, DZ=Incasso, Cespiti=Cespite).

    NOTA IMPORTANTE PER CHI ESTENDE QUESTO CODICE: l'incremento qui sotto
    (vedi next_number()) usa un semplice UPDATE all'interno della stessa
    transazione DB — sufficiente per un carico moderato, ma non è ancora
    "a prova di alta concorrenza". Prima di un vero go-live con più utenti
    contemporanei, sostituire con una SEQUENCE nativa Postgres o un
    SELECT ... FOR UPDATE esplicito, per eliminare ogni rischio di
    doppia assegnazione dello stesso numero.
    """
    __tablename__ = "document_sequences"

    id = db.Column(db.Integer, primary_key=True)
    doc_type = db.Column(db.String(10), unique=True, nullable=False)
    prefix = db.Column(db.String(10), nullable=False)
    current_number = db.Column(db.Integer, nullable=False, default=0)

    @classmethod
    def next_number(cls, doc_type, prefix):
        seq = cls.query.filter_by(doc_type=doc_type).first()
        if seq is None:
            seq = cls(doc_type=doc_type, prefix=prefix, current_number=0)
            db.session.add(seq)
        seq.current_number += 1
        db.session.flush()  # garantisce che current_number sia scritto prima del commit finale
        return f"{seq.prefix}{seq.current_number:08d}"


# ══════════════════════════════════════════════════════════════
# PRIMA NOTA — TESTATA E RIGHE (immutabili: si stornano, non si modificano)
# ══════════════════════════════════════════════════════════════
class JournalEntry(db.Model):
    __tablename__ = "journal_entries"

    id = db.Column(db.Integer, primary_key=True)
    doc_number = db.Column(db.String(20), unique=True, nullable=False)
    doc_type = db.Column(db.String(10), nullable=False)   # SA, KR, DR, KZ, DZ, Cespiti...
    doc_date = db.Column(db.Date, nullable=False)
    posting_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    description = db.Column(db.String(255))
    source_module = db.Column(db.String(20), default="LEDGER")  # LEDGER, MAGAZZINO, VENDITE, PRODUZIONE...
    reference = db.Column(db.String(80))

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Immutabilità: un documento non si modifica. Se sbagliato, si storna
    # creando un NUOVO documento di segno opposto collegato a questo.
    is_reversed = db.Column(db.Boolean, default=False)
    reversed_by_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    reverses_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)

    # Compensazione (clearing) semplificata per i documenti AP/AR (KR/DR):
    # True quando un pagamento/incasso ha chiuso la posizione. Un vero MasterLedger
    # userebbe la compensazione a livello di singola posizione (partita);
    # qui, per l'MVP, si compensa l'intero documento in un colpo solo.
    is_paid = db.Column(db.Boolean, default=False)
    paid_by_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)

    # Soggetto economico unico, cliente e/o fornitore.
    economic_subject_id = db.Column(db.Integer, db.ForeignKey("economic_subjects.id"), nullable=True)
    gross_amount = db.Column(db.Numeric(14, 2), nullable=True)  # importo totale fattura, per comodità

    # Aliquota IVA della fattura (unica, per l'MVP — una sola riga/aliquota).
    # Salvata esplicitamente invece di essere ricalcolata da IVA/Imponibile,
    # per evitare arrotondamenti quando si genera l'XML FatturaPA (vedi
    # services/fatturapa.py). Popolata da Fattura cliente (fatture cliente); NULL per
    # gli altri tipi documento dove non serve.
    vat_rate = db.Column(db.Numeric(5, 2), nullable=True)

    # Codice Natura IVA (N1, N2.1, N4, ...) — OBBLIGATORIO per le specifiche
    # SdI quando l'aliquota IVA è zero (controlli 00400 sulla linea e 00429
    # sui DatiRiepilogo: "l'indicazione di un'aliquota IVA pari a zero
    # obbliga all'indicazione della natura che giustifichi la non
    # imponibilità"). NULL per fatture con aliquota > 0 (dove la presenza
    # di Natura causerebbe invece lo scarto 00430).
    natura = db.Column(db.String(4), nullable=True)

    # Nota di credito (doc_type DG): riferimento alla fattura originale che
    # viene rettificata. Alimenta il blocco <DatiFattureCollegate> dell'XML
    # (TD04) — facoltativo nel tracciato ma buona prassi.
    linked_invoice_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    linked_invoice = db.relationship("JournalEntry", remote_side="JournalEntry.id",
                                     foreign_keys=[linked_invoice_id])

    party = db.relationship("EconomicSubject")

    lines = db.relationship("JournalLine", backref="entry", cascade="all, delete-orphan")
    created_by = db.relationship("User")

    @property
    def total_dare(self):
        return sum(l.dare for l in self.lines)

    @property
    def total_avere(self):
        return sum(l.avere for l in self.lines)

    @property
    def is_balanced(self):
        return abs(self.total_dare - self.total_avere) < 0.01

    def __repr__(self):
        return f"<JournalEntry {self.doc_number}>"


class JournalLine(db.Model):
    __tablename__ = "journal_lines"

    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    dare = db.Column(db.Numeric(14, 2), default=0)
    avere = db.Column(db.Numeric(14, 2), default=0)
    description = db.Column(db.String(255))

    # Oggetto Costi collegato (se il conto è cost_relevant) — Centro di costo,
    # Ordine Interno, o simile. Facoltativo per i conti patrimoniali.
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=True)

    account = db.relationship("Account")
    cost_center = db.relationship("CostCenter")


class InvoiceLine(db.Model):
    """
    Riga di dettaglio di una fattura/nota di credito cliente (Fattura cliente/Nota di credito cliente).
    Distinta dalla JournalLine (che è la riga CONTABILE in partita doppia):
    la InvoiceLine è la riga COMMERCIALE del documento, e alimenta i blocchi
    <DettaglioLinee> e <DatiRiepilogo> dell'XML FatturaPA.

    Le specifiche SdI impongono (controlli 00419/00422): un blocco
    DatiRiepilogo per ogni aliquota presente in fattura, con
    ImponibileImporto = somma dei PrezzoTotale delle righe con quella
    aliquota. Il raggruppamento avviene per coppia (aliquota, natura).
    """
    __tablename__ = "invoice_lines"

    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=False)
    line_number = db.Column(db.Integer, nullable=False)
    description = db.Column(db.String(255), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)      # imponibile di riga
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    natura = db.Column(db.String(4), nullable=True)             # solo se vat_rate = 0
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)

    entry = db.relationship("JournalEntry", backref=db.backref(
        "invoice_lines", cascade="all, delete-orphan",
        order_by="InvoiceLine.line_number"))
    account = db.relationship("Account")


# ══════════════════════════════════════════════════════════════
# ANAGRAFICHE FORNITORE / CLIENTE
# ══════════════════════════════════════════════════════════════
class EconomicSubject(db.Model):
    """Anagrafica unica: può operare contemporaneamente come cliente e fornitore."""
    __tablename__ = "economic_subjects"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)  # denominazione o nome completo
    subject_type = db.Column(db.String(12), nullable=False, default="azienda")  # azienda | persona
    is_customer = db.Column(db.Boolean, nullable=False, default=False)
    is_supplier = db.Column(db.Boolean, nullable=False, default=False)
    piva = db.Column(db.String(20), index=True)
    codice_fiscale = db.Column(db.String(16))
    indirizzo = db.Column(db.String(120))
    cap = db.Column(db.String(10))
    comune = db.Column(db.String(80))
    provincia = db.Column(db.String(2))
    nazione = db.Column(db.String(2), default="IT")
    email = db.Column(db.String(120))
    pec = db.Column(db.String(120))
    telefono = db.Column(db.String(40))
    codice_destinatario = db.Column(db.String(7), default="0000000")
    payment_terms = db.Column(db.String(40), default="Netto 30gg")
    iban = db.Column(db.String(34))
    active = db.Column(db.Boolean, default=True)

    # Canale ricavo (solo per i clienti): 'subappalto' se Iron Appalti lavora
    # per conto di un appaltatore principale (conto ricavi 4000), oppure
    # 'affidamento_diretto' se la commessa arriva direttamente da un grande
    # committente senza appaltatore intermedio (conto ricavi 4001). Guida la
    # scelta automatica del conto ricavi in Fatturazione DDT (blueprints/sd).
    revenue_channel = db.Column(db.String(20), nullable=True)

    @property
    def role_label(self):
        if self.is_customer and self.is_supplier:
            return "Cliente e fornitore"
        if self.is_customer:
            return "Cliente"
        if self.is_supplier:
            return "Fornitore"
        return "Da qualificare"

    @property
    def pec_destinatario(self):
        return self.pec

    @pec_destinatario.setter
    def pec_destinatario(self, value):
        self.pec = value



# ══════════════════════════════════════════════════════════════
# CENTRI DI COSTO
# ══════════════════════════════════════════════════════════════
class CostCenter(db.Model):
    __tablename__ = "cost_centers"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    active = db.Column(db.Boolean, default=True)


# ══════════════════════════════════════════════════════════════
# CESPITI (Asset Accounting)
# ══════════════════════════════════════════════════════════════
class Asset(db.Model):
    __tablename__ = "assets"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    description = db.Column(db.String(200), nullable=False)
    acquisition_value = db.Column(db.Numeric(14, 2), nullable=False)
    acquisition_date = db.Column(db.Date, nullable=False)

    # Categoria collegata ai Parametri Fiscali (aliquota/metodo) impostati
    # dal Commercialista — vedi FiscalParameter.
    asset_category = db.Column(db.String(40), default="Impianti e Macchinari")
    useful_life_years = db.Column(db.Integer, default=10)
    accumulated_depreciation = db.Column(db.Numeric(14, 2), default=0)
    active = db.Column(db.Boolean, default=True)


# ══════════════════════════════════════════════════════════════
# ENTERPRISE STRUCTURE — SETUP DEI MAGAZZINI (Sedi operative e aree di magazzino)
# ══════════════════════════════════════════════════════════════
class OperatingSite(db.Model):
    """Uno stabilimento/sede fisica assegnata al Codice azienda."""
    __tablename__ = "operating_sites"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(4), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    city = db.Column(db.String(80))
    region = db.Column(db.String(80))
    company_code = db.Column(db.String(4), default="1000")
    active = db.Column(db.Boolean, default=True)

    # Planimetria della sede, per disegnarci sopra i blocchi (WarehouseArea)
    # posizionati — bytes nel database, MAI su disco (coerente con tutto il
    # resto dell'app: il filesystem del container non è persistente).
    floor_plan_image = db.Column(db.LargeBinary, nullable=True)
    floor_plan_mimetype = db.Column(db.String(50), nullable=True)
    floor_plan_width = db.Column(db.Float, nullable=True)
    floor_plan_height = db.Column(db.Float, nullable=True)

    warehouse_areas = db.relationship("WarehouseArea", backref="site", cascade="all, delete-orphan")

    @property
    def ha_planimetria(self):
        return self.floor_plan_image is not None


class WarehouseArea(db.Model):
    """
    Un'area di magazzino all’interno di una sede operativa — il vero "Setup dei
    Magazzini" richiesto: definisce nome, tipo e soprattutto il conto G/L
    di magazzino a cui l'area è collegata, per studiare lo stoccaggio
    corretto (materie prime vs prodotti finiti vs blocco qualità, ecc.)

    Ogni area ("blocco") ha la SUA struttura di stoccaggio, attivabile in
    modo indipendente dalle altre (usa_*): un'area a terra non ha scaffali
    né cantilever, un magazzino verticale sì — il form di creazione
    ubicazione mostra e richiede solo i campi che l'area ha attivato.
    pos_x/pos_y/dim_x/dim_y posizionano il blocco sulla mappa a schermo
    della sede operativa (coordinate/ingombro liberi, non un'unità fissa).
    """
    __tablename__ = "warehouse_areas"

    AREA_TYPES = {
        "ROH":   "Materie Prime",
        "FERT":  "Prodotti Finiti",
        "HALB":  "Semilavorati",
        "QUAL":  "Blocco Qualità",
        "SCRAP": "Scarti/Resi",
        "TRANS": "Transito/Ricevimento",
    }

    id = db.Column(db.Integer, primary_key=True)
    site_id = db.Column(db.Integer, db.ForeignKey("operating_sites.id"), nullable=False)
    code = db.Column(db.String(4), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    area_type = db.Column(db.String(10), nullable=False, default="ROH")

    # Conto di magazzino collegato — NULL solo per aree di puro transito
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    account = db.relationship("Account")

    active = db.Column(db.Boolean, default=True)

    pos_x = db.Column(db.Float, nullable=True)
    pos_y = db.Column(db.Float, nullable=True)
    dim_x = db.Column(db.Float, nullable=True)
    dim_y = db.Column(db.Float, nullable=True)

    usa_corsie = db.Column(db.Boolean, nullable=False, default=True)
    usa_scaffali = db.Column(db.Boolean, nullable=False, default=True)
    usa_ripiani = db.Column(db.Boolean, nullable=False, default=True)
    usa_cassette = db.Column(db.Boolean, nullable=False, default=False)
    usa_cantilever = db.Column(db.Boolean, nullable=False, default=False)
    area_a_terra = db.Column(db.Boolean, nullable=False, default=False)

    __table_args__ = (db.UniqueConstraint("site_id", "code", name="uq_site_sloc_code"),)

    @property
    def area_type_label(self):
        return self.AREA_TYPES.get(self.area_type, self.area_type)

    @property
    def ha_mappa_posizionata(self):
        return None not in (self.pos_x, self.pos_y, self.dim_x, self.dim_y)


class StorageLocation(db.Model):
    """
    Ubicazione granulare dentro un'Area di Magazzino ("blocco") — corsia,
    scaffale, ripiano, cassetta: SOLO i livelli che quel blocco ha attivato
    (WarehouseArea.usa_*), mai un campo obbligatorio che il blocco non usa.
    Il codice è univoco all'interno del blocco (non serve essere univoco
    globalmente: il codice completo, per chi legge, è sempre
    SEDE-BLOCCO-codice, es. 2000-ROH1-C03-S12-L02).

    pos_x/pos_y/dim_x/dim_y posizionano l'ubicazione sulla mappa a schermo
    DI QUEL BLOCCO — coordinate locali al blocco, indipendenti dalla
    posizione del blocco stesso sulla mappa della sede.
    """
    __tablename__ = "storage_locations"

    TIPI_STOCCAGGIO = {
        "SCAFFALE": "Scaffale",
        "CANTILEVER": "Cantilever",
        "AREA_TERRA": "Area a terra",
        "PALLET": "Postazione pallet",
    }
    STATI = {
        "libero": "Libero",
        "occupato": "Occupato",
        "manutenzione": "In manutenzione",
        "bloccato": "Bloccato",
    }

    id = db.Column(db.Integer, primary_key=True)
    warehouse_area_id = db.Column(db.Integer, db.ForeignKey("warehouse_areas.id"), nullable=False, index=True)
    codice = db.Column(db.String(40), nullable=False)
    corridoio = db.Column(db.String(10))
    scaffale = db.Column(db.String(10))
    ripiano = db.Column(db.String(10))
    cassetta = db.Column(db.String(10))
    tipo_stoccaggio = db.Column(db.String(20), nullable=False, default="SCAFFALE")
    stato = db.Column(db.String(20), nullable=False, default="libero")

    pos_x = db.Column(db.Float, nullable=False, default=0)
    pos_y = db.Column(db.Float, nullable=False, default=0)
    dim_x = db.Column(db.Float, nullable=False, default=100)
    dim_y = db.Column(db.Float, nullable=False, default=100)
    peso_max_kg = db.Column(db.Float, nullable=True)

    note = db.Column(db.String(255))
    active = db.Column(db.Boolean, default=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    warehouse_area = db.relationship("WarehouseArea", backref="storage_locations")

    __table_args__ = (db.UniqueConstraint("warehouse_area_id", "codice", name="uq_storage_location_codice"),)

    @property
    def stato_label(self):
        return self.STATI.get(self.stato, self.stato)

    @property
    def tipo_label(self):
        return self.TIPI_STOCCAGGIO.get(self.tipo_stoccaggio, self.tipo_stoccaggio)


# ══════════════════════════════════════════════════════════════
# MAGAZZINO INTERNO — ledger movimenti + distinta base (sostituisce
# l'integrazione in sola lettura verso MasterLogistic-WMS: qui non serve
# nessun parser perché ordini cliente/fornitore vivono già come righe vere
# in questo stesso database — SalesOrderLine/PurchaseOrderLine — non come
# PDF da rileggere). Vedi services/warehouse.py per la logica.
# ══════════════════════════════════════════════════════════════
class StockMovement(db.Model):
    """
    Riga di ledger di magazzino: OGNI variazione di giacenza — carico da
    Entrata Merci, scarico da DDT (PGI), prelievo/versamento produzione,
    rettifica manuale — passa da qui. Material.qty_on_hand resta come
    CACHE dell'ultimo saldo (per le query veloci nelle liste), ma la
    fonte di verità per audit e riconciliazione è la somma di questi
    movimenti — mai un PDF, mai un sistema esterno.
    """
    __tablename__ = "stock_movements"
    id = db.Column(db.Integer, primary_key=True)
    material_id = db.Column(db.Integer, db.ForeignKey("materials.id"), nullable=False, index=True)
    warehouse_area_id = db.Column(db.Integer, db.ForeignKey("warehouse_areas.id"), nullable=True)
    qty = db.Column(db.Numeric(14, 3), nullable=False)          # + carico, - scarico
    unit_cost = db.Column(db.Numeric(14, 4), nullable=True)     # valorizzazione al momento del movimento
    movement_type = db.Column(db.String(20), nullable=False)    # delivery|goods_receipt|production_issue|production_receipt|adjustment
    source_type = db.Column(db.String(30), nullable=True)       # 'delivery_line'|'goods_receipt_line'|'production_order'|'manual'
    source_id = db.Column(db.Integer, nullable=True)            # id della riga/documento sorgente
    doc_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    notes = db.Column(db.String(255))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    material = db.relationship("Material")
    warehouse_area = db.relationship("WarehouseArea")

    @property
    def total_value(self):
        if self.unit_cost is None:
            return None
        return Decimal(str(self.qty)) * Decimal(str(self.unit_cost))


class WorkCenter(db.Model):
    """
    Centro di lavoro (reparto fisico: taglio, foratura, assemblaggio,
    confezionamento) — l'oggetto su cui si accumula il pool di overhead
    (ProductionOverheadItem, tramite work_center_id) e attraverso cui il
    Ciclo di Lavorazione (Routing) assorbe manodopera diretta + overhead nel
    costo standard, sostituendo per gli articoli che hanno un ciclo attivo la
    quota-fatturato approssimata di _calcola_overhead_da_fatturato.

    hourly_rate_labor: tariffa oraria manodopera diretta, inserita a mano
    (costo orario pieno dell'operatore, es. comprensivo di TFR/contributi) —
    non calcolata dal pool, perché la manodopera diretta non è un costo
    indiretto di reparto.

    capacity_hours_month: ore pianificate/mese del centro, denominatore per
    calcolare la tariffa oraria di overhead = pool del centro nel mese /
    capacity_hours_month (metodo SAP del centro di lavoro).
    """
    __tablename__ = "work_centers"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    description = db.Column(db.String(120), nullable=False)
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=True)
    capacity_hours_month = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    hourly_rate_labor = db.Column(db.Numeric(10, 4), nullable=False, default=0)
    active = db.Column(db.Boolean, default=True)

    cost_center = db.relationship("CostCenter")


class Routing(db.Model):
    """
    Ciclo di lavorazione di un articolo padre (HALB o FERT), a versione —
    stesso principio di versionamento di BillOfMaterial: ogni modifica alle
    fasi apre una nuova versione invece di sovrascrivere quella in uso.
    """
    __tablename__ = "routings"
    id = db.Column(db.Integer, primary_key=True)
    parent_material_id = db.Column(db.Integer, db.ForeignKey("materials.id"), nullable=False, index=True)
    version = db.Column(db.String(10), nullable=False, default="1")
    active = db.Column(db.Boolean, default=True)
    notes = db.Column(db.String(255))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    parent_material = db.relationship("Material", foreign_keys=[parent_material_id])
    operations = db.relationship("RoutingOperation", backref="routing", cascade="all, delete-orphan",
                                 order_by="RoutingOperation.seq")

    __table_args__ = (db.UniqueConstraint("parent_material_id", "version", name="uq_routing_parent_version"),)


class RoutingOperation(db.Model):
    """Fase del ciclo: tempo standard macchina/manodopera per 1 unità del
    padre, presso un centro di lavoro. seq in stile SAP (10, 20, 30...)."""
    __tablename__ = "routing_operations"
    id = db.Column(db.Integer, primary_key=True)
    routing_id = db.Column(db.Integer, db.ForeignKey("routings.id"), nullable=False)
    seq = db.Column(db.Integer, nullable=False, default=10)
    work_center_id = db.Column(db.Integer, db.ForeignKey("work_centers.id"), nullable=False)
    description = db.Column(db.String(200))
    machine_time_min = db.Column(db.Numeric(10, 4), nullable=False, default=0)
    labor_time_min = db.Column(db.Numeric(10, 4), nullable=False, default=0)

    work_center = db.relationship("WorkCenter")

    __table_args__ = (db.UniqueConstraint("routing_id", "seq", name="uq_routing_operation_seq"),)


class BillOfMaterial(db.Model):
    """
    Distinta base di un articolo padre (HALB o FERT), a versione — sostituisce
    la DistintaBase piatta di MasterLogistic-WMS con qualcosa di versionabile:
    ogni modifica ai componenti apre una nuova versione invece di sovrascrivere
    quella in uso (coerente con come funzionano già i Costi Standard per mese).
    """
    __tablename__ = "bill_of_materials"
    id = db.Column(db.Integer, primary_key=True)
    parent_material_id = db.Column(db.Integer, db.ForeignKey("materials.id"), nullable=False, index=True)
    version = db.Column(db.String(10), nullable=False, default="1")
    active = db.Column(db.Boolean, default=True)
    notes = db.Column(db.String(255))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    parent_material = db.relationship("Material", foreign_keys=[parent_material_id])
    components = db.relationship("BOMComponent", backref="bom", cascade="all, delete-orphan")

    __table_args__ = (db.UniqueConstraint("parent_material_id", "version", name="uq_bom_parent_version"),)


class BOMComponent(db.Model):
    __tablename__ = "bom_components"
    id = db.Column(db.Integer, primary_key=True)
    bom_id = db.Column(db.Integer, db.ForeignKey("bill_of_materials.id"), nullable=False)
    component_material_id = db.Column(db.Integer, db.ForeignKey("materials.id"), nullable=False)
    qty_per = db.Column(db.Numeric(14, 4), nullable=False)   # quantità componente per 1 unità di padre
    scrap_pct = db.Column(db.Numeric(5, 2), nullable=False, default=0)  # % scarto fisiologico, applicato in explode_bom
    notes = db.Column(db.String(255))

    component_material = db.relationship("Material", foreign_keys=[component_material_id])

    __table_args__ = (db.UniqueConstraint("bom_id", "component_material_id", name="uq_bom_component"),)


# ══════════════════════════════════════════════════════════════
# PARAMETRI FISCALI — pannello riservato al Commercialista
# ══════════════════════════════════════════════════════════════
class FiscalParameter(db.Model):
    """
    Coppia chiave/valore + descrizione, modificabile SOLO da utenti con
    ruolo 'commercialista'. Qui vivono le decisioni di giudizio
    professionale (metodo ammortamento, valutazione magazzino, % svalutazione
    crediti) — il codice applicativo le LEGGE, non le decide.
    """
    __tablename__ = "fiscal_parameters"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(255))
    category = db.Column(db.String(40))  # 'ammortamenti' | 'magazzino' | 'crediti' | 'iva'
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    updated_by = db.relationship("User")


# ══════════════════════════════════════════════════════════════
# Fase 4 (progettazione parti mancanti, punto 6) — Riconciliazione
# bancaria. Modello a tre livelli: testata estratto conto, righe
# importate, allocazioni molti-a-molti verso le righe del mastrino
# (un bonifico può chiudere più fatture, una riga GL può essere
# coperta da più movimenti bancari — es. incasso + commissione
# separati sull'estratto conto).
class BankStatement(db.Model):
    __tablename__ = "bank_statements"

    id = db.Column(db.Integer, primary_key=True)
    bank_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    period_from = db.Column(db.Date, nullable=False)
    period_to = db.Column(db.Date, nullable=False)
    opening_balance = db.Column(db.Numeric(14, 2), nullable=False)
    closing_balance = db.Column(db.Numeric(14, 2), nullable=False)
    import_filename = db.Column(db.String(255))
    file_hash = db.Column(db.String(64))  # impronta dell'INTERO file — evita di reimportare lo stesso file due volte
    imported_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    imported_at = db.Column(db.DateTime, default=datetime.utcnow)

    bank_account = db.relationship("Account")
    imported_by = db.relationship("User")

    __table_args__ = (db.UniqueConstraint("bank_account_id", "file_hash", name="uq_statement_file_hash"),)


class BankStatementLine(db.Model):
    __tablename__ = "bank_statement_lines"

    id = db.Column(db.Integer, primary_key=True)
    statement_id = db.Column(db.Integer, db.ForeignKey("bank_statements.id"), nullable=False)
    value_date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(255))
    amount = db.Column(db.Numeric(14, 2), nullable=False)  # positivo=accredito, negativo=addebito
    bank_transaction_id = db.Column(db.String(120))  # identificativo bancario, se il formato lo fornisce
    # Hash CONTESTUALIZZATO (non unique globale): due movimenti reali
    # possono avere stesso importo/data/descrizione senza essere lo
    # stesso movimento — l'unicità è su (statement_id, hash), non sulla
    # sola riga, così un secondo import dello stesso file viene bloccato
    # ma due bonifici identici nello stesso giorno restano entrambi validi.
    import_hash = db.Column(db.String(64), nullable=False)

    statement = db.relationship("BankStatement", backref="lines")

    __table_args__ = (db.UniqueConstraint("statement_id", "import_hash", name="uq_statement_line_hash"),)

    @property
    def allocated_amount(self):
        return sum((a.amount_allocated for a in self.allocations if not a.reversed), Decimal("0"))

    @property
    def residual_amount(self):
        return abs(Decimal(str(self.amount))) - self.allocated_amount

    @property
    def is_reconciled(self):
        return self.residual_amount <= 0


class BankReconciliationAllocation(db.Model):
    """Allocazione molti-a-molti tra una riga di estratto conto e una riga
    di mastrino GL — mai un collegamento 1:1 rigido: un bonifico può
    chiudere più fatture, un incasso e la sua commissione possono comparire
    come due righe separate sull'estratto conto per un solo movimento GL."""
    __tablename__ = "bank_reconciliation_allocations"

    id = db.Column(db.Integer, primary_key=True)
    statement_line_id = db.Column(db.Integer, db.ForeignKey("bank_statement_lines.id"), nullable=False)
    journal_line_id = db.Column(db.Integer, db.ForeignKey("journal_lines.id"), nullable=False)
    amount_allocated = db.Column(db.Numeric(14, 2), nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reversed = db.Column(db.Boolean, default=False)

    statement_line = db.relationship("BankStatementLine", backref="allocations")
    journal_line = db.relationship("JournalLine", backref="bank_allocations")


# ══════════════════════════════════════════════════════════════
# Fase 3 (progettazione parti mancanti, punto 1) — Pagamenti
# parziali, scadenze e residui.
#
# Ogni fattura (KR/DR) riceve almeno UNA rata (InvoiceInstallment)
# alla creazione — se non è configurato un piano a più scadenze,
# la rata è unica e copre l'intero gross_amount, così il comportamento
# di oggi (paga tutto insieme) resta disponibile senza differenze.
#
# is_paid su JournalEntry NON diventa una proprietà calcolata (lo
# era stato proposto in una prima versione e correttamente respinto
# in revisione: il codice esistente lo usa dentro filter() SQL, una
# property Python non è interrogabile lì). Resta una colonna vera,
# sincronizzata dentro la STESSA transazione di ogni allocazione:
# quando l'ultima rata di una fattura si azzera, is_paid diventa True
# nello stesso commit — mai in un passaggio separato.
class InvoiceInstallment(db.Model):
    __tablename__ = "invoice_installments"

    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=False)
    numero_rata = db.Column(db.Integer, nullable=False, default=1)
    due_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)           # importo originario della rata
    residual_amount = db.Column(db.Numeric(14, 2), nullable=False)  # si riduce ad ogni allocazione, mai < 0, mai > amount
    version = db.Column(db.Integer, nullable=False, default=0)      # controllo di concorrenza ottimistico
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    entry = db.relationship("JournalEntry", foreign_keys=[entry_id])

    @property
    def is_settled(self):
        return self.residual_amount <= 0

    @property
    def is_overdue(self):
        """Non salvato: calcolato al momento della query contro la data
        odierna — 'scaduta' non è mai un valore persistito e stantio."""
        return (not self.is_settled) and self.due_date < datetime.utcnow().date()

    @property
    def status_label(self):
        if self.is_settled:
            return "saldata"
        if self.residual_amount < self.amount:
            return "scaduta e parziale" if self.is_overdue else "parziale"
        return "scaduta" if self.is_overdue else "aperta"


class PaymentAllocation(db.Model):
    """Allocazione molti-a-molti: un pagamento può chiudere più rate, una
    rata può ricevere più pagamenti nel tempo. cash_amount è la quota
    coperta da denaro reale (si somma al movimento banca del pagamento);
    abbuono_amount è una quota che riduce comunque il residuo della rata
    ma genera una riga contabile separata sul conto abbuoni autorizzato,
    non un movimento di cassa."""
    __tablename__ = "payment_allocations"

    id = db.Column(db.Integer, primary_key=True)
    payment_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=False)
    installment_id = db.Column(db.Integer, db.ForeignKey("invoice_installments.id"), nullable=False)
    cash_amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    abbuono_amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reversed = db.Column(db.Boolean, default=False)
    reversed_at = db.Column(db.DateTime)
    reversed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    payment_entry = db.relationship("JournalEntry", foreign_keys=[payment_entry_id])
    installment = db.relationship("InvoiceInstallment")


# ══════════════════════════════════════════════════════════════
# Fase 2 (progettazione parti mancanti, punto 5) — Chiusura e
# blocco dei periodi contabili.
#
# Un periodo ASSENTE da questa tabella è bloccante per default
# (vedi PERIOD_LOCK_ENFORCED in services/posting.py) SOLO dopo che
# l'azienda ha iniziato a creare i propri periodi. Durante la messa
# in produzione iniziale, quando ancora nessun periodo esiste, il
# blocco è disattivabile da FiscalParameter così da non paralizzare
# il lavoro il primo giorno — va riattivato non appena i periodi
# sono stati creati (vedi info_box nella pagina di gestione periodi).
class AccountingPeriod(db.Model):
    __tablename__ = "accounting_periods"
    __table_args__ = (db.UniqueConstraint("company", "year", "month", name="uq_period_company_year_month"),)

    id = db.Column(db.Integer, primary_key=True)
    company = db.Column(db.String(80), nullable=False, default="Iron Appalti")  # esplicito fin da subito, anche se oggi single-company
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)  # 1-12
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    period_type = db.Column(db.String(20), default="mensile")  # mensile | trimestrale | annuale
    status = db.Column(db.String(24), default="aperto")  # aperto | chiuso | riaperto_temporaneamente
    closed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    closed_at = db.Column(db.DateTime)
    reopen_reason = db.Column(db.String(255))

    closed_by = db.relationship("User")

    @property
    def is_open(self):
        return self.status in ("aperto", "riaperto_temporaneamente")

    @staticmethod
    def find_for_date(d, company="Iron Appalti"):
        return AccountingPeriod.query.filter(
            AccountingPeriod.company == company,
            AccountingPeriod.start_date <= d,
            AccountingPeriod.end_date >= d,
        ).first()


class AccountingPeriodLog(db.Model):
    """Log immutabile di chiusure/riaperture — tabella separata da
    AccountingPeriod apposta: anche se lo stato del periodo viene
    riportato indietro (riapertura), la storia di CHI e QUANDO resta,
    riga per riga, mai sovrascritta."""
    __tablename__ = "accounting_period_logs"

    id = db.Column(db.Integer, primary_key=True)
    period_id = db.Column(db.Integer, db.ForeignKey("accounting_periods.id"), nullable=False)
    action = db.Column(db.String(20), nullable=False)  # chiusura | riapertura
    performed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    performed_at = db.Column(db.DateTime, default=datetime.utcnow)
    reason = db.Column(db.String(255))

    period = db.relationship("AccountingPeriod")
    performed_by = db.relationship("User")


# ══════════════════════════════════════════════════════════════
# Fase 1 (progettazione parti mancanti, punto 0) — Piano dei conti
# canonico e configurabile.
#
# PERCHÉ ESISTE: prima di questa tabella, AP/AR/GL/Cespiti avevano i
# codici conto (banca, IVA a credito/debito, crediti clienti, debiti
# fornitori...) scritti a mano dentro _get_account_by_code("180000")
# sparsi in più file. Cambiare piano dei conti significava una caccia
# al codice in ogni blueprint. Ora ogni concetto si legge da qui.
#
# COSA NON FA: non esegue una migrazione automatica verso il piano dei
# conti "reale" di Iron Appalti. Gli Schemi 1-13 (services/
# classificazione_operazioni.py, services/rettifiche_operazioni.py)
# usano già i codici reali (032003, 045001...) perché quei codici sono
# noti con certezza. Per i concetti di BASE usati qui sotto (crediti
# clienti, debiti fornitori, cespiti) NON esiste ancora una conferma
# del codice reale equivalente nel piano dei ~795 conti del
# commercialista — il CSV completo non è mai stato ricevuto. Finché
# non arriva, questa tabella punta ai codici "generici" originari
# (140000, 154000, 170000, 180000, 210000...): NIENTE CAMBIA nel
# comportamento oggi, cambia solo DOVE il codice è scritto — da
# sparso nel codice a centralizzato qui, pronto per essere aggiornato
# in un solo posto quando la mappatura reale sarà approvata.
class AccountMapping(db.Model):
    """
    Concetto contabile → conto attivo. Modificabile SOLO da ruolo
    'commercialista' (stessa protezione di FiscalParameter/
    PayrollAccountConfig). Il codice applicativo LEGGE da qui,
    non decide più da solo quale conto usare.
    """
    __tablename__ = "account_mappings"

    id = db.Column(db.Integer, primary_key=True)
    concept_key = db.Column(db.String(60), unique=True, nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    label = db.Column(db.String(120), nullable=False)   # descrizione leggibile per l'UI di configurazione
    category = db.Column(db.String(40))                 # 'banca' | 'iva' | 'clienti_fornitori' | 'cespiti'
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    account = db.relationship("Account")
    updated_by = db.relationship("User")

    @staticmethod
    def get(concept_key):
        """Ritorna l'Account mappato per un concetto, o None se non configurato."""
        m = AccountMapping.query.filter_by(concept_key=concept_key).first()
        return m.account if m else None

    @staticmethod
    def get_or_error(concept_key):
        """Come get(), ma solleva un errore esplicito invece di un None silenzioso —
        così un concetto non ancora configurato blocca l'operazione con un
        messaggio chiaro invece di un crash a valle o, peggio, una scrittura
        su un conto sbagliato."""
        account = AccountMapping.get(concept_key)
        if account is None:
            raise ValueError(
                f"Il concetto contabile '{concept_key}' non è configurato in "
                f"AccountMapping — contatta il commercialista prima di procedere."
            )
        return account


# ══════════════════════════════════════════════════════════════
# ANAGRAFICA ARTICOLI (Material Master — MM01 semplificato)
# ══════════════════════════════════════════════════════════════
class Material(db.Model):
    """
    Articolo con costo standard (per il Costo del Venduto all'uscita merci,
    come SAP) e prezzo di vendita. La giacenza è tenuta qui a quantità;
    il VALORE di magazzino vive nei conti G/L collegati al tipo articolo.
    """
    __tablename__ = "materials"

    TYPE_ACCOUNTS = {"ROH": "150000", "HALB": "155000", "FERT": "160000"}
    TYPE_LABELS = {"ROH": "Materia Prima", "HALB": "Semilavorato", "FERT": "Prodotto Finito"}

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(30), unique=True, nullable=False)
    description = db.Column(db.String(200), nullable=False)
    material_type = db.Column(db.String(5), nullable=False, default="FERT")  # ROH|HALB|FERT
    uom = db.Column(db.String(10), default="PZ")
    standard_cost = db.Column(db.Numeric(14, 4), nullable=False, default=0)   # costo del venduto
    sales_price = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=22)
    qty_on_hand = db.Column(db.Numeric(14, 3), nullable=False, default=0)
    # Scorta minima (reorder point) — soglia sotto la quale il cruscotto
    # Magazzino/Fabbisogno (stile MasterLogistic-WMS) segnala fabbisogno.
    reorder_point = db.Column(db.Numeric(14, 3), nullable=False, default=0)
    active = db.Column(db.Boolean, default=True)
    # Flag: questo articolo passa dal reparto carpenteria propria (taglio e
    # foratura di semilavorati, poi saldatura) — es. paletti parapedonali,
    # transenne, archetti parcheggi, cavalletti cartelli stradali, cartelli
    # stradali assemblati (pellicola su lamiera ferrosa acquistata). Serve per
    # sapere QUALI prodotti condividono il pool di costi indiretti di
    # carpenteria (taglio/foratura) quando lo si spalma in base al fatturato —
    # un prodotto comprato e rivenduto così com'è NON deve riceverne quota.
    is_carpenteria_propria = db.Column(db.Boolean, default=False, nullable=False)

    @property
    def type_label(self):
        return self.TYPE_LABELS.get(self.material_type, self.material_type)

    @property
    def inventory_account_code(self):
        return self.TYPE_ACCOUNTS.get(self.material_type, "160000")


# ══════════════════════════════════════════════════════════════
# CICLO ATTIVO SD — Preventivo → Ordine → DDT (PGI+COGS) → Fattura
# ══════════════════════════════════════════════════════════════
class Quotation(db.Model):
    """Preventivo cliente (VA21)."""
    __tablename__ = "quotations"
    id = db.Column(db.Integer, primary_key=True)
    doc_number = db.Column(db.String(20), unique=True, nullable=False)
    doc_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    economic_subject_id = db.Column(db.Integer, db.ForeignKey("economic_subjects.id"), nullable=True)  # FIX: allineato alla migrazione reale (nullable=True), che non ha imposto NOT NULL sulle righe storiche
    status = db.Column(db.String(15), default="aperto")  # aperto | convertito | scaduto
    note = db.Column(db.String(255))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    party = db.relationship("EconomicSubject")
    lines = db.relationship("QuotationLine", backref="quotation", cascade="all, delete-orphan")

    @property
    def total_net(self):
        return sum(float(l.qty) * float(l.price) for l in self.lines)


class QuotationLine(db.Model):
    __tablename__ = "quotation_lines"
    id = db.Column(db.Integer, primary_key=True)
    quotation_id = db.Column(db.Integer, db.ForeignKey("quotations.id"), nullable=False)
    material_id = db.Column(db.Integer, db.ForeignKey("materials.id"), nullable=False)
    qty = db.Column(db.Numeric(14, 3), nullable=False)
    price = db.Column(db.Numeric(14, 4), nullable=False)  # prezzo unitario netto
    material = db.relationship("Material")


class SalesOrder(db.Model):
    """Ordine cliente (VA01) — creato libero o da Preventivo (copy control)."""
    __tablename__ = "sales_orders"
    id = db.Column(db.Integer, primary_key=True)
    doc_number = db.Column(db.String(20), unique=True, nullable=False)
    doc_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    economic_subject_id = db.Column(db.Integer, db.ForeignKey("economic_subjects.id"), nullable=True)  # FIX: allineato alla migrazione reale (nullable=True), che non ha imposto NOT NULL sulle righe storiche
    quotation_id = db.Column(db.Integer, db.ForeignKey("quotations.id"), nullable=True)
    status = db.Column(db.String(15), default="aperto")  # aperto | consegnato
    note = db.Column(db.String(255))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Cruscotto Ordini Cliente stile MasterLogistic (logistics_bp):
    delivery_due_date = db.Column(db.Date, nullable=True)   # data consegna concordata col cliente
    confirmed = db.Column(db.Boolean, nullable=False, default=False)  # confermato col cliente (telefono/mail)
    priority = db.Column(db.Integer, nullable=False, default=0)  # ordine manuale (drag&drop) nel cruscotto

    party = db.relationship("EconomicSubject")
    quotation = db.relationship("Quotation", backref="orders")
    lines = db.relationship("SalesOrderLine", backref="order", cascade="all, delete-orphan")

    @property
    def total_net(self):
        return sum(float(l.qty) * float(l.price) for l in self.lines)

    @property
    def qty_delivered_total(self):
        return sum(float(l.qty_delivered or 0) for l in self.lines)


class SalesOrderLine(db.Model):
    __tablename__ = "sales_order_lines"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("sales_orders.id"), nullable=False)
    material_id = db.Column(db.Integer, db.ForeignKey("materials.id"), nullable=False)
    qty = db.Column(db.Numeric(14, 3), nullable=False)
    qty_delivered = db.Column(db.Numeric(14, 3), nullable=False, default=0)
    price = db.Column(db.Numeric(14, 4), nullable=False)
    material = db.relationship("Material")


class Delivery(db.Model):
    """
    DDT / Consegna (VL01N). Alla registrazione avviene l'USCITA MERCI (PGI):
    scarico giacenza + scrittura Costo del Venduto:
        Dare  Costo del Venduto (450000)
        Avere Magazzino Prodotti Finiti (160000)
    per qty × costo standard — esattamente come SAP (mov. 601).
    """
    __tablename__ = "deliveries"
    id = db.Column(db.Integer, primary_key=True)
    doc_number = db.Column(db.String(20), unique=True, nullable=False)
    doc_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    order_id = db.Column(db.Integer, db.ForeignKey("sales_orders.id"), nullable=False)
    economic_subject_id = db.Column(db.Integer, db.ForeignKey("economic_subjects.id"), nullable=True)  # FIX: allineato alla migrazione reale (nullable=True), che non ha imposto NOT NULL sulle righe storiche
    cogs_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    billing_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    # Fatture da Emettere (rateo attivo) — valorizzato quando il DDT è stato
    # spedito ma non ancora fatturato e si è generata la scrittura provvisoria
    # di competenza. Stornata automaticamente quando arriva la fattura vera
    # (billing()), per non contare il ricavo due volte.
    accrual_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Vero quando MasterLogistic-WMS ha davvero confermato la giacenza al
    # momento della spedizione; False quando il DDT è stato registrato col
    # bypass (WMS non ancora collegato) — segnalato bene in elenco.
    stock_verified = db.Column(db.Boolean, default=True)
    # Fase 4 (progettazione parti mancanti, punto 4) — storno di dominio
    is_reversed = db.Column(db.Boolean, default=False)
    reversal_reason = db.Column(db.String(255))
    reversed_at = db.Column(db.DateTime)
    reversed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    order = db.relationship("SalesOrder", backref="deliveries")
    party = db.relationship("EconomicSubject")
    cogs_entry = db.relationship("JournalEntry", foreign_keys=[cogs_entry_id])
    billing_entry = db.relationship("JournalEntry", foreign_keys=[billing_entry_id])
    lines = db.relationship("DeliveryLine", backref="delivery", cascade="all, delete-orphan")

    @property
    def total_net(self):
        return sum(float(l.qty) * float(l.price) for l in self.lines)

    @property
    def total_cogs(self):
        return sum(float(l.qty) * float(l.unit_cost) for l in self.lines)

    @property
    def is_billed(self):
        return self.billing_entry_id is not None


class DeliveryLine(db.Model):
    __tablename__ = "delivery_lines"
    id = db.Column(db.Integer, primary_key=True)
    delivery_id = db.Column(db.Integer, db.ForeignKey("deliveries.id"), nullable=False)
    material_id = db.Column(db.Integer, db.ForeignKey("materials.id"), nullable=False)
    # Riga d'ordine di origine — nullable per compatibilità con righe storiche
    # create prima di questo campo. Serve a ripristinare la riga ESATTA in
    # caso di storno: senza questo riferimento, un ordine con lo stesso
    # articolo su due righe (stesso SKU a prezzi diversi, nessun vincolo lo
    # impedisce) renderebbe ambiguo quale riga decrementare allo storno.
    sales_order_line_id = db.Column(db.Integer, db.ForeignKey("sales_order_lines.id"), nullable=True)
    qty = db.Column(db.Numeric(14, 3), nullable=False)
    price = db.Column(db.Numeric(14, 4), nullable=False)      # prezzo di vendita (dall'ordine)
    unit_cost = db.Column(db.Numeric(14, 4), nullable=False)  # costo standard AL MOMENTO del PGI
    material = db.relationship("Material")
    sales_order_line = db.relationship("SalesOrderLine")


# ══════════════════════════════════════════════════════════════
# CICLO PASSIVO MM — Ordine Acquisto → Entrata Merci → Verifica Fattura
# con THREE-WAY MATCH (Ordinato vs Ricevuto vs Fatturato)
# ══════════════════════════════════════════════════════════════
class PurchaseOrder(db.Model):
    """Ordine d'acquisto (ME21N)."""
    __tablename__ = "purchase_orders"
    id = db.Column(db.Integer, primary_key=True)
    doc_number = db.Column(db.String(20), unique=True, nullable=False)
    doc_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    economic_subject_id = db.Column(db.Integer, db.ForeignKey("economic_subjects.id"), nullable=True)  # FIX: allineato alla migrazione reale (nullable=True), che non ha imposto NOT NULL sulle righe storiche
    note = db.Column(db.String(255))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Cruscotto Ordini Fornitore stile MasterLogistic (logistics_bp):
    delivery_due_date = db.Column(db.Date, nullable=True)   # data consegna/ritiro concordata col fornitore
    confirmed = db.Column(db.Boolean, nullable=False, default=False)  # confermato dal fornitore
    pickup_mode = db.Column(db.String(20), nullable=False, default="consegnano_loro")  # consegnano_loro | ritiriamo_noi
    priority = db.Column(db.Integer, nullable=False, default=0)  # ordine manuale (drag&drop) nel cruscotto

    party = db.relationship("EconomicSubject")
    lines = db.relationship("PurchaseOrderLine", backref="po", cascade="all, delete-orphan")

    @property
    def total_net(self):
        return sum(float(l.qty) * float(l.price) for l in self.lines)

    @property
    def status(self):
        recv = sum(float(l.qty_received or 0) for l in self.lines)
        inv = sum(float(l.qty_invoiced or 0) for l in self.lines)
        tot = sum(float(l.qty) for l in self.lines)
        if inv >= tot and tot > 0:
            return "fatturato"
        if recv >= tot and tot > 0:
            return "ricevuto"
        if recv > 0:
            return "parz. ricevuto"
        return "aperto"


class PurchaseOrderLine(db.Model):
    __tablename__ = "purchase_order_lines"
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"), nullable=False)
    material_id = db.Column(db.Integer, db.ForeignKey("materials.id"), nullable=False)
    qty = db.Column(db.Numeric(14, 3), nullable=False)
    price = db.Column(db.Numeric(14, 4), nullable=False)          # prezzo ordine (base del match)
    qty_received = db.Column(db.Numeric(14, 3), nullable=False, default=0)
    qty_invoiced = db.Column(db.Numeric(14, 3), nullable=False, default=0)
    material = db.relationship("Material")


class GoodsReceipt(db.Model):
    """
    Entrata merci (MIGO mov. 101). Scrittura, come SAP:
        Dare  Magazzino (conto del tipo articolo)
        Avere Ricevimenti da fatturare — EM/RF (165000)
    al PREZZO ORDINE. Il conto EM/RF verrà chiuso dalla Verifica Fattura.
    """
    __tablename__ = "goods_receipts"
    id = db.Column(db.Integer, primary_key=True)
    doc_number = db.Column(db.String(20), unique=True, nullable=False)
    doc_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    po_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"), nullable=False)
    ddt_vendor_ref = db.Column(db.String(60))  # n. DDT del fornitore
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Fase 4 (progettazione parti mancanti, punto 4) — storno di dominio
    is_reversed = db.Column(db.Boolean, default=False)
    reversal_reason = db.Column(db.String(255))
    reversed_at = db.Column(db.DateTime)
    reversed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    po = db.relationship("PurchaseOrder", backref="receipts")
    journal_entry = db.relationship("JournalEntry", foreign_keys=[journal_entry_id])
    lines = db.relationship("GoodsReceiptLine", backref="receipt", cascade="all, delete-orphan")


class GoodsReceiptLine(db.Model):
    __tablename__ = "goods_receipt_lines"
    id = db.Column(db.Integer, primary_key=True)
    receipt_id = db.Column(db.Integer, db.ForeignKey("goods_receipts.id"), nullable=False)
    po_line_id = db.Column(db.Integer, db.ForeignKey("purchase_order_lines.id"), nullable=False)
    qty = db.Column(db.Numeric(14, 3), nullable=False)
    po_line = db.relationship("PurchaseOrderLine")


class InvoiceVerificationLine(db.Model):
    """Riga di dettaglio della Verifica Fattura (MIRO/three-way match) —
    stesso ruolo di GoodsReceiptLine ma per la fattura fornitore: senza
    questa tabella non c'era modo di sapere, dato un KR generato da MM,
    quali righe ordine e quali quantità aveva davvero fatturato — quindi
    nessun modo sicuro di ripristinare qty_invoiced se il documento
    veniva eliminato. Popolata da blueprints/mm/routes.py, invoice_verification()."""
    __tablename__ = "invoice_verification_lines"
    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=False)
    po_line_id = db.Column(db.Integer, db.ForeignKey("purchase_order_lines.id"), nullable=False)
    qty = db.Column(db.Numeric(14, 3), nullable=False)
    po_line = db.relationship("PurchaseOrderLine")


# ══════════════════════════════════════════════════════════════
# PRODUZIONE COMPLETATA (COGM) — soluzione PONTE finché non c'è
# MasterProduction. Registrazione periodica (tipicamente mensile) del
# Costo del Prodotto Finito (Cost of Goods Manufactured), a costo standard:
#
#   Dare  Magazzino Prodotti Finiti (160000)     = materie prime + manodopera + costi indiretti
#     Avere  Magazzino Materie Prime (150000)    = materie prime consumate (movimento di magazzino reale)
#     Avere  Variazione Rimanenze PF (430000)    = manodopera diretta + costi indiretti capitalizzati
#                                                  (la manodopera è già stata spesata a conto economico
#                                                   altrove — es. dalle buste paga — qui si "recupera"
#                                                   la quota che è finita a valore di magazzino, non persa)
#
# Quando MasterProduction sarà pronto, questa tabella diventa il punto in
# cui i dati arrivano in automatico invece che inseriti a mano — la
# struttura contabile sotto non cambia.
# ══════════════════════════════════════════════════════════════
class ProductionEntry(db.Model):
    __tablename__ = "production_entries"
    id = db.Column(db.Integer, primary_key=True)
    doc_number = db.Column(db.String(20), unique=True, nullable=False)
    doc_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    period_label = db.Column(db.String(30))  # es. "Luglio 2026" — solo descrittivo

    material_id = db.Column(db.Integer, db.ForeignKey("materials.id"), nullable=False)
    qty_produced = db.Column(db.Numeric(14, 3), nullable=False)

    raw_material_cost = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    direct_labor_cost = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    overhead_cost = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    notes = db.Column(db.String(300))

    # Se al momento della registrazione esisteva un Costo Standard per questo
    # materiale/periodo, il magazzino viene capitalizzato ALLO STANDARD (non
    # più al consuntivo) e queste tre varianze vengono postate e salvate qui
    # per tracciabilità. Restano a 0 se non c'era nessuno standard applicabile
    # (in quel caso si capitalizza al consuntivo come sempre, senza varianze).
    standard_cost_id = db.Column(db.Integer, db.ForeignKey("standard_costs.id"), nullable=True)
    variance_materiali = db.Column(db.Numeric(14, 2), nullable=False, default=0)   # >0 sfavorevole, <0 favorevole
    variance_manodopera = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    variance_overhead = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    material = db.relationship("Material")
    journal_entry = db.relationship("JournalEntry")
    standard_cost = db.relationship("StandardCost")

    @property
    def total_cogm(self):
        return (self.raw_material_cost or 0) + (self.direct_labor_cost or 0) + (self.overhead_cost or 0)

    @property
    def usa_standard(self):
        return self.standard_cost_id is not None


class StandardCost(db.Model):
    """
    Costo Standard di un prodotto finito, FISSATO IN ANTICIPO (es. a inizio
    mese/anno) — il prerequisito per fare le varianze di produzione alla SAP.
    A differenza del costo consuntivo (quello che si registra volta per volta
    in Produzione Completata), questo è un valore di RIFERIMENTO deciso PRIMA,
    con cui il consuntivo verrà confrontato per calcolare le varianze:

        Varianza Materiali   = costo materiali CONSUNTIVO - costo materiali STANDARD
        Varianza Manodopera  = costo manodopera CONSUNTIVO - costo manodopera STANDARD
        Varianza Overhead    = costo overhead CONSUNTIVO - costo overhead STANDARD

    (positivo = sfavorevole, si è speso più del previsto; negativo = favorevole)

    Un valore >0 di ciascuna componente STANDARD è "per unità prodotta" (costo
    standard unitario), moltiplicato per qty_produced al momento del confronto.
    """
    __tablename__ = "standard_costs"
    id = db.Column(db.Integer, primary_key=True)
    material_id = db.Column(db.Integer, db.ForeignKey("materials.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)  # 1-12: valido da questo mese in poi, fino al prossimo standard dello stesso materiale

    standard_material_cost = db.Column(db.Numeric(14, 4), nullable=False, default=0)   # € per unità
    standard_labor_cost = db.Column(db.Numeric(14, 4), nullable=False, default=0)      # € per unità
    standard_overhead_cost = db.Column(db.Numeric(14, 4), nullable=False, default=0)   # € per unità

    notes = db.Column(db.String(300))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    material = db.relationship("Material")

    @property
    def standard_total_unitario(self):
        return (self.standard_material_cost or 0) + (self.standard_labor_cost or 0) + (self.standard_overhead_cost or 0)


class ProductCostTarget(db.Model):
    """Costo obiettivo unitario, versionato per prodotto e data di decorrenza.

    Non sovrascrive mai il target precedente: l'analisi sceglie l'ultima
    versione valida alla data di fine del periodo, conservando la storia dei
    budget e rendendo il confronto ripetibile in audit.
    """
    __tablename__ = "product_cost_targets"
    id = db.Column(db.Integer, primary_key=True)
    material_id = db.Column(db.Integer, db.ForeignKey("materials.id"), nullable=False, index=True)
    effective_date = db.Column(db.Date, nullable=False, index=True)
    target_material_cost = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    target_labor_cost = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    target_overhead_cost = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    notes = db.Column(db.String(300))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    material = db.relationship("Material")
    created_by = db.relationship("User")

    __table_args__ = (
        db.UniqueConstraint("material_id", "effective_date", name="uq_product_cost_target_material_date"),
    )

    @property
    def target_total_unitario(self):
        return ((self.target_material_cost or 0) + (self.target_labor_cost or 0) +
                (self.target_overhead_cost or 0))


class ProductionOverheadItem(db.Model):
    """
    Voce singola del pool di costi indiretti di REPARTO (Livello 1: taglio,
    foratura, assemblaggio, confezionamento) per un dato mese — es.
    "Ammortamento macchina taglio: 300€". Inserite a mano da Mauri, voce per
    voce, UNA VOLTA AL MESE (non per singolo prodotto): la somma di queste
    voci è il pool condiviso da cui ogni prodotto di carpenteria propria
    riceve la propria quota, in proporzione al fatturato, quando si registra
    una Produzione Completata (vedi _calcola_overhead_da_fatturato).
    """
    __tablename__ = "production_overhead_items"
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)   # 1-12
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    # Centro di lavoro a cui è assegnata la voce — nullable per compatibilità
    # con le voci storiche inserite prima del metodo SAP (routing_cost.py):
    # quelle restano nel pool "non ripartito" e NON entrano nel calcolo
    # tariffa oraria di nessun centro finché non vengono riassegnate.
    work_center_id = db.Column(db.Integer, db.ForeignKey("work_centers.id"), nullable=True, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    work_center = db.relationship("WorkCenter")


class OverheadAdjustment(db.Model):
    """
    Correzione manuale (rateo o risconto) al calcolo AUTOMATICO dell'overhead
    generale aziendale (Livello 2 — vedi _calcola_overhead_generale). Un
    rateo/risconto tipico: un costo di competenza del mese non ancora
    fatturato/registrato (rateo, amount positivo) o un costo già registrato
    ma di competenza di mesi futuri (risconto, amount negativo).
    """
    __tablename__ = "overhead_adjustments"
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)   # 1-12
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)  # + aumenta l'overhead, - lo riduce
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ══════════════════════════════════════════════════════════════
# PAGHE / F24 — import PDF con revisione prima della contabilizzazione
# ══════════════════════════════════════════════════════════════
class PayrollEmployeeMapping(db.Model):
    __tablename__ = "payroll_employee_mappings"
    id = db.Column(db.Integer, primary_key=True)
    employee_key = db.Column(db.String(80), unique=True, nullable=False)  # CF, oppure codice Zucchetti
    employee_name = db.Column(db.String(160), nullable=False)
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    cost_center = db.relationship("CostCenter")


class PayrollAccountConfig(db.Model):
    __tablename__ = "payroll_account_configs"
    id = db.Column(db.Integer, primary_key=True)
    wage_expense_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    employer_burden_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    net_salary_payable_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    inps_payable_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    withholding_payable_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    bank_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    imu_expense_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    accrued_holiday_expense_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    accrued_permission_expense_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    accrued_thirteenth_expense_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    accrued_payable_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    tfr_expense_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    tfr_fund_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    # Aliquote SOLO come comodo valore di precompilazione in revisione: l'importo
    # che viene davvero registrato è sempre quello (eventualmente corretto a mano)
    # visibile e modificabile riga per riga nella maschera di revisione — mai
    # un'aliquota applicata alla cieca in fase di contabilizzazione.
    employee_inps_rate = db.Column(db.Numeric(5, 2), nullable=True)      # es. 9.19 — quota INPS a carico dipendente, dentro le trattenute
    employer_contribution_rate = db.Column(db.Numeric(5, 2), nullable=True)  # es. 30.00 — oneri INPS/INAIL a carico azienda, NON dentro il lordo busta
    wage_expense_account = db.relationship("Account", foreign_keys=[wage_expense_account_id])
    employer_burden_account = db.relationship("Account", foreign_keys=[employer_burden_account_id])
    net_salary_payable_account = db.relationship("Account", foreign_keys=[net_salary_payable_account_id])
    inps_payable_account = db.relationship("Account", foreign_keys=[inps_payable_account_id])
    withholding_payable_account = db.relationship("Account", foreign_keys=[withholding_payable_account_id])
    bank_account = db.relationship("Account", foreign_keys=[bank_account_id])
    imu_expense_account = db.relationship("Account", foreign_keys=[imu_expense_account_id])
    accrued_holiday_expense_account = db.relationship("Account", foreign_keys=[accrued_holiday_expense_account_id])
    accrued_permission_expense_account = db.relationship("Account", foreign_keys=[accrued_permission_expense_account_id])
    accrued_thirteenth_expense_account = db.relationship("Account", foreign_keys=[accrued_thirteenth_expense_account_id])
    accrued_payable_account = db.relationship("Account", foreign_keys=[accrued_payable_account_id])
    tfr_expense_account = db.relationship("Account", foreign_keys=[tfr_expense_account_id])
    tfr_fund_account = db.relationship("Account", foreign_keys=[tfr_fund_account_id])


class F24ImuMapping(db.Model):
    """Optional remembered default for an IMU municipality/tribute pair."""
    __tablename__ = "f24_imu_mappings"
    id = db.Column(db.Integer, primary_key=True)
    municipality_code = db.Column(db.String(8), nullable=False)
    tribute_code = db.Column(db.String(12), nullable=False)
    expense_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint("municipality_code", "tribute_code", name="uq_f24_imu_mapping"),)
    expense_account = db.relationship("Account")
    cost_center = db.relationship("CostCenter")


class PayrollImport(db.Model):
    __tablename__ = "payroll_imports"
    id = db.Column(db.Integer, primary_key=True)
    document_kind = db.Column(db.String(12), nullable=False)  # PAYSLIP, F24
    filename = db.Column(db.String(255), nullable=False)
    fingerprint = db.Column(db.String(64), nullable=False, unique=True)
    document_reference = db.Column(db.String(120), nullable=True)
    document_date = db.Column(db.Date, nullable=True)
    parsed_data = db.Column(db.Text, nullable=False)  # reviewed extraction snapshot, JSON
    status = db.Column(db.String(20), nullable=False, default="review")
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    posted_at = db.Column(db.DateTime, nullable=True)
    journal_entry = db.relationship("JournalEntry")

class PayrollEmployeeAllocation(db.Model):
    """Percentual split; legacy PayrollEmployeeMapping.cost_center_id remains readable."""
    __tablename__ = "payroll_employee_allocations"
    id = db.Column(db.Integer, primary_key=True)
    mapping_id = db.Column(db.Integer, db.ForeignKey("payroll_employee_mappings.id"), nullable=False)
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=False)
    percentage = db.Column(db.Numeric(5, 2), nullable=False)
    mapping = db.relationship("PayrollEmployeeMapping", backref=db.backref("allocations", cascade="all, delete-orphan"))
    cost_center = db.relationship("CostCenter")
    __table_args__ = (db.UniqueConstraint("mapping_id", "cost_center_id", name="uq_payroll_mapping_center"),)


class AllocationSplit(db.Model):
    """Generic future-ready allocation for AP/AR documents and commercial lines."""
    __tablename__ = "allocation_splits"
    id = db.Column(db.Integer, primary_key=True)
    document_type = db.Column(db.String(30), nullable=False)
    document_id = db.Column(db.Integer, nullable=False)
    document_line_id = db.Column(db.Integer, nullable=True)
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=False)
    percentage = db.Column(db.Numeric(5, 2), nullable=False)
    cost_center = db.relationship("CostCenter")
    __table_args__ = (db.UniqueConstraint("document_type", "document_id", "document_line_id", "cost_center_id", name="uq_allocation_split_target_center"),)

# ══════════════════════════════════════════════════════════════
# COMMESSE DI PRODUZIONE / WIP — ordine, prelievi e versamento PF
# ══════════════════════════════════════════════════════════════
class ProductionOrder(db.Model):
    """Commessa/ordine di produzione. L'apertura non genera movimenti FI;
    i movimenti nascono dai consuntivi: prelievo, assorbimento e versamento PF."""
    __tablename__ = "production_orders"
    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(30), unique=True, nullable=False)
    order_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    material_id = db.Column(db.Integer, db.ForeignKey("materials.id"), nullable=False)
    qty_planned = db.Column(db.Numeric(14, 3), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="rilasciata")  # rilasciata|in_lavorazione|completata
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=True)
    notes = db.Column(db.String(300))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    material = db.relationship("Material")
    cost_center = db.relationship("CostCenter")
    issues = db.relationship("ProductionMaterialIssue", backref="production_order", cascade="all, delete-orphan")
    absorptions = db.relationship("ProductionCostAbsorption", backref="production_order", cascade="all, delete-orphan")

    @property
    def actual_wip(self):
        return sum((i.total_cost for i in self.issues), Decimal("0")) + sum((a.amount for a in self.absorptions), Decimal("0"))


class ProductionMaterialIssue(db.Model):
    """Prelievo componenti alla commessa: Dare WIP / Avere magazzino componente."""
    __tablename__ = "production_material_issues"
    id = db.Column(db.Integer, primary_key=True)
    production_order_id = db.Column(db.Integer, db.ForeignKey("production_orders.id"), nullable=False)
    material_id = db.Column(db.Integer, db.ForeignKey("materials.id"), nullable=False)
    qty = db.Column(db.Numeric(14, 3), nullable=False)
    unit_cost = db.Column(db.Numeric(14, 4), nullable=False)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=False)
    issue_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    material = db.relationship("Material")
    journal_entry = db.relationship("JournalEntry")
    @property
    def total_cost(self): return Decimal(str(self.qty)) * Decimal(str(self.unit_cost))


class ProductionCostAbsorption(db.Model):
    """MOD o overhead assorbito: Dare WIP / Avere conto di assorbimento."""
    __tablename__ = "production_cost_absorptions"
    id = db.Column(db.Integer, primary_key=True)
    production_order_id = db.Column(db.Integer, db.ForeignKey("production_orders.id"), nullable=False)
    cost_type = db.Column(db.String(15), nullable=False)  # MOD | OVERHEAD
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=False)
    posting_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    notes = db.Column(db.String(255))
    journal_entry = db.relationship("JournalEntry")


# ══════════════════════════════════════════════════════════════
# RFQ MM — richiesta d'offerta, confronto, scelta e conversione in OA
# ══════════════════════════════════════════════════════════════
class RequestForQuotation(db.Model):
    __tablename__ = "requests_for_quotation"
    id = db.Column(db.Integer, primary_key=True)
    rfq_number = db.Column(db.String(30), unique=True, nullable=False)
    request_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    material_id = db.Column(db.Integer, db.ForeignKey("materials.id"), nullable=False)
    qty = db.Column(db.Numeric(14, 3), nullable=False)
    required_date = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="aperta")  # aperta|aggiudicata|ordinata
    notes = db.Column(db.String(300))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    material = db.relationship("Material")
    offers = db.relationship("SupplierQuotation", backref="rfq", cascade="all, delete-orphan")


class RfqDelivery(db.Model):
    """Traccia ogni inoltro di una RFQ a un fornitore selezionato."""
    __tablename__ = "rfq_deliveries"
    id = db.Column(db.Integer, primary_key=True)
    rfq_id = db.Column(db.Integer, db.ForeignKey("requests_for_quotation.id"), nullable=False, index=True)
    economic_subject_id = db.Column(db.Integer, db.ForeignKey("economic_subjects.id"), nullable=False)
    recipient_email = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="inviata")  # inviata|errore
    error_message = db.Column(db.String(500))
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    sent_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    rfq = db.relationship("RequestForQuotation", backref=db.backref("deliveries", cascade="all, delete-orphan"))
    supplier = db.relationship("EconomicSubject")
    sent_by = db.relationship("User")


class SupplierQuotation(db.Model):
    __tablename__ = "supplier_quotations"
    id = db.Column(db.Integer, primary_key=True)
    rfq_id = db.Column(db.Integer, db.ForeignKey("requests_for_quotation.id"), nullable=False)
    economic_subject_id = db.Column(db.Integer, db.ForeignKey("economic_subjects.id"), nullable=False)
    offer_ref = db.Column(db.String(60))
    unit_price = db.Column(db.Numeric(14, 4), nullable=False)
    lead_days = db.Column(db.Integer, nullable=True)
    valid_until = db.Column(db.Date, nullable=True)
    selected = db.Column(db.Boolean, nullable=False, default=False)
    purchase_order_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    supplier = db.relationship("EconomicSubject")
    purchase_order = db.relationship("PurchaseOrder")
