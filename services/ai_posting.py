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


def suggerisci_scrittura(descrizione, accounts):
    """
    Chiede all'AI di proporre le righe di una scrittura contabile a partire
    da una descrizione in linguaggio naturale.

    accounts: lista di oggetti Account (code, name, account_type) — il piano
              dei conti REALE dell'azienda; l'AI può usare solo questi codici,
              non può inventarne altri.

    Ritorna un dict:
        {"description": str, "lines": [{"account_code": str, "pk": "40"|"50", "amount": float}, ...], "note": str|None}

    Solleva AISuggestionError se manca la chiave API, se la chiamata fallisce,
    o se la risposta dell'AI non è un JSON valido con almeno due righe.
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

    if not descrizione or not descrizione.strip():
        raise AISuggestionError("Descrivi prima l'operazione da registrare.")

    piano_conti = "\n".join(f"{a.code} | {a.name} | {a.account_type}" for a in accounts)

    system_prompt = (
        "Sei un contabile esperto in partita doppia secondo i principi contabili italiani (OIC).\n"
        "Il piano dei conti disponibile è ESATTAMENTE questo (codice | nome | tipo conto). "
        "Non puoi inventare altri conti né altri codici:\n"
        f"{piano_conti}\n\n"
        "Dato un testo in linguaggio naturale che descrive un'operazione contabile, rispondi "
        "SOLO con un oggetto JSON (nessun testo prima o dopo), con questa struttura:\n"
        "{\n"
        '  "description": "testo breve per la Prima Nota",\n'
        '  "lines": [\n'
        '    {"account_code": "codice ESATTO dal piano conti sopra", "pk": "40 oppure 50", "amount": numero},\n'
        "    ...\n"
        "  ],\n"
        '  "note": "eventuali avvertenze o incertezze, altrimenti null"\n'
        "}\n\n"
        "Regole obbligatorie:\n"
        "- 40 = Dare, 50 = Avere.\n"
        "- La somma degli importi in Dare deve essere ESATTAMENTE uguale alla somma in Avere.\n"
        "- Usa solo i codici conto elencati sopra: se nessuno è adatto, scegli il più plausibile "
        "e scrivi il dubbio in \"note\".\n"
        "- Servono almeno due righe (una in Dare, una in Avere)."
    )

    client = OpenAI(api_key=api_key)

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": descrizione.strip()},
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
