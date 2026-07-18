"""
services/fatturapa.py — Generatore XML FatturaPA a partire da un documento
Fattura cliente (fattura cliente) già registrato in Prima Nota.

IMPORTANTE — cosa fa e cosa NON fa questo modulo:
  - Genera un file XML conforme al tracciato FatturaPA (schema versione
    1.2.x, formato FPR12 — fattura verso privati/B2B), pronto per essere
    firmato e trasmesso allo SdI (Sistema di Interscambio).
  - NON firma digitalmente il file, NON lo trasmette e NON gestisce la
    conservazione a norma: questi tre passaggi restano affidati
    all'intermediario (es. pannello Fatturazione Elettronica di Aruba,
    dove il file va semplicemente CARICATO — vedi guida "Carica Fatture").
  - Prerequisito: il file caricato su Aruba deve essere XML già conforme
    al tracciato ufficiale — questo modulo esiste esattamente per produrre
    quel file, in modo che l'unico passaggio manuale rimasto sia l'upload.

Dati necessari (raccolti da tre punti diversi dell'applicazione):
  - Dati del Cedente/Prestatore (la tua azienda): letti dai FiscalParameter
    di categoria "fatturazione elettronica" — Configurazione Fiscale,
    riservata al Commercialista.
  - Dati del Cessionario/Committente (il cliente): letti dall'anagrafica
    EconomicSubject — vedi blueprints/ar/routes.py, rotta "dati fiscali cliente".
  - Dati del documento: letti dal JournalEntry (doc_type="DR") e dalle sue
    JournalLine.

Se manca un dato obbligatorio, viene sollevata FatturaPAConfigError con un
messaggio che dice ESATTAMENTE dove andare a compilarlo — niente XML non
valido generato "a metà".
"""
import io
import re
from decimal import Decimal, ROUND_HALF_UP
from xml.etree import ElementTree as ET
from xml.dom import minidom

from models import FiscalParameter, Account

NS_P = "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"

# Chiavi FiscalParameter per i dati del Cedente/Prestatore (la tua azienda).
# Vengono create con valore vuoto/di default da dashboard/routes.py la prima
# volta che si apre "Configurazione Fiscale" — qui le rileggiamo per nome.
CEDENTE_KEYS = {
    "denominazione": "fe_denominazione",
    "piva": "fe_piva",
    "codice_fiscale": "fe_codice_fiscale",
    "regime_fiscale": "fe_regime_fiscale",
    "indirizzo": "fe_indirizzo",
    "cap": "fe_cap",
    "comune": "fe_comune",
    "provincia": "fe_provincia",
    "nazione": "fe_nazione",
    "iban": "fe_iban",
}


class FatturaPAConfigError(Exception):
    """Sollevata quando mancano dati obbligatori per generare un XML valido."""
    pass


def _fiscal_params_map():
    rows = FiscalParameter.query.filter(FiscalParameter.key.in_(CEDENTE_KEYS.values())).all()
    by_key = {r.key: (r.value or "").strip() for r in rows}
    return {label: by_key.get(param_key, "") for label, param_key in CEDENTE_KEYS.items()}


