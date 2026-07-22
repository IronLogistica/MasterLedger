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

    warehouse_areas = db.relationship("WarehouseArea", backref="site", cascade="all, delete-orphan")


class WarehouseArea(db.Model):
    """
    Un'area di magazzino all’interno di una sede operativa — il vero "Setup dei
    Magazzini" richiesto: definisce nome, tipo e soprattutto il conto G/L
    di magazzino a cui l'area è collegata, per studiare lo stoccaggio
    corretto (materie prime vs prodotti finiti vs blocco qualità, ecc.)
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

    __table_args__ = (db.UniqueConstraint("site_id", "code", name="uq_site_sloc_code"),)

    @property
    def area_type_label(self):
        return self.AREA_TYPES.get(self.area_type, self.area_type)


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
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
    qty = db.Column(db.Numeric(14, 3), nullable=False)
    price = db.Column(db.Numeric(14, 4), nullable=False)      # prezzo di vendita (dall'ordine)
    unit_cost = db.Column(db.Numeric(14, 4), nullable=False)  # costo standard AL MOMENTO del PGI
    material = db.relationship("Material")


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

    po = db.relationship("PurchaseOrder", backref="receipts")
    journal_entry = db.relationship("JournalEntry")
    lines = db.relationship("GoodsReceiptLine", backref="receipt", cascade="all, delete-orphan")


class GoodsReceiptLine(db.Model):
    __tablename__ = "goods_receipt_lines"
    id = db.Column(db.Integer, primary_key=True)
    receipt_id = db.Column(db.Integer, db.ForeignKey("goods_receipts.id"), nullable=False)
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
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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
