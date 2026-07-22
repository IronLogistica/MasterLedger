"""services/logistic_client.py — Integrazione con MasterLogistic-WMS.

MasterLogistic-WMS è la fonte di verità per le giacenze fisiche. STATO
ATTUALE (deciso da Mauri): MasterLedger per ora SOLO LEGGE da qui — non
scrive ancora la giacenza. Le funzioni di lettura (get_magazzino, get_stock,
get_bom) sono quindi quelle effettivamente usate oggi in MasterLedger
(controllo disponibilità in SD, calcolo costi materie prime in Produzione
Completata da distinta base).

La funzione di scrittura (sposta_stock, sotto) resta pronta e testata per
quando si deciderà di attivare anche la scrittura da MasterLedger verso
MasterLogistic-WMS — al momento NON è chiamata da nessuna rotta.

ASSUNZIONE CHIAVE (da verificare/allineare con Mauri se qualcosa non torna):
il campo Material.code di MasterLedger corrisponde esattamente allo SKU
(Articolo.sku) di MasterLogistic-WMS. Se i codici non combaciano tra i due
sistemi, queste funzioni non troveranno gli articoli e solleveranno
LogisticError con il codice cercato, invece di fallire in silenzio.

Le API di MasterLogistic-WMS usate qui:
    GET  /get_magazzino_wms      → {sku: {stock, ordinati, impegnato, ...}}
    GET  /api/distinta_base      → {sku_padre: {figli: [{codice_figlio, quantita, ...}]}}
    POST /rettifica_magazzino    → {sku, stock, scorta_minima} sovrascrive lo stock (non ancora usata)

NOTA IMPORTANTE SU rettifica_magazzino: è un'operazione di SCRITTURA ASSOLUTA
(sovrascrive lo stock con il valore dato), non un delta. La funzione
`sposta_stock` qui sotto fa perciò un "leggi valore attuale → calcola nuovo
valore → scrivi" (read-modify-write): se due processi scrivono sullo stesso
SKU nello stessissimo istante c'è un rischio (piccolo, ma reale) di
sovrascrittura reciproca. Non è risolvibile lato MasterLedger da solo —
richiederebbe un endpoint "incrementa di N" lato MasterLogistic-WMS.
NOTA IMPORTANTE SU AUTENTICAZIONE: queste API risultano prive di
autenticazione lato MasterLogistic-WMS al momento — chiunque conosca l'URL
può leggere/scrivere la giacenza. Da sistemare (basta una chiave condivisa)
prima di considerarlo definitivo.
"""
import requests
from flask import current_app


class LogisticError(Exception):
    """Sollevata per qualunque problema nel parlare con MasterLogistic-WMS."""
    pass


def _base_url():
    url = current_app.config.get("MASTERLOGISTIC_URL", "")
    if not url:
        raise LogisticError(
            "MASTERLOGISTIC_URL non configurato. Aggiungilo nelle variabili d'ambiente "
            "(su Railway: Variables) per collegare MasterLedger a MasterLogistic-WMS."
        )
    return url


def get_magazzino(timeout=8):
    """
    Ritorna l'intera giacenza da MasterLogistic-WMS: {sku: {stock, ordinati,
    impegnato, dispo_netta, scorta_minima, stato_scorta, ...}}.
    """
    url = f"{_base_url()}/get_magazzino_wms"
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise LogisticError(f"MasterLogistic-WMS non raggiungibile ({url}): {e}")

    dati = resp.json()
    if isinstance(dati, dict) and "__errore__" in dati:
        raise LogisticError(f"MasterLogistic-WMS ha risposto con un errore: {dati['__errore__']}")
    return dati


def get_stock(sku, timeout=8):
    """
    Ritorna il dict di giacenza per UN sku (vedi get_magazzino per i campi),
    oppure None se lo sku non esiste in MasterLogistic-WMS.
    """
    magazzino = get_magazzino(timeout=timeout)
    return magazzino.get(sku)


def get_bom(sku, timeout=8):
    """
    Ritorna la distinta base (primo livello) per lo sku dato: lista di
    {codice_figlio, quantita, desc_figlio, stock_figlio, livello, note}.
    Lista vuota se lo sku non ha una distinta base registrata.
    """
    url = f"{_base_url()}/api/distinta_base"
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise LogisticError(f"MasterLogistic-WMS non raggiungibile ({url}): {e}")

    dati = resp.json()
    voce = dati.get(sku)
    if not voce:
        return []
    return voce.get("figli", [])


def _rettifica_stock_assoluto(sku, nuovo_stock, scorta_minima=None, timeout=8):
    """Scrive lo stock ASSOLUTO per uno sku (vedi nota read-modify-write sopra)."""
    url = f"{_base_url()}/rettifica_magazzino"
    payload = {"sku": sku, "stock": int(round(nuovo_stock))}
    if scorta_minima is not None:
        payload["scorta_minima"] = int(scorta_minima)
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise LogisticError(f"MasterLogistic-WMS non raggiungibile ({url}): {e}")

    dati = resp.json()
    if not dati.get("ok"):
        raise LogisticError(f"MasterLogistic-WMS ha rifiutato la rettifica per {sku}: "
                             f"{dati.get('error', 'errore sconosciuto')}")
    return dati


def sposta_stock(sku, delta, timeout=8):
    """
    Aumenta (delta positivo) o diminuisce (delta negativo) lo stock di uno sku
    su MasterLogistic-WMS. Legge il valore attuale, applica il delta, riscrive.

    Solleva LogisticError se lo sku non esiste su MasterLogistic-WMS (non lo
    crea automaticamente: un articolo deve esistere già lì) o se il risultato
    sarebbe negativo (mai possibile fisicamente).
    """
    attuale = get_stock(sku, timeout=timeout)
    if attuale is None:
        raise LogisticError(
            f'Articolo "{sku}" non trovato su MasterLogistic-WMS. '
            f"Verifica che Material.code in MasterLedger corrisponda esattamente "
            f"allo SKU su MasterLogistic (assunzione di collegamento tra i due sistemi)."
        )

    nuovo_stock = int(attuale.get("stock", 0)) + delta
    if nuovo_stock < 0:
        raise LogisticError(
            f'Giacenza insufficiente per "{sku}" su MasterLogistic-WMS: '
            f"disponibili {attuale.get('stock', 0)}, richiesti {abs(delta)}."
        )
    return _rettifica_stock_assoluto(sku, nuovo_stock, timeout=timeout)


def get_fabbisogni_acquisto(timeout=8):
    """Ritorna solo i materiali WMS con fabbisogno di acquisto positivo."""
    magazzino = get_magazzino(timeout=timeout)
    if not isinstance(magazzino, dict):
        raise LogisticError("Risposta magazzino WMS non valida.")
    needs = []
    for sku, row in magazzino.items():
        if not isinstance(row, dict):
            continue
        try:
            qty = float(row.get("fabbisogno_netto", 0) or 0)
        except (TypeError, ValueError):
            continue
        if qty > 0:
            needs.append({"sku": sku, **row, "fabbisogno_netto": qty})
    return sorted(needs, key=lambda r: (r.get("stato_scorta") != "esaurito", r["sku"]))
