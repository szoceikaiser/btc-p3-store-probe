"""A2: causal, versioned as-of contract for optional flow measurements.

Times are UTC milliseconds. A bar-indexed observation becomes available at the
bar close; a snapshot or funding timestamp becomes available at its own time.
This is a processing assumption, not proof of historical API publication.
"""

from bisect import bisect_right
import math

FLOW_VERSION = "a2-flow-v1"
FOUR_HOURS_MS = 4 * 60 * 60 * 1000
MAX_OI_AGE_MS = 2 * FOUR_HOURS_MS
MAX_FUNDING_AGE_MS = 2 * FOUR_HOURS_MS


class AsOfSeries:
    def __init__(self, pairs, availability_lag_ms=0):
        self.rows = sorted((int(t) + availability_lag_ms, int(t), float(v))
                           for t, v in pairs)
        self.times = [row[0] for row in self.rows]


def asof(pairs, decision_ts, source, max_age_ms, availability_lag_ms=0):
    """Return (number, evidence); no value is backfilled before availability."""
    series = pairs if isinstance(pairs, AsOfSeries) else AsOfSeries(pairs, availability_lag_ms)
    i = bisect_right(series.times, decision_ts) - 1
    if i < 0:
        return 0.0, dict(source=source, sample_ts=None, available_ts=None,
                         decision_ts=decision_ts, age_ms=None, coverage="missing")
    available, measured, value = series.rows[i]
    age = decision_ts - available
    return value, dict(source=source, sample_ts=measured, available_ts=available,
                       decision_ts=decision_ts, age_ms=age,
                       coverage=("stale" if age > max_age_ms else
                                 "observed" if age == 0 else "carried"))


def direct(ts, decision_ts, source, present=True, available_ts=None):
    if not present:
        return dict(source=source, sample_ts=None, available_ts=None,
                    decision_ts=decision_ts, age_ms=None, coverage="missing")
    available = decision_ts if available_ts is None else available_ts
    return dict(source=source, sample_ts=ts, available_ts=available,
                decision_ts=decision_ts, age_ms=decision_ts - available,
                coverage="observed")


def usable(point, field):
    """Legacy hand-built points remain measured inputs; producers set evidence."""
    meta = point.provenance.get(field)
    return meta is None or meta["coverage"] in ("observed", "carried")


def liquidation_usable(point, field):
    """UM2-M5-v1 evidence for one liquidation side, including measured zero.

    Unlike legacy optional-flow callers, absence of evidence is unknown. This
    validates existing observations; it never creates or carries a measurement.
    The four-hour availability assumption is the frozen A2/UM2 contract.
    """
    meta = point.provenance.get(field)
    if not isinstance(meta, dict) or meta.get('coverage') not in ('observed', 'carried'):
        return False
    sample, available, decision, age = [meta.get(k) for k in
        ('sample_ts', 'available_ts', 'decision_ts', 'age_ms')]
    if not all(type(x) is int for x in (sample, available, decision, age)):
        return False
    if (decision != point.ts + FOUR_HOURS_MS or sample > point.ts
            or available < sample + FOUR_HOURS_MS or available > decision
            or age != decision - available or age < 0):
        return False
    if ((meta['coverage'] == 'observed' and age != 0)
            or (meta['coverage'] == 'carried' and age == 0)):
        return False
    value = getattr(point, field)
    return math.isfinite(value) and value >= 0


def legacy_unknown(ts, decision_ts):
    """Old serialized rows carry numbers, but no proof those zeros were measured."""
    return {field: dict(source="legacy_unverified", sample_ts=None,
                        available_ts=None, decision_ts=decision_ts,
                        age_ms=None, coverage="missing")
            for field in ("spot_cvd", "fut_cvd", "oi", "oi_btc", "funding",
                          "long_liq", "short_liq", "long_pct")}
