#!/bin/sh
# Aggiorna lo schema prima di avviare l'app. Se l'aggiornamento non riesce,
# l'avvio si interrompe: è più sicuro che lavorare con uno schema incompleto.
set -e
flask --app app db upgrade
# Popola/completa il Piano dei Conti, gli utenti demo e la configurazione
# Paghe. È idempotente (salta tutto ciò che esiste già, vedi seed.py), quindi
# è sicuro rilanciarlo ad ogni deploy — senza questo passo, gli account che
# vivono SOLO in seed.py (banca, cassa, crediti, debiti, IVA, personale,
# utenze...) non venivano mai creati in produzione, restando dipendenti da
# un comando manuale che nessuno lanciava più dopo il primo giro.
flask --app app seed
exec gunicorn --bind "0.0.0.0:${PORT:-8080}" app:app
