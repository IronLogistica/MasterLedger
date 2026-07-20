# Correzione avvio e Costo Standard

Il precedente Procfile conteneva un processo `release` per le migrazioni.
Nell'ambiente Railway in uso tale processo non veniva eseguito prima del web
process: il database era quindi rimasto senza `standard_costs`.

Ora `start.sh` esegue sempre `flask --app app db upgrade` prima di Gunicorn.
Se una migrazione non va a buon fine, l'app non parte volutamente e Railway
mostra l'errore nei log, evitando di usare uno schema parziale.

La migrazione `4e5f6a7b8c9d_repair_standard_costs.py` è prudente e crea solo
la tabella/colonne mancanti, senza cancellare o riscrivere dati.
