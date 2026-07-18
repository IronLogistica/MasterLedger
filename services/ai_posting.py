"""services/ai_posting.py — Suggerimento scritture contabili tramite AI (OpenAI).

IMPORTANTE: l'AI non registra MAI nulla da sola. Propone solo le righe
(conto, Dare/Avere, importo), che arrivano pre-compilate nel form di Prima
Nota — l'utente le vede, le corregge se serve, e deve comunque premere
"Registra Documento" per confermarle. Passano sempre da post_journal_entry(),
quindi restano soggette alla stessa validazione Dare=Avere di ogni altra
scrittura: l'AI non bypassa in nessun modo i controlli esistenti.
"""
import json
import os


class AISuggestionError(Exception):
    """Sollevata per qualunque problema nel chiedere/interpretare il suggerimento AI."""
    pass


# ── Conoscenza specialistica per tipi di documento con schema contabile
# standard in Italia. L'AI riceve queste indicazioni SOLO come guida sullo
# schema di scrittura tipico — i conti restano comunque vincolati al piano
# dei conti reale passato a parte: se un conto "giusto" non esiste, l'AI
# sceglie il più vicino e lo segnala in "note", non ne inventa uno nuovo.
_GUIDA_BUSTA_PAGA = """
Questo documento è (o potrebbe essere) una BUSTA PAGA italiana. Lo schema
CONTABILE STANDARD per una busta paga (competenza, non pagamento) è:

DARE  Costo del personale — Salari e stipendi     = retribuzione LORDA in busta
DARE  Oneri sociali INPS/INAIL a carico azienda    = contributi datore di lavoro (NON le trattenute del dipendente)
DARE  Accantonamento TFR (costo)                   = quota TFR maturata nel periodo
AVERE Debiti v/dipendenti c/retribuzioni           = netto a pagare al dipendente
AVERE Debiti v/Istituti previdenziali (INPS/INAIL) = trattenute previdenziali del dipendente + contributi a carico azienda
AVERE Erario c/ritenute da versare (IRPEF)          = ritenute fiscali (IRPEF + eventuali addizionali regionali/comunali)
AVERE Fondo TFR                                     = quota TFR accantonata (se non gestita a parte)

Nota bene: le trattenute previdenziali/fiscali A CARICO DEL DIPENDENTE non
sono un costo aggiuntivo per l'azienda — riducono solo il netto pagato,
spostando l'importo dal debito verso il dipendente al debito verso
INPS/Erario. Il costo aziendale vero è: retribuzione lorda + contributi
INPS/INAIL a carico azienda + quota TFR.
Se dal testo non risulta chiaramente un valore (es. TFR non indicato), non
inventarlo: ometti quella riga e segnalalo in "note".
"""

_GUIDA_F24 = """
Questo documento è (o potrebbe essere) un modello F24 italiano (pagamento
unificato di imposte/contributi). Un F24 di norma NON genera un nuovo
costo: è il PAGAMENTO di debiti/ritenute già registrati in precedenza
(es. dalle buste paga, dalle fatture, dall'IVA periodica). Lo schema
CONTABILE STANDARD è:

DARE  Erario c/ritenute da versare        = importi a debito con codice tributo erariale (es. ritenute IRPEF, 1001/1040...)
DARE  Debiti v/Istituti previdenziali     = importi a debito con codice tributo INPS/INAIL (sezione INPS)
DARE  altri debiti tributari pertinenti   = es. IVA a debito, IRES, IRAP, se presenti come voci a debito nell'F24
AVERE Banca c/c                           = SALDO EFFETTIVAMENTE PAGATO (somma "importi a debito" meno somma "importi a credito compensato")

Attenzione alla COMPENSAZIONE: se l'F24 mostra sia importi "a debito" sia
importi "a credito compensato" (es. un credito IVA usato per compensare un
debito INPS), il conto Banca si muove solo per il saldo netto — gli importi
compensati tra loro NON escono dalla banca, ma vanno comunque estinti nei
rispettivi conti debito/credito (quindi possono servire righe aggiuntive che
si bilanciano tra loro senza toccare la banca).
Se non riesci a distinguere con certezza le singole voci per codice
tributo, raggruppa in modo ragionevole e segnala l'incertezza in "note".
"""

_GUIDE_PER_TIPO = {
    "busta_paga": _GUIDA_BUSTA_PAGA,
    "f24": _GUIDA_F24,
}


