"""Pure calculation helpers for product cost / target / variance reporting.

Positive variances are unfavourable (actual cost above benchmark), negative
variances favourable.  Keeping these formulas independent from Flask/SQLAlchemy
makes the accounting conventions directly testable.
"""
from decimal import Decimal, ROUND_HALF_UP

ZERO = Decimal("0")


def dec(value):
    """Convert an input to Decimal without binary floating point artefacts."""
    return ZERO if value is None else Decimal(str(value))


def money(value):
    return dec(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def safe_div(numerator, denominator):
    denominator = dec(denominator)
    return None if denominator == ZERO else dec(numerator) / denominator


def variance(actual, benchmark):
    """Actual minus benchmark; + means unfavourable."""
    return dec(actual) - dec(benchmark)


def variance_pct(actual, benchmark):
    ratio = safe_div(variance(actual, benchmark) * 100, benchmark)
    return None if ratio is None else ratio.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def classification(value):
    value = dec(value)
    if abs(value) < Decimal("0.005"):
        return "nulla"
    return "sfavorevole" if value > ZERO else "favorevole"


def material_quantity_variance(standard_qty, actual_qty, standard_unit_cost):
    return (dec(actual_qty) - dec(standard_qty)) * dec(standard_unit_cost)


def material_price_variance(standard_unit_cost, actual_unit_cost, actual_qty):
    return (dec(actual_unit_cost) - dec(standard_unit_cost)) * dec(actual_qty)


def efficiency_variance(actual_hours, standard_hours, standard_hourly_rate):
    return (dec(actual_hours) - dec(standard_hours)) * dec(standard_hourly_rate)


def rate_variance(actual_hourly_rate, standard_hourly_rate, actual_hours):
    return (dec(actual_hourly_rate) - dec(standard_hourly_rate)) * dec(actual_hours)