def _next_progressivo_invio():
    """
    Contatore separato dalla numerazione fatture (DocumentSequence): il
    tracciato SdI richiede un ProgressivoInvio univoco per OGNI FILE
    trasmesso (max 5 caratteri alfanumerici), concettualmente diverso dal
    numero fattura. Lo teniamo — per semplicità, coerente col resto
    dell'app — come un FiscalParameter numerico incrementale.

    NOTA PER CHI ESTENDE QUESTO CODICE: come già segnalato per
    DocumentSequence.next_number(), questo incremento non è a prova di
    alta concorrenza. Va bene per un uso mono-operatore; prima di un
    volume serio, sostituire con una SEQUENCE nativa del DB.
    """
    from extensions import db

    param = FiscalParameter.query.filter_by(key="fe_progressivo_invio_counter").first()
    if param is None:
        param = FiscalParameter(
            key="fe_progressivo_invio_counter", value="0",
            description="Contatore interno ProgressivoInvio XML FatturaPA (non modificare a mano)",
            category="fatturazione elettronica",
        )
        db.session.add(param)

    current = int(param.value or "0")
    current += 1
    if current > 60466175:
        # 60.466.175 = ZZZZZ in base 36: limite teorico dei 5 caratteri
        # alfanumerici ammessi dal tracciato. Oltre, meglio fermarsi che
        # riciclare un nome file: lo SdI scarta PER SEMPRE i nomi già
        # trasmessi (errore 00002), anche a distanza di anni.
        raise FatturaPAConfigError(
            "Esaurito lo spazio dei ProgressivoInvio (oltre 60 milioni di file): "
            "il contatore non può essere riciclato senza generare nomi file "
            "duplicati, che lo SdI scarta con errore 00002."
        )
    param.value = str(current)
    db.session.commit()

    # Codifica in base 36 (0-9, A-Z) su 5 caratteri: la specifica ammette
    # progressivi alfanumerici, quindi 36^5 combinazioni invece delle sole
    # 99.999 puramente numeriche — nessun rischio di nome file duplicato.
    digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    n, out = current, ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out.rjust(5, "0")


