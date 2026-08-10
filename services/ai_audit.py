"""services/ai_audit.py — Ispezione AI del Libro Giornale.

COSA FA: manda al modello AI un riepilogo compatto delle scritture di un
periodo e chiede di segnalare ANOMALIE DI PATTERN — non correttezza
fiscale/tributaria (quella resta lavoro del commercialista). Esempi di
cosa può notare: scritture che a conti fatti non tornano, conti insoliti
per il tipo di documento, centro di costo mancante su una riga di costo,
importi identici e ravvicinati che sembrano doppioni, descrizioni troppo
generiche per essere verificabili in un controllo.

COSA NON FA: non modifica nulla, non decide nulla — è un elenco di punti
da verificare a occhio umano, mai un verdetto. Ogni riga del risultato va
lettA e giudicata da una persona.

PRIVACY: ogni chiamata manda dati contabili reali (importi, controparti,
date, descrizioni) a OpenAI — va usata consapevolmente, mai in automatico.
"""
import json
import os

MAX_ENTRIES = 400  # oltre questa soglia, meglio restringere il periodo che troncare in silenzio


class AuditError(Exception):
    pass


def _formatta_scrittura(entry):
    righe = "; ".join(
        f"{l.account.code} {l.account.name} "
        f"{'D' if l.dare else 'A'}{float(l.dare or l.avere):.2f}"
        f"{' [CC:' + l.cost_center.code + ']' if l.cost_center else ''}"
        for l in entry.lines
    )
    controparte = entry.economic_subject.name if entry.economic_subject else "—"
    return (f"{entry.doc_number} | {entry.doc_date} | {entry.doc_type} | "
            f"{entry.source_module} | {controparte} | \"{entry.description or ''}\" | "
            f"stornata:{entry.is_reversed} | righe: {righe}")


def ispeziona_giornale(entries):
    """entries: lista di JournalEntry (già filtrata per periodo dal chiamante).
    Ritorna una lista di dict {doc_number, gravita, motivo}."""
    if not entries:
        raise AuditError("Nessuna scrittura nel periodo selezionato.")
    if len(entries) > MAX_ENTRIES:
        raise AuditError(
            f"{len(entries)} scritture nel periodo — oltre il limite di {MAX_ENTRIES} per una singola "
            f"ispezione. Restringi il periodo (es. un trimestre alla volta) invece di mandarle tutte: "
            f"un controllo parziale silenzioso darebbe un falso senso di sicurezza."
        )

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY")
    if not api_key:
        raise AuditError(
            "Nessuna OPENAI_API_KEY configurata. Aggiungila nelle variabili d'ambiente "
            "(su Railway: Variables) per attivare l'ispezione AI."
        )
    try:
        from openai import OpenAI
    except ImportError:
        raise AuditError("Il pacchetto 'openai' non è installato.")

    elenco = "\n".join(_formatta_scrittura(e) for e in entries)

    system_prompt = (
        "Sei un revisore contabile che fa un CONTROLLO DI PATTERN su un elenco di scritture in "
        "partita doppia (non un controllo fiscale/tributario, solo strutturale). Ogni riga del "
        "formato è: numero documento | data | tipo documento | modulo di origine | controparte | "
        "descrizione | se è stata stornata | elenco righe (conto, D=Dare/A=Avere, importo, "
        "[CC:centro di costo] se presente).\n\n"
        "Segnala SOLO cose concretamente sospette, non stilistiche. Esempi di cosa cercare:\n"
        "- conti insoliti per il tipo di documento o il modulo di origine;\n"
        "- righe di costo (account con nome che suggerisce un costo) senza centro di costo [CC:...];\n"
        "- descrizioni troppo generiche per essere verificabili (es. solo \"Prima Nota\", \"Varie\");\n"
        "- possibili doppioni: stesso importo, stessa controparte, date molto vicine, stesso tipo documento;\n"
        "- importi rotondi e ripetuti in modo sospetto;\n"
        "- pattern strani nella sequenza (es. uno storno senza il documento originale nell'elenco).\n\n"
        "NON commentare la correttezza fiscale/IVA, non dare consigli tributari, non inventare "
        "problemi se non ne vedi di concreti — un elenco vuoto è un risultato valido e buono.\n\n"
        "Rispondi SOLO con un oggetto JSON: "
        '{"anomalie": [{"doc_number": "...", "gravita": "bassa|media|alta", "motivo": "..."}], '
        '"riepilogo": "una frase sull\'esito generale"}'
    )

    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Scritture da ispezionare ({len(entries)}):\n{elenco}"},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
    except Exception as e:
        raise AuditError(f"Errore nella chiamata a OpenAI: {e}")

    try:
        data = json.loads(response.choices[0].message.content)
    except (ValueError, TypeError):
        raise AuditError("Risposta AI non interpretabile come JSON.")

    return data.get("anomalie", []), data.get("riepilogo", "")
