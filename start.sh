#!/bin/sh
# Aggiorna lo schema prima di avviare l'app. Se l'aggiornamento non riesce,
# l'avvio si interrompe: è più sicuro che lavorare con uno schema incompleto.
set -e
flask --app app db upgrade
exec gunicorn --bind "0.0.0.0:${PORT:-8080}" app:app