def _sub(parent, tag, text=None):
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def _money(value):
    """
    Formato importo a 2 decimali con arrotondamento HALF_UP, come richiesto
    dalle specifiche SdI (controllo 00421: "arrotondato alla seconda cifra
    decimale, per difetto se la terza cifra decimale è inferiore a 5, per
    eccesso se uguale o superiore a 5"). La formattazione f"{x:.2f}" su
    float usa invece il round-half-even e può differire di un centesimo.
    """
    return str(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _derive_invoice_amounts(entry):
    """
    Ricostruisce imponibile/IVA/aliquota dalle righe di Prima Nota generate
    da Fattura cliente (vedi blueprints/ar/routes.py): Avere sul conto Ricavi = netto,
    Avere sul conto 170000 (IVA a Debito) = imposta.
    """
    net = 0.0
    vat = 0.0
    for line in entry.lines:
        acc = line.account
        if acc is None:
            continue
        if acc.account_type == "ricavo":
            net += float(line.avere or 0)
        elif acc.code == "170000":
            vat += float(line.avere or 0)

    if entry.vat_rate is not None:
        vat_rate = float(entry.vat_rate)
    elif net:
        vat_rate = round(vat / net * 100, 2)
    else:
        vat_rate = 0.0

    gross = float(entry.gross_amount) if entry.gross_amount is not None else (net + vat)
    return net, vat, vat_rate, gross


def build_fatturapa_xml(entry):
    """
    Costruisce l'XML FatturaPA (formato FPR12) per un JournalEntry DR
    (fattura cliente) generato da Fattura cliente.

    Ritorna: (filename, xml_bytes)
    Solleva: FatturaPAConfigError se mancano dati obbligatori (cedente o
             cliente), ValueError se il documento non è una fattura cliente.
    """
    if entry.doc_type not in ("DR", "DG"):
        raise ValueError("L'XML FatturaPA si genera solo per fatture cliente (Fattura cliente/DR) "
                         "e note di credito cliente (Nota di credito cliente/DG).")
    tipo_documento = "TD01" if entry.doc_type == "DR" else "TD04"

    customer = entry.party
    if customer is None:
        raise FatturaPAConfigError(
            "Questo documento non ha un Cliente collegato: impossibile generare l'XML."
        )

    cedente = _fiscal_params_map()

    # ── Validazione dati obbligatori del Cedente/Prestatore ─────────────
    missing_cedente = [
        label for label in ("denominazione", "piva", "regime_fiscale", "indirizzo", "cap", "comune", "provincia")
        if not cedente.get(label)
    ]
    if missing_cedente:
        raise FatturaPAConfigError(
            "Dati azienda incompleti per generare l'XML FatturaPA (mancano: "
            + ", ".join(missing_cedente)
            + "). Vai in Configurazione Fiscale → sezione \"Fatturazione elettronica\" e compilali."
        )

    # ── Validazione dati obbligatori del cliente ────────────────────────
    # NOTA sul recapito (par. 1.5.5 delle specifiche): CodiceDestinatario
    # "0000000" SENZA PEC è perfettamente valido — in quel caso lo SdI
    # "mette a disposizione il file fattura nell'area autenticata dei
    # servizi telematici del cessionario/committente" (cassetto fiscale).
    # La PEC quindi NON è obbligatoria: se presente viene usata come
    # canale di recapito, se assente la fattura finisce in area riservata.
    missing_customer = []
    if not customer.piva and not customer.codice_fiscale:
        missing_customer.append("Partita IVA o Codice Fiscale")  # scarto SdI 00417
    if not customer.indirizzo:
        missing_customer.append("Indirizzo")
    if not customer.cap:
        missing_customer.append("CAP")
    if not customer.comune:
        missing_customer.append("Comune")
    codice_dest = (customer.codice_destinatario or "0000000").strip().upper() or "0000000"
    if missing_customer:
        raise FatturaPAConfigError(
            f"Dati fiscali incompleti per il cliente '{customer.name}' (mancano: "
            + ", ".join(missing_customer)
            + f"). Vai su Cliente → \"Dati fiscali fatturazione elettronica\" e compilali."
        )

    # ── Controlli di formato (evitano scarti a livello di schema/SdI) ───
    if customer.cap and not re.fullmatch(r"\d{5}", customer.cap.strip()):
        raise FatturaPAConfigError(
            f"CAP '{customer.cap}' non valido per il cliente '{customer.name}': "
            "il tracciato richiede esattamente 5 cifre numeriche (CAPType)."
        )
    if not re.fullmatch(r"[A-Z0-9]{7}", codice_dest):
        raise FatturaPAConfigError(
            f"CodiceDestinatario '{codice_dest}' non valido: per il formato FPR12 "
            "deve essere di esattamente 7 caratteri alfanumerici (scarto SdI 00427/00311)."
        )
    if not re.fullmatch(r"\d{5}", (cedente.get("cap") or "").strip()):
        raise FatturaPAConfigError(
            "Il CAP della tua azienda in Configurazione Fiscale deve essere di 5 cifre numeriche."
        )
    if (customer.nazione or "IT").upper() != "IT":
        raise FatturaPAConfigError(
            f"Il cliente '{customer.name}' ha nazione {customer.nazione}: questo modulo "
            "genera solo fatture verso soggetti italiani. Per gli esteri il tracciato "
            "richiede CodiceDestinatario 'XXXXXXX', IdPaese estero e regole diverse "
            "(par. 3.1 delle specifiche) — flusso non ancora implementato."
        )

    net, vat, vat_rate, gross = _derive_invoice_amounts(entry)

    # ── Righe commerciali: multi-riga (InvoiceLine) o legacy mono-riga ──
    inv_lines = list(getattr(entry, "invoice_lines", []) or [])
    if inv_lines:
        # Riepiloghi per coppia (aliquota, natura) come da controlli SdI
        # 00419 (un DatiRiepilogo per ogni aliquota) e 00422 (imponibile
        # del riepilogo = somma dei PrezzoTotale con quella aliquota).
        groups = {}
        for l in inv_lines:
            rate = Decimal(str(l.vat_rate or 0))
            nat = (l.natura or "").strip() or None
            if rate == 0 and not nat:
                raise FatturaPAConfigError(
                    f"La riga {l.line_number} del documento {entry.doc_number} ha aliquota 0% "
                    "senza codice Natura: lo SdI scarta con errore 00400/00429."
                )
            if rate != 0:
                nat = None  # Natura con aliquota > 0 → scarto 00430
            groups.setdefault((rate, nat), Decimal("0"))
            groups[(rate, nat)] += Decimal(str(l.amount))
        riepiloghi = []
        total_net = Decimal("0")
        total_vat = Decimal("0")
        for (rate, nat), imponibile in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
            imposta = Decimal(str(imponibile * rate / Decimal("100"))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
            riepiloghi.append({"rate": rate, "natura": nat,
                               "imponibile": imponibile, "imposta": imposta})
            total_net += imponibile
            total_vat += imposta
        gross = total_net + total_vat
        natura = None  # gestita per riga/riepilogo, non a livello documento
    else:
        # Fatture registrate prima del multi-riga: una riga, un'aliquota.
        natura = (getattr(entry, "natura", None) or "").strip() or None
        if vat_rate == 0 and not natura:
            raise FatturaPAConfigError(
                f"La fattura {entry.doc_number} ha aliquota IVA 0% ma nessun codice Natura: "
                "le specifiche SdI impongono la Natura che giustifica la non imponibilità "
                "(scarti 00400/00429). Registra la fattura da Fattura cliente indicando il codice "
                "Natura (es. N4 per le esenti art. 10)."
            )
        if vat_rate != 0:
            natura = None  # Natura con aliquota > 0 → scarto 00430
        riepiloghi = None
    invoice_number = (entry.reference or entry.doc_number).strip()
    invoice_date = entry.doc_date.isoformat()
    description = entry.description or f"Fattura {invoice_number}"

    progressivo_invio = _next_progressivo_invio()

    # ══════════════════════════════════════════════════════════════════
    # Costruzione XML
    # ══════════════════════════════════════════════════════════════════
    ET.register_namespace("p", NS_P)
    ET.register_namespace("xsi", NS_XSI)

    root = ET.Element(f"{{{NS_P}}}FatturaElettronica", attrib={
        "versione": "FPR12",
        f"{{{NS_XSI}}}schemaLocation": f"{NS_P} Schema_del_file_xml_FatturaPA_v1.2.2.xsd",
    })

    # ── HEADER ───────────────────────────────────────────────────────
    header = _sub(root, "FatturaElettronicaHeader")

    dati_trasm = _sub(header, "DatiTrasmissione")
    id_trasm = _sub(dati_trasm, "IdTrasmittente")
    _sub(id_trasm, "IdPaese", "IT")
    _sub(id_trasm, "IdCodice", cedente["piva"])
    _sub(dati_trasm, "ProgressivoInvio", progressivo_invio)
    _sub(dati_trasm, "FormatoTrasmissione", "FPR12")
    _sub(dati_trasm, "CodiceDestinatario", codice_dest)
    # PECDestinatario: va emesso SOLO con CodiceDestinatario = 0000000
    # (altrimenti scarto 00426) e SOLO se effettivamente presente — con
    # 0000000 senza PEC lo SdI deposita la fattura in area riservata.
    if codice_dest == "0000000" and customer.pec_destinatario:
        _sub(dati_trasm, "PECDestinatario", customer.pec_destinatario)

    cedente_el = _sub(header, "CedentePrestatore")
    cedente_anag = _sub(cedente_el, "DatiAnagrafici")
    cedente_iva = _sub(cedente_anag, "IdFiscaleIVA")
    _sub(cedente_iva, "IdPaese", "IT")
    _sub(cedente_iva, "IdCodice", cedente["piva"])
    if cedente.get("codice_fiscale"):
        _sub(cedente_anag, "CodiceFiscale", cedente["codice_fiscale"])
    anagrafica = _sub(cedente_anag, "Anagrafica")
    _sub(anagrafica, "Denominazione", cedente["denominazione"])
    _sub(cedente_anag, "RegimeFiscale", cedente["regime_fiscale"])
    sede_cedente = _sub(cedente_el, "Sede")
    _sub(sede_cedente, "Indirizzo", cedente["indirizzo"])
    _sub(sede_cedente, "CAP", cedente["cap"])
    _sub(sede_cedente, "Comune", cedente["comune"])
    if cedente.get("provincia"):
        _sub(sede_cedente, "Provincia", cedente["provincia"])
    _sub(sede_cedente, "Nazione", cedente.get("nazione") or "IT")

    cessionario_el = _sub(header, "CessionarioCommittente")
    cess_anag = _sub(cessionario_el, "DatiAnagrafici")
    if customer.piva:
        cess_iva = _sub(cess_anag, "IdFiscaleIVA")
        _sub(cess_iva, "IdPaese", "IT")
        _sub(cess_iva, "IdCodice", customer.piva)
    if customer.codice_fiscale:
        _sub(cess_anag, "CodiceFiscale", customer.codice_fiscale)
    cess_anagrafica = _sub(cess_anag, "Anagrafica")
    _sub(cess_anagrafica, "Denominazione", customer.name)
    sede_cliente = _sub(cessionario_el, "Sede")
    _sub(sede_cliente, "Indirizzo", customer.indirizzo)
    _sub(sede_cliente, "CAP", customer.cap)
    _sub(sede_cliente, "Comune", customer.comune)
    if customer.provincia:
        _sub(sede_cliente, "Provincia", customer.provincia)
    _sub(sede_cliente, "Nazione", customer.nazione or "IT")

    # ── BODY ─────────────────────────────────────────────────────────
    body = _sub(root, "FatturaElettronicaBody")

    dati_generali = _sub(body, "DatiGenerali")
    dgd = _sub(dati_generali, "DatiGeneraliDocumento")
    _sub(dgd, "TipoDocumento", tipo_documento)
    _sub(dgd, "Divisa", "EUR")
    _sub(dgd, "Data", invoice_date)
    _sub(dgd, "Numero", invoice_number)
    _sub(dgd, "ImportoTotaleDocumento", _money(gross))

    # Nota di credito: riferimento alla fattura rettificata. Nell'XSD
    # (DatiGeneraliType) DatiFattureCollegate segue DatiGeneraliDocumento.
    linked = getattr(entry, "linked_invoice", None)
    if tipo_documento == "TD04" and linked is not None:
        dfc = _sub(dati_generali, "DatiFattureCollegate")
        _sub(dfc, "IdDocumento", (linked.reference or linked.doc_number)[:20])
        if linked.doc_date:
            _sub(dfc, "Data", linked.doc_date.isoformat())

    dati_beni = _sub(body, "DatiBeniServizi")
    if inv_lines:
        for l in inv_lines:
            rate = Decimal(str(l.vat_rate or 0))
            nat = (l.natura or "").strip() or None if rate == 0 else None
            linea = _sub(dati_beni, "DettaglioLinee")
            _sub(linea, "NumeroLinea", str(l.line_number))
            _sub(linea, "Descrizione", (l.description or "")[:1000])
            _sub(linea, "PrezzoUnitario", _money(l.amount))
            _sub(linea, "PrezzoTotale", _money(l.amount))
            _sub(linea, "AliquotaIVA", _money(rate))
            if nat:
                _sub(linea, "Natura", nat)
        for g in riepiloghi:
            riepilogo = _sub(dati_beni, "DatiRiepilogo")
            _sub(riepilogo, "AliquotaIVA", _money(g["rate"]))
            if g["natura"]:
                _sub(riepilogo, "Natura", g["natura"])
            _sub(riepilogo, "ImponibileImporto", _money(g["imponibile"]))
            _sub(riepilogo, "Imposta", _money(g["imposta"]))
            if not g["natura"]:
                _sub(riepilogo, "EsigibilitaIVA", "I")
    else:
        linea = _sub(dati_beni, "DettaglioLinee")
        _sub(linea, "NumeroLinea", "1")
        _sub(linea, "Descrizione", description)
        _sub(linea, "PrezzoUnitario", _money(net))
        _sub(linea, "PrezzoTotale", _money(net))
        _sub(linea, "AliquotaIVA", _money(vat_rate))
        if natura:
            _sub(linea, "Natura", natura)

        riepilogo = _sub(dati_beni, "DatiRiepilogo")
        _sub(riepilogo, "AliquotaIVA", _money(vat_rate))
        if natura:
            _sub(riepilogo, "Natura", natura)
        _sub(riepilogo, "ImponibileImporto", _money(net))
        _sub(riepilogo, "Imposta", _money(vat))
        if not natura:
            _sub(riepilogo, "EsigibilitaIVA", "I")

    if cedente.get("iban"):
        dati_pag = _sub(body, "DatiPagamento")
        _sub(dati_pag, "CondizioniPagamento", "TP02")
        dett_pag = _sub(dati_pag, "DettaglioPagamento")
        _sub(dett_pag, "ModalitaPagamento", "MP05")
        _sub(dett_pag, "ImportoPagamento", _money(gross))
        _sub(dett_pag, "IBAN", cedente["iban"])

    raw = ET.tostring(root, encoding="utf-8")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="UTF-8")

    filename = f"IT{cedente['piva']}_{progressivo_invio}.xml"
    return filename, pretty
