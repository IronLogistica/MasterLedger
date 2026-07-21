"""Regole comuni del Controlling (CO).

Il CO non crea un secondo libro contabile: usa le JournalLine FI e richiede
un oggetto CO (per ora il Centro di costo) quando una riga usa un elemento di
costo/ricavo, cioè un conto marcato ``cost_relevant``.
"""
from models import Account, CostCenter


class COValidationError(ValueError):
    """Dati analitici non validi per una registrazione CO."""


def get_active_cost_center(cost_center_id):
    """Restituisce un CdC attivo oppure solleva un errore leggibile."""
    if not cost_center_id:
        return None
    center = CostCenter.query.get(cost_center_id)
    if center is None:
        raise COValidationError("Il centro di costo selezionato non esiste.")
    if not center.active:
        raise COValidationError(f"Il centro di costo {center.code} non è attivo.")
    return center


def validate_co_assignment(account_id, cost_center_id, require_for_relevant=True):
    """Valida il legame elemento di costo/ricavo ↔ centro di costo.

    I conti patrimoniali (IVA, fornitori, banche, magazzino ecc.) non ricevono
    assegnazioni automatiche. Per i conti CO-rilevanti l'assegnazione è invece
    obbligatoria: impedisce che costi effettivi finiscano nel report
    ``Non assegnato``.
    """
    account = Account.query.get(account_id)
    if account is None or not account.active:
        raise COValidationError("Il conto selezionato non esiste o non è attivo.")
    center = get_active_cost_center(cost_center_id)
    if require_for_relevant and account.cost_relevant and center is None:
        raise COValidationError(
            f"Il centro di costo è obbligatorio per l'elemento CO {account.code} — {account.name}."
        )
    return account, center
