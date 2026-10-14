"""Native UM2 gates with optional process-local diagnostic collection."""
from contextlib import contextmanager
from contextvars import ContextVar
from flow_contract import liquidation_usable

SEMANTICS_VERSION = 'UM2-M5-v1-native'
_events = ContextVar('liquidation_gate_events', default=None)


@contextmanager
def collect_events():
    events = []
    token = _events.set(events)
    try:
        yield events
    finally:
        _events.reset(token)


def record(reason, side, decision_at, **details):
    events = _events.get()
    if events is not None:
        events.append(dict(reason=reason, side=side, decision_at=decision_at, **details))


def known(points, fields, reason, side, decision_at):
    unknown = [p.ts for p in points if not all(liquidation_usable(p, field) for field in fields)]
    if unknown:
        record(reason, side, decision_at, used_window_opens=[p.ts for p in points], unknown_opens=unknown)
    return not unknown
