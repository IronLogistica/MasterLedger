"""
services/fatturapa_import.py — Lettura delle fatture elettroniche RICEVUTE.

Legge un file XML FatturaPA (fattura fornitore scaricata dal pannello
dell'intermediario, es. Aruba, o dal cassetto fiscale) e ne estrae i dati
necessari a pre-compilare la registrazione Fattura fornitore: fornitore, numero, data,
tipo documento, imponibili e imposte per aliquota.

Gestisce anche i file .xml.p7m (firma CAdES-BES): il contenuto XML è
incapsulato nella busta crittografica DER, ma NON è cifrato — è leggibile
in chiaro all'interno del file. L'estrazione qui sotto NON verifica la
firma (per quello serve l'intermediario o un tool CAdES): si limita a
recuperare il payload XML, che per la registrazione contabile è ciò che
serve.

Nota su multi-body: un file FatturaPA può contenere PIÙ FatturaElettronicaBody
(lotto di fatture dello stesso cedente). Questo parser gestisce il caso
comune di un body singolo e segnala esplicitamente il lotto multiplo.
"""
import re
from decimal import Decimal
from xml.etree import ElementTree as ET


class FatturaImportError(Exception):
    """Sollevata quando il file non è leggibile o non è una FatturaPA valida."""
    pass


def _strip_ns(tag):
    """'{http://...}Nome' -> 'Nome' — il parsing è namespace-agnostico."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _find(el, path):
    """find() ignorando i namespace, su percorsi tipo 'A/B/C'."""
    current = el
    for part in path.split("/"):
        nxt = None
        for child in current:
            if _strip_ns(child.tag) == part:
                nxt = child
                break
        if nxt is None:
            return None
        current = nxt
    return current


def _findall(el, name):
    """Tutti i discendenti diretti+profondi con quel nome (senza namespace)."""
    return [e for e in el.iter() if _strip_ns(e.tag) == name]


def _text(el, path, default=""):
    node = _find(el, path)
    return (node.text or "").strip() if node is not None and node.text else default


def _extract_xml_from_p7m(raw):
    """
    Estrae il payload XML da una busta CAdES (.xml.p7m). La firma NON viene
    verificata: si cerca il blocco che va da '<?xml' fino alla chiusura di
    FatturaElettronica. Funziona per le buste CAdES con contenuto "attached"
    non compresso — cioè la totalità delle fatture SdI in circolazione.
    """
    start = raw.find(b"<?xml")
    if start == -1:
        raise FatturaImportError(
            "Impossibile trovare il contenuto XML dentro il file .p7m. "
            "Scarica dal pannello dell'intermediario la versione .xml già sbustata."
        )
    # chiusura: ultimo '>' dopo l'ultima occorrenza di 'FatturaElettronica'
    end_tag = raw.rfind(b"FatturaElettronica")
    if end_tag == -1:
        raise FatturaImportError("Il file .p7m non sembra contenere una FatturaPA.")
    end = raw.find(b">", end_tag)
    if end == -1:
        raise FatturaImportError("XML troncato all'interno del file .p7m.")
    return raw[start:end + 1]


def parse_fatturapa(raw_bytes, filename=""):
    """
    Ritorna un dict con i dati della fattura ricevuta:
      {
        "cedente_denominazione", "cedente_piva", "cedente_cf",
        "tipo_documento", "numero", "data" (str ISO),
        "totale_documento" (Decimal|None),
        "riepiloghi": [{"aliquota": Decimal, "natura": str|None,
                        "imponibile": Decimal, "imposta": Decimal}],
        "totale_imponibile" (Decimal), "totale_imposta" (Decimal),
        "descrizione_righe": str (riassunto delle prime righe),
        "multi_body": bool
      }
    """
    raw = raw_bytes
    if filename.lower().endswith(".p7m") or not raw.lstrip().startswith(b"<"):
        raw = _extract_xml_from_p7m(raw)

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise FatturaImportError(f"XML non leggibile: {e}")

    if _strip_ns(root.tag) != "FatturaElettronica":
        raise FatturaImportError(
            f"Il file non è una FatturaPA (elemento radice: {_strip_ns(root.tag)})."
        )

    header = _find(root, "FatturaElettronicaHeader")
    bodies = [e for e in root if _strip_ns(e.tag) == "FatturaElettronicaBody"]
    if header is None or not bodies:
        raise FatturaImportError("Struttura FatturaPA incompleta (manca Header o Body).")
    body = bodies[0]

    cedente = _find(header, "CedentePrestatore/DatiAnagrafici")
    denominazione = _text(cedente, "Anagrafica/Denominazione") if cedente is not None else ""
    if not denominazione and cedente is not None:
        nome = _text(cedente, "Anagrafica/Nome")
        cognome = _text(cedente, "Anagrafica/Cognome")
        denominazione = f"{nome} {cognome}".strip()
    piva = _text(cedente, "IdFiscaleIVA/IdCodice") if cedente is not None else ""
    cf = _text(cedente, "CodiceFiscale") if cedente is not None else ""

    dgd = _find(body, "DatiGenerali/DatiGeneraliDocumento")
    if dgd is None:
        raise FatturaImportError("Blocco DatiGeneraliDocumento assente.")
    tipo_doc = _text(dgd, "TipoDocumento")
    numero = _text(dgd, "Numero")
    data = _text(dgd, "Data")
    tot_doc_raw = _text(dgd, "ImportoTotaleDocumento")
    totale_documento = Decimal(tot_doc_raw) if tot_doc_raw else None

    riepiloghi = []
    tot_imponibile = Decimal("0")
    tot_imposta = Decimal("0")
    for r in _findall(body, "DatiRiepilogo"):
        imponibile = Decimal(_text(r, "ImponibileImporto") or "0")
        imposta = Decimal(_text(r, "Imposta") or "0")
        riepiloghi.append({
            "aliquota": Decimal(_text(r, "AliquotaIVA") or "0"),
            "natura": _text(r, "Natura") or None,
            "imponibile": imponibile,
            "imposta": imposta,
        })
        tot_imponibile += imponibile
        tot_imposta += imposta
    if not riepiloghi:
        raise FatturaImportError("Nessun blocco DatiRiepilogo trovato: file anomalo.")

    descrizioni = [
        (_text(l, "Descrizione") or "").strip()
        for l in _findall(body, "DettaglioLinee")
    ]
    descrizioni = [d for d in descrizioni if d]
    descr = "; ".join(descrizioni[:3])
    if len(descrizioni) > 3:
        descr += f" (+{len(descrizioni) - 3} righe)"

    return {
        "cedente_denominazione": denominazione,
        "cedente_piva": piva,
        "cedente_cf": cf,
        "tipo_documento": tipo_doc,
        "numero": numero,
        "data": data,
        "totale_documento": totale_documento,
        "riepiloghi": riepiloghi,
        "totale_imponibile": tot_imponibile,
        "totale_imposta": tot_imposta,
        "descrizione_righe": descr,
        "multi_body": len(bodies) > 1,
    }
