"""
services/routing_cost.py — Costo standard di manodopera diretta + overhead
da Ciclo di Lavorazione (Routing), metodo SAP del centro di lavoro:

    tariffa oraria overhead di un centro = pool di overhead ASSEGNATO al
    centro nel mese (ProductionOverheadItem.work_center_id) / capacità
    pianificata (WorkCenter.capacity_hours_month)

    tariffa oraria manodopera diretta = WorkCenter.hourly_rate_labor
    (inserita a mano, non calcolata dal pool: non è un costo indiretto)

applicate alle ore standard di ogni fase del ciclo. Per gli articoli che
hanno un ciclo attivo, questo sostituisce la quota-fatturato approssimata di
blueprints/production/routes.py::_calcola_overhead_da_fatturato — quel
metodo resta com'è per chi non ha ancora un ciclo, finché non si decide di
azzerarlo.

Costo pieno (materiali + manodopera + overhead) = BOM (materiali, già
calcolato da _calcola_materie_prime_da_bom) + Routing (manodopera + overhead,
qui). Tenute in due funzioni pure e componibili, senza scrivere nulla — solo
calcolo, come _calcola_materie_prime_da_bom.
"""
from decimal import Decimal

from models import Routing, ProductionOverheadItem

ZERO = Decimal("0")


def active_routing(material_id):
    """Ciclo di lavorazione ATTIVO più recente del materiale, o None se non ne ha uno."""
    return (Routing.query.filter_by(parent_material_id=material_id, active=True)
            .order_by(Routing.id.desc()).first())


def work_center_pool_mese(work_center_id, anno, mese):
    """Somma delle voci di overhead assegnate a questo centro per anno/mese."""
    righe = ProductionOverheadItem.query.filter_by(work_center_id=work_center_id, year=anno, month=mese).all()
    return sum((Decimal(str(r.amount or 0)) for r in righe), ZERO)


def work_center_overhead_rate(work_center, anno, mese):
    """
    Tariffa oraria di OVERHEAD del centro = pool assegnato al centro nel mese
    / capacità pianificata (ore/mese). Ritorna (tariffa, pool, avviso|None).
    """
    pool = work_center_pool_mese(work_center.id, anno, mese)
    capacita = Decimal(str(work_center.capacity_hours_month or 0))
    if capacita <= 0:
        return ZERO, pool, (f'Centro di lavoro {work_center.code}: capacità pianificata (ore/mese) non impostata — '
                            f'impossibile calcolare una tariffa oraria di overhead.')
    if pool <= 0:
        return ZERO, pool, (f'Centro di lavoro {work_center.code}: nessuna voce di overhead assegnata per '
                            f'{mese:02d}/{anno} — assegna le voci del pool a questo centro in '
                            f'"Pool Overhead Reparto".')
    return (pool / capacita), pool, None


def calcola_costo_pieno_da_routing(material, anno, mese):
    """
    Per ogni fase del Ciclo di Lavorazione ATTIVO di 'material', calcola:
      - costo manodopera diretta  = (labor_time_min / 60) × tariffa manodopera del centro
      - costo overhead            = ((machine_time_min + labor_time_min) / 60) × tariffa overhead del centro

    Ritorna (totale_manodopera, totale_overhead, dettaglio, avvisi):
      dettaglio: list[dict], una riga per fase
      avvisi: list[str], una voce per ciascun centro senza tariffa overhead calcolabile
    Se il materiale non ha un ciclo attivo, ritorna (0, 0, [], [avviso]).
    """
    routing = active_routing(material.id)
    if routing is None or not routing.operations:
        return ZERO, ZERO, [], [
            f'Nessun ciclo di lavorazione attivo per {material.code} — crealo in "Ciclo di Lavorazione".'
        ]

    totale_manodopera = ZERO
    totale_overhead = ZERO
    dettaglio = []
    avvisi = []
    centri_avvisati = set()
    for op in routing.operations:
        centro = op.work_center
        tariffa_ovh, pool, avviso = work_center_overhead_rate(centro, anno, mese)
        if avviso and centro.id not in centri_avvisati:
            avvisi.append(avviso)
            centri_avvisati.add(centro.id)

        tariffa_manodopera = Decimal(str(centro.hourly_rate_labor or 0))
        ore_manodopera = Decimal(str(op.labor_time_min or 0)) / 60
        ore_totali = (Decimal(str(op.machine_time_min or 0)) + Decimal(str(op.labor_time_min or 0))) / 60

        costo_manodopera_fase = ore_manodopera * tariffa_manodopera
        costo_overhead_fase = ore_totali * tariffa_ovh

        totale_manodopera += costo_manodopera_fase
        totale_overhead += costo_overhead_fase

        dettaglio.append({
            "seq": op.seq,
            "centro": centro.code,
            "descrizione": op.description or "",
            "minuti_macchina": float(op.machine_time_min or 0),
            "minuti_manodopera": float(op.labor_time_min or 0),
            "tariffa_oraria_manodopera": float(tariffa_manodopera),
            "tariffa_oraria_overhead": float(tariffa_ovh),
            "costo_manodopera": float(costo_manodopera_fase),
            "costo_overhead": float(costo_overhead_fase),
        })

    return totale_manodopera, totale_overhead, dettaglio, avvisi
