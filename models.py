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

    # Anagrafica collegata (per filtrare "fatture aperte per fornitore/cliente")
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"), nullable=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True)
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

    vendor = db.relationship("Vendor")
    customer = db.relationship("Customer")

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
class Vendor(db.Model):
    __tablename__ = "vendors"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    piva = db.Column(db.String(20))
    payment_terms = db.Column(db.String(40), default="Netto 30gg")
    active = db.Column(db.Boolean, default=True)


class Customer(db.Model):
    __tablename__ = "customers"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    piva = db.Column(db.String(20))
    payment_terms = db.Column(db.String(40), default="Netto 30gg")
    active = db.Column(db.Boolean, default=True)

    # ── Dati anagrafici/fiscali necessari per generare l'XML FatturaPA
    # (CessionarioCommittente) — vedi services/fatturapa.py. Nessuno di
    # questi è richiesto per usare l'app come simulatore didattico: servono
    # solo quando si genera davvero l'XML da mandare in conservazione/SdI
    # (tramite upload manuale su Aruba o altro intermediario).
    codice_fiscale = db.Column(db.String(16))
    indirizzo = db.Column(db.String(120))
    cap = db.Column(db.String(10))
    comune = db.Column(db.String(80))
    provincia = db.Column(db.String(2))   # sigla, es. "MI" — non richiesta se nazione != IT
    nazione = db.Column(db.String(2), default="IT")

    # CodiceDestinatario a 7 cifre (default "0000000" = recapito solo tramite
    # PEC, come richiesto dalle specifiche tecniche SdI quando il cliente
    # non ha un codice destinatario proprio).
    codice_destinatario = db.Column(db.String(7), default="0000000")
    pec_destinatario = db.Column(db.String(120))



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
