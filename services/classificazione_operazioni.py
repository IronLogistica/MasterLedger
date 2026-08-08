"""services/classificazione_operazioni.py — Classi di operazione ricorrenti
di Iron Appalti per la Prima Nota libera (tutto ciò che NON passa da un
modulo dedicato: vendite/SD, acquisti/AP-MM, buste paga/F24 sono già
automatici altrove e non transitano da qui).

COME FUNZIONA (in breve):
  1. classifica(testo_documento) prova a riconoscere la CLASSE del
     documento tra quelle note, con un punteggio di confidenza.
  2. Ogni classe ha uno SCHEMA fisso di conti (codici REALI del piano dei
     conti Iron Appalti) — l'AI/il parser deve solo leggere GLI IMPORTI dal
     documento e metterli nelle caselle giuste, non deve inventare conti.
  3. costruisci_proposta(...) genera la scrittura pronta per la revisione
     umana in Prima Nota — NON la registra da sola.
  4. Se la classificazione è incerta, o gli importi non pareggiano, o il
     documento non rientra in nessuna classe nota, la funzione ritorna
     esito="CONTATTA_COMMERCIALISTA" invece di forzare una proposta.

PRINCIPIO NON NEGOZIABILE (eredita da ai_posting.py): nessuna scrittura
diventa definitiva senza che un umano la veda e la confermi con un click
in Prima Nota. Le scritture qui sono immutabili — un errore passato in
automatico senza controllo si scopre solo dopo, e allora si storna, non si
cancella. Questo modulo automatizza la PROPOSTA, non la REGISTRAZIONE.
"""
from decimal import Decimal, InvalidOperation


class ClassificazioneIncerta(Exception):
    """Il documento non è stato classificato con sufficiente confidenza."""
    pass


# ══════════════════════════════════════════════════════════════
# CONTI BANCA/CASSA — il documento deve indicare quale, il default è
# solo un suggerimento se il testo non lo specifica.
# ══════════════════════════════════════════════════════════════
CONTI_BANCA_CASSA = {
    "intesa": "032003",       # Intesa San Paolo c/c
    "paypal": "032004",       # PayPal
    "contanti": "032601",     # cassa contanti
    "cassa": "032601",
}
CONTO_BANCA_DEFAULT = "032003"  # Intesa San Paolo c/c — conto banca principale osservato


