# MasterLedger

MasterLedger è un'applicazione web di contabilità operativa per aziende italiane. Gestisce prima nota, fatture attive e passive, incassi, pagamenti, cespiti, centri di costo e configurazione delle aree di magazzino.

## Moduli

| Area | Funzionalità |
|---|---|
| Contabilità generale | Prima nota, libro giornale e storni |
| Fornitori | Fatture ricevute, import XML e pagamenti |
| Clienti | Fatture emesse, note di credito, incassi e XML FatturaPA |
| Cespiti | Anagrafica, capitalizzazione e ammortamenti |
| Controllo costi | Centri di costo e reportistica |
| Magazzino | Sedi operative, aree di magazzino e conti collegati |

## Avvio locale

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
flask --app app db upgrade
flask --app app seed
flask --app app run --debug
```

Apri `http://localhost:5000`.

> Prima dell'uso reale, imposta una `SECRET_KEY` robusta, cambia tutte le credenziali demo e fai validare configurazioni fiscali e processi contabili da un professionista abilitato.

## Pubblicazione su Railway

1. Crea un repository GitHub e carica il contenuto di questa cartella.
2. In Railway scegli **New Project → Deploy from GitHub repo**.
3. Aggiungi PostgreSQL e imposta `SECRET_KEY`, `COMPANY_NAME` e `COMPANY_CODE`.
4. Dopo il primo rilascio esegui:
   ```bash
   flask --app app db upgrade
   flask --app app seed
   ```

Il file `Procfile` è già incluso.

## Variabili d'ambiente

| Variabile | Descrizione |
|---|---|
| `SECRET_KEY` | Chiave di sessione obbligatoria in produzione |
| `DATABASE_URL` | Connessione PostgreSQL; senza valore viene usato SQLite locale |
| `COMPANY_NAME` | Ragione sociale mostrata nell'app |
| `COMPANY_CODE` | Codice interno dell'azienda |

## Soggetti Economici
L'anagrafica unica **Soggetti Economici** sostituisce la creazione separata di clienti e fornitori. Ogni soggetto può avere entrambi i ruoli. La maschera raccoglie solo i dati operativi: identificazione, recapiti, dati fiscali essenziali, condizioni di pagamento e IBAN.

Dopo l'aggiornamento di un database già esistente, eseguire la migrazione prima dell'avvio dell'applicazione:

```bash
flask --app app db upgrade
```

La migrazione trasferisce gli attuali clienti e fornitori nella nuova anagrafica e collega lo storico dei documenti. Quando cliente e fornitore condividono la stessa Partita IVA, vengono uniti nello stesso soggetto con entrambi i ruoli.
