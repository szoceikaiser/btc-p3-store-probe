"""Remaining Long/Spot lots. No signal-to-live-fill inference."""
from copy import deepcopy
import math


def summary(lots, kind=None):
    selected = [l for l in lots if kind is None or l['kind'] == kind]
    units = math.fsum(l['units'] for l in selected)
    complete = all(l['cost'] is not None for l in selected)
    cost = math.fsum(l['cost'] for l in selected) if complete else None
    return dict(units=units, cost=cost, entry=cost/units if units and complete else None,
                complete=complete)


def buy(lots, *, lot_id, at, price, units, cost, fee, kind):
    if any(l['id'] == lot_id for l in lots):
        raise ValueError('Duplicate lot ID')
    lots.append(dict(id=lot_id, at=at, fill_price=price, units=units,
                     cost=cost, buy_fee=fee, kind=kind))


def sell(lots, units, *, e42_only=False, close_cohort=False,
         rounding_scale=None):
    """Proportional within the selected cohort; return disposed cost/fee.

    No absolute dust threshold: tiny real BTC retain their cost. A full sale
    removes its cohort exactly. Caller supplies F09-corrected executable units.
    """
    selected = [l for l in lots if not e42_only or l['kind'] == 'e42']
    total = math.fsum(l['units'] for l in selected)
    # Summing many surviving lots and updating aggregate units follow different
    # floating-point paths. Bound only that accumulation noise, per lot.
    scale = total if rounding_scale is None else max(total, rounding_scale)
    rounding = max(max(16, 8*len(selected))*math.ulp(total),
                   8*math.ulp(scale)) if total else 0.
    if not total or units > total + rounding:
        raise ValueError('Lot oversell')
    fraction = 1. if close_cohort else min(1., units/total)
    cost = math.fsum(l['cost']*fraction for l in selected) if all(l['cost'] is not None for l in selected) else None
    fee = math.fsum((l['buy_fee'] or 0)*fraction for l in selected)
    for lot in selected:
        lot['units'] *= 1-fraction
        if lot['cost'] is not None:
            lot['cost'] *= 1-fraction
        if lot['buy_fee'] is not None:
            lot['buy_fee'] *= 1-fraction
    lots[:] = [l for l in lots if l['units'] > 0]
    return cost, fee


def validate(lots):
    ids = set()
    for lot in lots:
        if set(lot) != {'id', 'at', 'fill_price', 'units', 'cost', 'buy_fee', 'kind'}:
            raise ValueError('Incomplete lot')
        if lot['id'] in ids or lot['kind'] not in ('base', 'e42'):
            raise ValueError('Invalid lot identity/cohort')
        ids.add(lot['id'])
        if not math.isfinite(lot['units']) or lot['units'] <= 0:
            raise ValueError('Invalid lot units')
        for key in ('cost', 'buy_fee', 'fill_price'):
            if lot[key] is not None and (not math.isfinite(lot[key]) or lot[key] < 0):
                raise ValueError('Invalid lot cost/price')
    return deepcopy(lots)
