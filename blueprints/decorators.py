"""
Decoratore condiviso per limitare l'accesso in base al ruolo.

Uso:
    @commercialista_required
    def vista_riservata():
        ...

Un 'operatore' che prova ad accedere a una vista protetta riceve un 403
(vedi templates/errors/403.html) — esattamente il comportamento che vuoi
per il pannello di Configurazione Fiscale: lo strumento c'è, ma solo il
Commercialista può usarlo.
"""
from functools import wraps
from flask import abort
from flask_login import current_user


def commercialista_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_commercialista:
            abort(403)
        return view_func(*args, **kwargs)
    return wrapped
