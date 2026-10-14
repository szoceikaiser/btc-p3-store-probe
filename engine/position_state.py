"""Versioned strategy state, shared by live observer and offline checkpoints.

Version 2 is complete; only current-bar E42 messages are transient. Legacy
states remain signal references with explicit missing-history information.
"""
from copy import deepcopy
from dataclasses import fields
import math
import inventory
from strategy_core import Position, PosState, Pivot, Impulse, FibZones

VERSION = 2
TRANSIENT = {'e42_meldungen'}


def pos_to_state(pos):
    d = {f.name: deepcopy(getattr(pos, f.name)) for f in fields(Position)
         if f.name not in TRANSIENT | {'state', 'zones'}}
    d.update(state_version=VERSION, pos_state=pos.state.value, zones=None)
    if pos.zones:
        z = pos.zones
        d['zones'] = dict(impuls_start=z.impulse.start.price,
            impuls_start_ts=z.impulse.start.ts, impuls_start_kind=z.impulse.start.kind,
            impuls_start_idx=z.impulse.start.idx, impuls_ende=z.impulse.end.price,
            impuls_ende_ts=z.impulse.end.ts, impuls_ende_kind=z.impulse.end.kind,
            impuls_ende_idx=z.impulse.end.idx, level_05=z.level_05, gp_upper=z.gp_upper,
            gp_lower=z.gp_lower, level_0786=z.level_0786, invalidation=z.invalidation)
        ref = pos.ziel_extrem if pos.ziel_extrem is not None else pos.retrace_extreme
        if ref is not None:
            d['zones'].update(ext1=z.ext_target(ref, 1.0), ext2=z.ext_target(ref, 1.618))
    return d


def pos_from_state(d):
    pos = Position()
    if not d:
        return pos
    version = d.get('state_version')
    if version is None and {'inventory_source', 'lots', 'valid_stop', 'cost_basis_complete'} & set(d):
        raise ValueError('Version missing from new position state')
    if version is not None and version != VERSION:
        raise ValueError('Unsupported position state version')
    if version == VERSION:
        missing = set(pos_to_state(pos)) - set(d)
        if missing:
            raise ValueError('Incomplete position state: '+', '.join(sorted(missing)))
    for f in fields(Position):
        if f.name not in TRANSIENT | {'state', 'zones'} and f.name in d:
            setattr(pos, f.name, deepcopy(d[f.name]))
    pos.state = PosState(d.get('pos_state', 'FLAT'))
    if version is None:
        pos.inventory_source = 'signal_reference'
        pos.lots = []
        pos.cost_basis_complete = pos.state == PosState.FLAT
        # Keep the historical conservative default without truncating new floats.
        pos.bestand_pct = d.get('bestand_pct', min(100, pos.entry_pct or 0)) or 0
        pos.migration_notes = ['legacy_unversioned', 'live_fills_unknown']
        if 'widerstand_exits' not in d:
            pos.migration_notes.append('widerstand_exits_missing_default_0')
        if pos.state != PosState.FLAT and 'valid_stop' not in d:
            pos.migration_notes.append('prior_stop_maximum_unknown')
    z = d.get('zones')
    if z and 'impuls_start' in z:
        required_zones = {'impuls_start_idx', 'impuls_ende_idx', 'impuls_start_ts',
            'impuls_ende_ts', 'impuls_start_kind', 'impuls_ende_kind', 'impuls_start',
            'impuls_ende', 'level_05', 'gp_upper', 'gp_lower', 'level_0786', 'invalidation'}
        if version == VERSION and not required_zones <= set(z):
            raise ValueError('Incomplete pivot state')
        imp = Impulse(
            Pivot(z.get('impuls_start_idx', 0), z.get('impuls_start_ts', 0),
                  z['impuls_start'], z.get('impuls_start_kind', 'L')),
            Pivot(z.get('impuls_ende_idx', 0), z.get('impuls_ende_ts', 0),
                  z['impuls_ende'], z.get('impuls_ende_kind', 'H')))
        pos.zones = FibZones(imp, z['level_05'], z['gp_upper'], z['gp_lower'],
                             z['level_0786'], z['invalidation'])
    inventory.validate(pos.lots)
    if pos.valid_stop is not None and (not math.isfinite(pos.valid_stop) or pos.valid_stop <= 0
                                      or not pos.valid_stop_reason):
        raise ValueError('Invalid stored stop boundary')
    if pos.inventory_source not in ('signal_reference', 'simulated_fills'):
        raise ValueError('Unknown inventory source; no live fill import exists')
    if pos.inventory_source == 'signal_reference' and pos.lots:
        raise ValueError('Signal state cannot assert filled lots')
    if pos.inventory_source == 'simulated_fills' and pos.cost_basis_complete != inventory.summary(pos.lots)['complete']:
        raise ValueError('Stored cost completeness disagrees with lots')
    return pos