# ══════════════════════════════════════════════════════════════
# CLASSI DI OPERAZIONE — schema fisso di conti reali per classe.
# "dare_fissi"/"avere_fissi": conti sempre coinvolti in quella posizione.
# "avere_variabili"/"dare_variabili": conti ammessi, ma la ripartizione
#   dell'importo tra loro va letta dal documento (es. rata AdER: quota
#   capitale INPS + interessi + sanzioni + eventuale IVA + commissione,
#   voce per voce diversa ogni volta).
# ══════════════════════════════════════════════════════════════
CLASSI_OPERAZIONE = {
    "ADER": {
        "nome": "Pagamento rateizzazione Enti/AdER",
        "parole_chiave": ["ader", "agenzia delle entrate riscossione", "rottamazione",
                          "rateizzazione", "piano di rientro cartelle", "prot."],
        "dare_variabili": ["032003", "032004"],  # banca o paypal, uno dei due secondo il documento
        "avere_variabili": ["044692", "070500", "054607", "045001", "070018"],
        # INPS c/contributi | interessi passivi su pagamenti | multe e sanzioni (indeducibile)
        # | iva c/erario | commissioni/oneri bancari — non tutte compaiono in ogni rata
        "note": "La ripartizione tra le voci in Avere cambia rata per rata: leggerla dal "
                "documento (di norma il piano di rateizzazione la specifica voce per voce).",
    },
    "TFR_ESTERNO": {
        "nome": "Pagamento a Fondo TFR esterno (Alleanza Previdenza)",
        "parole_chiave": ["alleanza previdenza", "fondo tfr", "trf alleanza"],
        "dare_fissi": ["044960"],   # Debiti v/fondo TFR Alleanza Previdenza
        "avere_variabili": ["032003", "032004"],
        "note": "Verificare se il documento indica anche una commissione bancaria separata "
                "(070018) da aggiungere in Dare.",
    },
    "CASSA_EDILE": {
        "nome": "Pagamento Cassa Edile (piano di rientro a rate)",
        "parole_chiave": ["cassa edile", "piano di rientro"],
        "dare_fissi": ["044915"],  # debiti v/Cassa Edile
        "avere_variabili": ["032003", "032004"],
        "note": "Se il documento mostra una commissione bancaria separata, aggiungerla "
                "in Dare (070018).",
    },
    "EFFETTI": {
        "nome": "Pagamento effetti/cambiali passive",
        "parole_chiave": ["cambiale", "effetto", "effetti passivi", "tratta"],
        "dare_fissi": ["044202"],  # effetti passivi
        "avere_variabili": ["032003", "032004", "032601"],
        "note": None,
    },
    "COMMISSIONI_BANCARIE": {
        "nome": "Addebito commissioni bancarie / imposta di bollo",
        "parole_chiave": ["commissioni", "spese bancarie", "imposta di bollo", "bollo conto"],
        "dare_variabili": ["070018", "057019"],  # commissioni/oneri bancari | imposte di bollo
        "avere_variabili": ["032003", "032004"],
        "note": None,
    },
    "RETTIFICA_GENERICA": {
        "nome": "Rettifica / movimento generico",
        "parole_chiave": ["rettifica", "storno", "movimento generico"],
        "dare_fissi": [],
        "avere_fissi": [],
        "note": "Nessuno schema fisso possibile: i conti coinvolti dipendono interamente dal "
                "caso. Non proporre uno schema — passa sempre da revisione con l'AI generica "
                "(suggerisci_scrittura) o da CONTATTA_COMMERCIALISTA se anche quella è incerta.",
        "sempre_incerta": True,  # questa classe non genera mai una proposta automatica
    },
}


def guida_per_classe(chiave, code_to_name=None):
    """
    Genera il testo da passare come guida_extra a suggerisci_scrittura()
    quando classifica() ha riconosciuto una classe nota con confidenza
    sufficiente. code_to_name: dict opzionale {codice: nome conto} per
    rendere leggibile la guida invece di elencare solo codici nudi.
    """
    classe = CLASSI_OPERAZIONE.get(chiave)
    if classe is None or classe.get("sempre_incerta"):
        return None

    def _fmt(codici):
        if not codici:
            return "(nessuno)"
        if code_to_name:
            return ", ".join(f"{c} ({code_to_name.get(c, '?')})" for c in codici)
        return ", ".join(codici)

    righe = [f"Classe riconosciuta: {classe['nome']}."]
    if classe.get("dare_fissi"):
        righe.append(f"DARE (sempre presente): {_fmt(classe['dare_fissi'])}")
    if classe.get("dare_variabili"):
        righe.append(f"DARE (uno o più tra questi, secondo il documento): {_fmt(classe['dare_variabili'])}")
    if classe.get("avere_fissi"):
        righe.append(f"AVERE (sempre presente): {_fmt(classe['avere_fissi'])}")
    if classe.get("avere_variabili"):
        righe.append(f"AVERE (uno o più tra questi, secondo il documento): {_fmt(classe['avere_variabili'])}")
    if classe.get("note"):
        righe.append(f"Nota: {classe['note']}")
    return "\n".join(righe)