def estrai_testo_pdf(file_stream, max_pagine=15, max_caratteri=12000):
    """
    Estrae il testo da un PDF "digitale" (con testo selezionabile — la
    stragrande maggioranza di fatture, bollette e documenti generati da
    software gestionali). NON fa OCR: un PDF scansionato come pura immagine
    restituirà testo vuoto o quasi — in quel caso avvisiamo l'utente invece
    di fingere che sia andato tutto bene.

    file_stream: oggetto file-like (es. request.files['documento'].stream)
    Ritorna: (testo_estratto: str, pagine_lette: int)
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        raise AISuggestionError(
            "Il pacchetto 'pypdf' non è installato. Aggiungilo a requirements.txt (pypdf==5.1.0)."
        )

    try:
        reader = PdfReader(file_stream)
    except Exception as e:
        raise AISuggestionError(f"Impossibile leggere il PDF: {e}")

    testo_totale = []
    pagine_lette = 0
    for pagina in reader.pages[:max_pagine]:
        try:
            testo_pagina = pagina.extract_text() or ""
        except Exception:
            testo_pagina = ""
        if testo_pagina.strip():
            testo_totale.append(testo_pagina)
        pagine_lette += 1

    testo = "\n".join(testo_totale).strip()
    # Limite di sicurezza sui caratteri per non gonfiare troppo la richiesta all'AI
    if len(testo) > max_caratteri:
        testo = testo[:max_caratteri] + "\n[...testo troncato...]"

    return testo, pagine_lette


def suggerisci_scrittura(descrizione, accounts, testo_documento=None, tipo_documento=None):
    """
    Chiede all'AI di proporre le righe di una scrittura contabile a partire
    da una descrizione in linguaggio naturale e/o dal testo di un documento
    (es. una fattura PDF già estratta con estrai_testo_pdf).

    accounts: lista di oggetti Account (code, name, account_type) — il piano
              dei conti REALE dell'azienda; l'AI può usare solo questi codici,
              non può inventarne altri.
    testo_documento: testo estratto da un PDF caricato (opzionale). Se presente,
              l'AI lo usa come fonte primaria (importi, aliquote IVA, controparte,
              date) e la "descrizione" diventa un'indicazione aggiuntiva/di contesto.
    tipo_documento: None/"generico" | "busta_paga" | "f24" — se indicato, aggiunge
              alla richiesta lo schema contabile standard italiano per quel tipo
              di documento (vedi _GUIDE_PER_TIPO), così l'AI non deve indovinare
              da zero lo schema di scritture più complesse.

    Ritorna un dict:
        {"description": str, "lines": [{"account_code": str, "pk": "40"|"50", "amount": float}, ...], "note": str|None}

    Solleva AISuggestionError se manca la chiave API, se la chiamata fallisce,
    se non c'è nessun contenuto da analizzare, o se la risposta dell'AI non è
    un JSON valido con almeno due righe.
    """
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY")
    if not api_key:
        raise AISuggestionError(
            "Nessuna OPENAI_API_KEY configurata. Aggiungila nelle variabili d'ambiente "
            "(su Railway: Variables) per attivare il suggerimento AI."
        )

    try:
        from openai import OpenAI
    except ImportError:
        raise AISuggestionError(
            "Il pacchetto 'openai' non è installato. Aggiungilo a requirements.txt "
            "(openai==1.54.4) e rifai il deploy."
        )

    descrizione = (descrizione or "").strip()
    testo_documento = (testo_documento or "").strip()
    if not descrizione and not testo_documento:
        raise AISuggestionError("Descrivi l'operazione oppure carica un documento da analizzare.")

    piano_conti = "\n".join(f"{a.code} | {a.name} | {a.account_type}" for a in accounts)

    system_prompt = (
        "Sei un contabile esperto in partita doppia secondo i principi contabili italiani (OIC).\n"
        "Il piano dei conti disponibile è ESATTAMENTE questo (codice | nome | tipo conto). "
        "Non puoi inventare altri conti né altri codici:\n"
        f"{piano_conti}\n\n"
        "Riceverai una descrizione in linguaggio naturale e/o il testo estratto da un documento "
        "(es. una fattura). Se c'è il testo del documento, usalo come fonte principale per importi, "
        "aliquota IVA, data e controparte; la descrizione (se presente) è solo un'indicazione di contesto.\n\n"
        "Rispondi SOLO con un oggetto JSON (nessun testo prima o dopo), con questa struttura:\n"
        "{\n"
        '  "description": "testo breve per la Prima Nota (es. numero fattura e fornitore/cliente)",\n'
        '  "lines": [\n'
        '    {"account_code": "codice ESATTO dal piano conti sopra", "pk": "40 oppure 50", "amount": numero},\n'
        "    ...\n"
        "  ],\n"
        '  "note": "eventuali avvertenze o incertezze (es. IVA non chiara, importo dubbio), altrimenti null"\n'
        "}\n\n"
        "Regole obbligatorie:\n"
        "- 40 = Dare, 50 = Avere.\n"
        "- La somma degli importi in Dare deve essere ESATTAMENTE uguale alla somma in Avere.\n"
        "- Se è una fattura, ricordati di scorporare l'IVA se un conto IVA è disponibile nel piano dei conti.\n"
        "- Usa solo i codici conto elencati sopra: se nessuno è adatto, scegli il più plausibile "
        "e scrivi il dubbio in \"note\".\n"
        "- Servono almeno due righe (una in Dare, una in Avere)."
    )

    parti_messaggio_utente = []
    if descrizione:
        parti_messaggio_utente.append(f"Descrizione fornita dall'utente:\n{descrizione}")
    if testo_documento:
        parti_messaggio_utente.append(f"Testo estratto dal documento caricato:\n{testo_documento}")
    messaggio_utente = "\n\n".join(parti_messaggio_utente)

    guida_specialistica = _GUIDE_PER_TIPO.get(tipo_documento, "")
    if guida_specialistica:
        system_prompt += "\n\n" + guida_specialistica

    client = OpenAI(api_key=api_key)

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": messaggio_utente},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
    except Exception as e:
        raise AISuggestionError(f"Errore nella chiamata a OpenAI: {e}")

    raw = response.choices[0].message.content
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise AISuggestionError("La risposta dell'AI non è un JSON valido, riprova riformulando la richiesta.")

    lines = data.get("lines") or []
    if len(lines) < 2:
        raise AISuggestionError("L'AI non è riuscita a proporre almeno due righe (una in Dare e una in Avere).")

    return data