def classifica(testo_documento):
    """
    Cerca la classe più plausibile per il testo di un documento, con un
    punteggio di confidenza molto semplice (conteggio parole chiave
    trovate). Ritorna (chiave_classe, confidenza 0-1) oppure (None, 0.0)
    se nessuna classe ha un punteggio sufficiente.
    """
    testo = (testo_documento or "").lower()
    if not testo.strip():
        return None, 0.0

    migliore, punteggio_migliore = None, 0
    for chiave, classe in CLASSI_OPERAZIONE.items():
        punteggio = sum(1 for kw in classe["parole_chiave"] if kw in testo)
        if punteggio > punteggio_migliore:
            migliore, punteggio_migliore = chiave, punteggio

    if migliore is None or punteggio_migliore == 0:
        return None, 0.0

    # confidenza grezza: 1 parola chiave trovata = 0.6, 2+ = 0.85 (mai 1.0 —
    # è pur sempre un match testuale, non una certezza)
    confidenza = 0.6 if punteggio_migliore == 1 else 0.85
    return migliore, confidenza


def costruisci_proposta(testo_documento, importi_letti, confidenza_minima=0.6):
    """
    importi_letti: dict {codice_conto: importo} già estratto dal documento
    (da un parser o dall'AI generica con testo_documento come contesto) —
    questa funzione NON legge il PDF, si limita a verificare che gli
    importi passati rispettino lo schema della classe e pareggino.

    Ritorna un dict:
      {"esito": "PROPOSTA", "classe": ..., "lines": [...], "note": ...}
      oppure
      {"esito": "CONTATTA_COMMERCIALISTA", "motivo": "..."}
    """
    chiave, confidenza = classifica(testo_documento)

    if chiave is None or confidenza < confidenza_minima:
        return {"esito": "CONTATTA_COMMERCIALISTA",
                "motivo": "Documento non riconducibile con sufficiente certezza a una classe "
                          "nota di Prima Nota libera. Verificare manualmente con il "
                          "commercialista prima di registrare."}

    classe = CLASSI_OPERAZIONE[chiave]
    if classe.get("sempre_incerta"):
        return {"esito": "CONTATTA_COMMERCIALISTA",
                "motivo": f"Classe '{classe['nome']}' non ha uno schema fisso: serve "
                          f"valutazione caso per caso."}

    conti_ammessi = set(classe.get("dare_fissi", []) + classe.get("dare_variabili", []) +
                         classe.get("avere_fissi", []) + classe.get("avere_variabili", []))
    for codice in importi_letti:
        if codice not in conti_ammessi:
            return {"esito": "CONTATTA_COMMERCIALISTA",
                    "motivo": f"Il conto {codice} non è previsto per la classe "
                              f"'{classe['nome']}': possibile errore di lettura del documento."}

    try:
        totale_dare = sum(Decimal(str(importi_letti[c])) for c in
                           (classe.get("dare_fissi", []) + classe.get("dare_variabili", []))
                           if c in importi_letti)
        totale_avere = sum(Decimal(str(importi_letti[c])) for c in
                            (classe.get("avere_fissi", []) + classe.get("avere_variabili", []))
                            if c in importi_letti)
    except (InvalidOperation, TypeError):
        return {"esito": "CONTATTA_COMMERCIALISTA",
                "motivo": "Importi letti dal documento non validi."}

    if abs(totale_dare - totale_avere) > Decimal("0.01"):
        return {"esito": "CONTATTA_COMMERCIALISTA",
                "motivo": f"La scrittura proposta non pareggia (Dare {totale_dare} / "
                          f"Avere {totale_avere}): serve revisione manuale."}

    lines = []
    for codice in (classe.get("dare_fissi", []) + classe.get("dare_variabili", [])):
        if codice in importi_letti:
            lines.append({"account_code": codice, "pk": "40", "amount": float(importi_letti[codice])})
    for codice in (classe.get("avere_fissi", []) + classe.get("avere_variabili", [])):
        if codice in importi_letti:
            lines.append({"account_code": codice, "pk": "50", "amount": float(importi_letti[codice])})

    return {
        "esito": "PROPOSTA",
        "classe": classe["nome"],
        "confidenza": confidenza,
        "lines": lines,
        "note": classe.get("note"),
        "richiede_conferma_operatore": True,  # SEMPRE — nessuna registrazione senza click umano
    }
