"""Kern-Engine der Order-Flow-Strategie (nach Furkan Yildirim).

Offline-Modul ohne Netzabhaengigkeit (E4a). Regeln: docs/STRATEGIE.md.
Alle Preise in USD. Timeframe-agnostisch: arbeitet auf einer Liste
abgeschlossener Kerzen (primaer 4h) + optionalen Order-Flow-Serien.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional
from flow_contract import usable
from flow_contract import FOUR_HOURS_MS
import liquidation_evidence as liq_evidence

# ---------------------------------------------------------------- Datentypen


@dataclass(frozen=True)
class Candle:
    ts: int          # Open-Time in ms (UTC), eindeutiger Schluessel
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class FlowPoint:
    """Order-Flow-Daten je Kerze (aggregiert ueber Boersen)."""
    ts: int
    spot_cvd: float      # kumuliertes Spot-Delta (USD)
    fut_cvd: float       # kumuliertes Futures-Delta (USD)
    oi: float            # Open Interest (USD); missing/stale status is in provenance
    funding: float       # 8h-Funding-Rate; zero needs observed coverage to confirm
    long_liq: float = 0.0   # Long-Liquidationen dieser Kerze (USD), E9.1 (Coinalyze)
    short_liq: float = 0.0  # Short-Liquidationen dieser Kerze (USD)
    long_pct: float = 0.0   # Anteil der Long-Positionierung in % (E16, Coinalyze);
                            # 0.0 = keine Daten. >50 = mehrheitlich long.
    oi_btc: float = 0.0     # Open Interest in BTC = Kontrakte (E43.4, oi_in_btc);
                            # 0.0 = keine Kontrakt-Reihe. `oi` bleibt in USD.
    provenance: dict = field(default_factory=dict)  # A2: per-field source/time/coverage/age


class Pattern(Enum):
    """Der Order-Flow-Kompass (STRATEGIE.md Abschnitt 3).

    Muster 1-4 stammen woertlich aus Furkans Notizen (Frame 14:50). Muster 5 ist eine
    Ergaenzung aus E13: Furkans vier Muster beschreiben drei steigende Maerkte und EINEN
    gesunden Absturz (Kapitulation). Fuer einen UNgesunden Absturz gab es keinen Begriff —
    er landete auf NEUTRAL und war damit von einem ruhigen Seitwaertsmarkt nicht zu
    unterscheiden. Genau in dieser Lage hat die Engine gekauft (16 von 34 Ersteinstiegen
    bei NEUTRAL). Muster 5 gibt der Lage einen Namen, damit man sie sperren kann.
    """
    GESUNDER_TREND = 1
    DERIVATE_PUMP = 2
    SHORT_COVERING = 3
    CAPITULATION_RESET = 4
    UNGESUNDER_ABVERKAUF = 5
    NEUTRAL = 0


class SignalType(Enum):
    KAUF_1 = "KAUF 1 (Teilposition am 0.5-Level)"
    KAUF_2 = "KAUF 2 (Kernposition im Golden Pocket)"
    NACHKAUF = "NACHKAUF (0.786-Zone)"
    TEILVERKAUF_LADDER = "TEILVERKAUF Leiter (Zwischenziel vor 1.0)"
    TEILVERKAUF_1 = "TEILVERKAUF 1 (Extension 1.0)"
    TEILVERKAUF_2 = "TEILVERKAUF 2 (Extension 1.618)"
    VERKAUF_REST = "VERKAUF Rest (Muster/Divergenz am Ziel)"
    STOPLOSS = "STOPLOSS"
    WARNUNG = "WARNUNG (Derivate-Pump aktiv)"
    # Short-Seite (spiegelbildlich)
    SHORT_1 = "SHORT 1 (Teilposition am 0.5-Level)"
    SHORT_2 = "SHORT 2 (Kernposition im Golden Pocket)"
    SHORT_NACHLEGEN = "SHORT NACHLEGEN (0.786-Zone)"
    SHORT_TP_LADDER = "SHORT TEILGEWINN Leiter (Zwischenziel vor 1.0)"
    SHORT_TP_1 = "SHORT TEILGEWINN 1 (Extension 1.0)"
    SHORT_TP_2 = "SHORT TEILGEWINN 2 (Extension 1.618)"
    SHORT_COVER_REST = "SHORT Rest schliessen"
    SHORT_STOPLOSS = "SHORT STOPLOSS"
    # E44.3 (E42, Kaisers Regel "Ausbruch mit Ruecktest", Default aus). Eigene Typen,
    # weil der Rueckkauf-Teil einen EIGENEN Stop hat (Kaiser 27.09.2026: "nur die 25 %")
    # und die Abrechnung genau diesen Teil wieder verkaufen muss - weder ein Kauf am
    # Fib-Level noch ein Teilgewinn beschreibt das.
    RUECKKAUF = "RUECKKAUF (Ausbruch mit Ruecktest)"
    RUECKKAUF_STOP = "STOP Rueckkauf-Teil (Schluss unter der Ausbruchsmarke)"
    SHORT_RUECKTEST = "SHORT NACH RUECKTEST (Durchbruch mit Ruecktest)"
    SHORT_RUECKTEST_STOP = "STOP Short-Rueckteil (Schluss ueber der Durchbruchsmarke)"


@dataclass
class Signal:
    ts: int
    type: SignalType
    price: float
    tranche_pct: int          # Anteil der Gesamtposition in %
    reason: str               # Begruendung (Muster/Level) fuer die Telegram-Nachricht
    stop_ref: Optional[float] = None
    tag: str = ""             # z. B. "FLUSH" = aggressiver Kapitulations-Einstieg (Kaiser entscheidet)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.name
        d["label"] = self.type.value
        return d


@dataclass(frozen=True)
class Pivot:
    idx: int
    ts: int
    price: float
    kind: str  # "H" oder "L"


@dataclass
class Impulse:
    """Letzter signifikanter Impuls: Basis fuer Fib-Zonen."""
    start: Pivot
    end: Pivot

    @property
    def up(self) -> bool:
        return self.end.price > self.start.price

    @property
    def range(self) -> float:
        return abs(self.end.price - self.start.price)


@dataclass
class FibZones:
    """Alle relevanten Zonen eines Impulses (Richtung folgt dem Impuls)."""
    impulse: Impulse
    level_05: float
    gp_upper: float   # 0.618
    gp_lower: float   # 0.65 (bei Long unterhalb von 0.618)
    level_0786: float
    invalidation: float  # 1.0 = Startpunkt des Impulses

    def ext_target(self, retrace_extreme: float, factor: float = 1.0) -> float:
        """Extension-Ziel vom Retracement-Extrem aus (1.0 = gleiche Bewegung)."""
        sign = 1.0 if self.impulse.up else -1.0
        return retrace_extreme + sign * factor * self.impulse.range


# ------------------------------------------------------------------ Analyse


def atr(candles: list[Candle], period: int = 14) -> float:
    """Average True Range der letzten `period` Kerzen (einfacher Durchschnitt)."""
    if period < 1:
        raise ValueError("period muss mindestens 1 sein")
    if len(candles) < 2:
        return 0.0
    trs = []
    # Fuer n beobachtbare True Ranges braucht es n+1 Kerzen. Bei kurzem
    # Vorlauf wurden die zwei Slices bisher unterschiedlich abgeschnitten und
    # falsche Paare (oder nur Kerzenranges) gemittelt.
    window = candles[-(period + 1):]
    for prev, cur in zip(window, window[1:]):
        trs.append(max(cur.high - cur.low,
                       abs(cur.high - prev.close),
                       abs(cur.low - prev.close)))
    return sum(trs) / len(trs) if trs else 0.0


def find_pivots(candles: list[Candle], n: int = 5) -> list[Pivot]:
    """Pivot-Hochs/-Tiefs mit n Kerzen Bestaetigung links UND rechts.

    Ein Pivot gilt erst als bestaetigt, wenn n Folgekerzen vorliegen —
    dadurch 'wandern' die Zonen erst mit bestaetigter neuer Struktur
    (dynamische Golden Pockets, STRATEGIE.md 4.1).
    """
    pivots: list[Pivot] = []
    for i in range(n, len(candles) - n):
        c = candles[i]
        window = candles[i - n:i] + candles[i + 1:i + n + 1]
        if all(c.high >= w.high for w in window):
            pivots.append(Pivot(i, c.ts, c.high, "H"))
        if all(c.low <= w.low for w in window):
            pivots.append(Pivot(i, c.ts, c.low, "L"))
    # bei Duplikaten (H und L derselben Kerze) Reihenfolge stabil halten
    pivots.sort(key=lambda p: (p.idx, p.kind))
    # aufeinanderfolgende gleiche Typen: nur das Extrem behalten
    cleaned: list[Pivot] = []
    for p in pivots:
        if cleaned and cleaned[-1].kind == p.kind:
            keep = (p.price >= cleaned[-1].price) if p.kind == "H" else (p.price <= cleaned[-1].price)
            if keep:
                cleaned[-1] = p
        else:
            cleaned.append(p)
    return cleaned


# E19 (Befund 2026-08-27, Furkan-Video vom 02.08.): Die Wahl des Referenz-Beins war der
# blinde Fleck des Projekts. Am 02.08.2026 zeichnete die Engine ein Zwei-Tage-Bein von
# 4,8 % (65.409->62.275, Richtung SHORT und damit bei bias_short=false unhandelbar),
# waehrend Furkan zur selben Stunde ueber drei Wochen und 15,8 % mass (57.800->66.956).
# Seine Zonen und unsere Formel stimmen exakt ueberein — nur das Bein nicht.
#
# Warum das blockiert: Der Abstand vom Golden Pocket zur Invalidierung betraegt immer rund
# 35-38 % der Beinlaenge. `min_stop_pct` = 2 % verlangt damit implizit ein Bein von etwa
# 5,5 % — die Auswahl liefert im Median aber 4,0 %. Gemessen an 30 Tagen (27.07.-27.08.):
# 17 Tage Richtung SHORT (gesperrt), 9 Tage LONG mit Stop-Abstand unter 2 % (verworfen),
# nur 4 Tage handelbar. Die Engine zeichnete Beine, die fuer ihre eigene Mindest-
# anforderung zu klein sind.
#
# Zwei Schalter, beide Default aus:
#   min_bein_pct  — harte Untergrenze fuer die Beinlaenge; kleinere Beine kommen als
#                   Referenz gar nicht in Frage, die Suche geht zum naechstgroesseren.
#   bein_wahl     — "juengstes" (bisher) oder "groesstes": unter den letzten
#                   `bein_pivots` Pivots das groesste signifikante Bein.
BEIN_PIVOTS = 12          # Fenster fuer bein_wahl="groesstes" (Struktur statt Uhrzeit)


def last_significant_impulse(candles: list[Candle], pivots: list[Pivot],
                             k_atr: float = 3.0, min_pct: float = 0.03,
                             min_bein_pct: float = 0.0,
                             bein_wahl: str = "juengstes",
                             bein_pivots: int = BEIN_PIVOTS,
                             nur_auf: Optional[bool] = None) -> Optional[Impulse]:
    """Referenz-Impuls (Pivot->Pivot) fuer die Fib-Zonen.

    Signifikant = Spanne >= k_atr * ATR(14) ODER >= min_pct des Startpreises.
    Zusaetzlich (E19) muss die Spanne >= min_bein_pct des Startpreises sein, wenn gesetzt.
    Der Zeitabschnitt ergibt sich aus der Swing-Struktur selbst (kein starres Fenster).

    bein_wahl="juengstes": das juengste Bein, das alle Bedingungen erfuellt (bisheriges
    Verhalten). "groesstes": unter den Beinen der letzten `bein_pivots` Pivots das mit der
    groessten Spanne — bei Gleichstand das juengere.

    nur_auf: True = nur Aufwaerts-Beine (Long-Setups), False = nur Abwaerts-Beine,
    None = beide (bisheriges Verhalten). Grundlage ist Frame 16:12/16:45 des Videos vom
    02.08.2026: Furkan fuehrt ZWEI Fib-Raster gleichzeitig — das grosse Aufwaerts-Bein
    57.802,4 -> 66.939,7 fuer seine Long-Nachkaeufe und das kleine Abwaerts-Bein von
    demselben Hoch als Widerstandszone. Unsere Engine kennt nur eins und nahm an jenem
    Tag das kleine, abwaertsgerichtete — das sie bei bias_short=false nicht handeln darf.
    """
    if len(pivots) < 2:
        return None
    a = atr(candles)

    def _taugt(start: Pivot, end: Pivot) -> bool:
        if start.kind == end.kind:
            return False
        if nur_auf is not None and (end.price > start.price) != nur_auf:
            return False
        rng = abs(end.price - start.price)
        if min_bein_pct > 0 and start.price > 0 and rng < min_bein_pct * start.price:
            return False
        return rng >= k_atr * a or rng >= min_pct * start.price

    if bein_wahl == "groesstes":
        fenster = pivots[-bein_pivots:] if bein_pivots > 0 else pivots
        bester, beste_spanne = None, -1.0
        for i in range(len(fenster) - 1, 0, -1):
            start, end = fenster[i - 1], fenster[i]
            if not _taugt(start, end):
                continue
            spanne = abs(end.price - start.price)
            if spanne > beste_spanne:          # ">" statt ">=" -> bei Gleichstand das juengere
                bester, beste_spanne = Impulse(start, end), spanne
        return bester

    for i in range(len(pivots) - 1, 0, -1):
        start, end = pivots[i - 1], pivots[i]
        if _taugt(start, end):
            return Impulse(start, end)
    return None


def fib_zones(imp: Impulse) -> FibZones:
    """Fib-Retracement-Zonen des Impulses (Levels aus dem Video: 0.5/0.618/0.65/0.786)."""
    h, l = (imp.end.price, imp.start.price) if imp.up else (imp.start.price, imp.end.price)
    rng = h - l
    if imp.up:  # Retracement vom Hoch nach unten
        lv = lambda r: h - r * rng
    else:       # Short: Retracement vom Tief nach oben
        lv = lambda r: l + r * rng
    return FibZones(
        impulse=imp,
        level_05=lv(0.5),
        gp_upper=lv(0.618),
        gp_lower=lv(0.65),
        level_0786=lv(0.786),
        invalidation=imp.start.price,
    )


# ---------------------------------- Uebergeordneter Kontext (1D-Ebene)
# Furkans Entscheidungsprozess (Transkript 16:03-19:41): ZUERST der uebergeordnete
# Bias/Trend, DANN Orderflow+Fib nur zum Timing des Einstiegs. Die folgenden Helfer
# leiten die 1D-Ebene aus den vorhandenen 4h-Kerzen ab (Resampling), damit Live und
# Backtest dieselbe Logik nutzen.


def ema(values: list[float], period: int) -> Optional[float]:
    """Exponentieller gleitender Durchschnitt; liefert den letzten Wert."""
    if not values:
        return None
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def resample_daily(candles: list[Candle]) -> list[Candle]:
    """Nur vollstaendige UTC-Tage aus bereits abgeschlossenen 4h-Kerzen.

    Der letzte 20-Uhr-Slot schliesst erst an der naechsten UTC-Mitternacht.
    Die Eingangsadapter muessen nach D01 laufende 4h-Kerzen entfernen.
    """
    step = 14_400_000
    day_ms = 86_400_000
    days: dict[int, dict[int, Candle]] = {}
    invalid_days = set()
    for c in candles:
        day = (c.ts // day_ms) * day_ms
        slot = c.ts - day
        if slot % step:
            invalid_days.add(day)
            continue
        bucket = days.setdefault(day, {})
        # Doppelte Eingaben belegen keine zweite abgeschlossene Kerze.
        if slot in bucket:
            bucket[slot] = None
        else:
            bucket[slot] = c
    out = []
    for day in sorted(days):
        if day in invalid_days:
            continue
        slots = days[day]
        if set(slots) != {i * step for i in range(6)} or any(c is None for c in slots.values()):
            continue
        ordered = [slots[i * step] for i in range(6)]
        out.append(Candle(day, ordered[0].open, max(c.high for c in ordered),
                          min(c.low for c in ordered), ordered[-1].close))
    return out


def daily_trend(candles: list[Candle], period: int = 50, streng: bool = False):
    """(letzter Tages-Schluss, Tages-EMA(period)) — Basis fuer den Trendfilter.

    E33 (13.09.2026) — STILLER FEHLER, der hier lag: Die Zeile rechnete
    `ema(closes, min(period, len(closes)))`. Wer `period=200` verlangte, aber nur 67
    Tage Historie hatte (Live-Engine mit LIMIT=400), bekam klaglos einen EMA ueber 67
    Tage zurueck - als waere es ein EMA200. Nachgemessen an einer 1300-Kerzen-Serie
    lagen die beiden Werte 21 % auseinander. Schlimmer noch: Der Backtest sieht das
    volle Fenster und rechnete den ECHTEN EMA200 - Live und Messung haetten also
    verschiedene Dinge getan, ohne dass es jemandem aufgefallen waere.

    `streng=True` gibt in diesem Fall None zurueck: lieber keine Aussage als eine
    falsche. Aufrufer behandeln None als "unbekannt" und blockieren nichts.
    `streng=False` bleibt das alte Verhalten (rueckwaertskompatibel).
    """
    daily = resample_daily(candles)
    if len(daily) < 2:
        return None
    if streng and len(daily) < period:
        return None
    closes = [c.close for c in daily]
    return daily[-1].close, ema(closes, min(period, len(closes)))


def trend_lage(candles: list[Candle], period: int = 200) -> Optional[dict]:
    """Der uebergeordnete Trend als ANZEIGE (E33-B) - nicht als Regel.

    Furkan leitet seinen Bias aus Makro ab (Transkript 15:58: "Ich benutze keine 17
    verschiedenen Indikatoren ... Makrokorrelation, US-Aktienmarkt, Renditen ... damit
    ich ueberhaupt erstmal meinen Bias habe"). Ein Tages-EMA ist NICHT seine Methode,
    sondern der backtestbare Behelf aus STRATEGIE.md Abschnitt 5.

    Deshalb steht er zuerst nur in der Nachricht: Kaiser sieht die Lage und entscheidet.
    Neun Order-Flow-FILTER wurden in diesem Projekt gemessen, alle waren schlechter -
    die ANZEIGE kostet dagegen nichts.

    Gibt None zurueck, wenn die Historie fuer den verlangten EMA nicht reicht.
    """
    t = daily_trend(candles, period, streng=True)
    if t is None:
        return None
    close, e = t
    if e is None:
        return None
    ueber = close >= e
    return {
        "stand": "ueber" if ueber else "unter",
        "ema": e, "period": period, "kurs": close,
        "text": (f"Uebergeordnet: Kurs {_p(close)} {'ueber' if ueber else 'unter'} "
                 f"EMA{period} ({_p(e)})"),
    }


def daily_fib_zone(candles: list[Candle], pivot_n: int = 5,
                   k_atr: float = 3.0, min_bein_pct: float = 0.0,
                   bein_wahl: str = "juengstes",
                   pivot_n_1d: int = 0) -> Optional[FibZones]:
    """Fib-Zonen des letzten signifikanten 1D-Impulses (fuer die 4h+1D-Konfluenz).

    E32.3 (13.09.2026): `pivot_n_1d` ist die Swing-Weite AUF DER TAGESEBENE. 0 = wie
    `pivot_n`, also das bisherige Verhalten.

    WARUM DAS NOETIG WURDE: E23 rief diese Funktion mit `pivot_n` auf - also mit der
    Weite, die fuer 4h-Kerzen eingestellt ist (5). Auf Tageskerzen ist das sehr fein;
    schon ein kleines Zwischentief zaehlt dann als Swing, und `bein_wahl="juengstes"`
    nimmt anschliessend das kleine, junge Bein. Die "1D-Ebene" war damit nicht Furkans
    uebergeordnete Ebene, sondern dieselbe Feinstruktur auf groeberen Kerzen.

    Nachgerechnet am 13.09.2026 mit echten Tageskerzen (Stand 12.09.):
      n=5 -> Bein 76.264 -> 82.300 ( 8 %), Golden Pocket 78.377-78.570
      n=8 -> Bein 62.535 -> 82.300 (32 %), Golden Pocket 69.453-70.085
    Furkan nannte im Video vom 10.09.2026 "das Golden Pocket aus dieser Bewegung von
    62.000 auf 82.000" - im Chart abgelesen 69.000-70.000. Das trifft n=8, nicht n=5.

    PREIS DAFUER (ebenfalls nachgerechnet, ehrlich halten): Ein Pivot bei n=8 braucht
    acht Tageskerzen RECHTS zur Bestaetigung. Ueber die letzten 21 Tage lieferte n=8 an
    8 Tagen gar kein Bein, waehrend n=5 durchgehend eines hatte. Die groebere Ebene ist
    traeger und zeigt die Zone spaeter. Ob sich das lohnt, entscheidet der Backtest.
    """
    daily = resample_daily(candles)
    n1d = pivot_n_1d if pivot_n_1d > 0 else pivot_n
    if len(daily) < 2 * n1d + 2:
        return None
    piv = find_pivots(daily, n=n1d)
    imp = last_significant_impulse(daily, piv, k_atr=k_atr, min_bein_pct=min_bein_pct,
                                   bein_wahl=bein_wahl)
    return fib_zones(imp) if imp is not None else None


def gleiches_bein(a: Optional[Impulse], b: Optional[Impulse]) -> bool:
    """Zeichnen zwei Ebenen dasselbe Bein? (E23)

    Wenn die 1D-Ebene denselben Impuls liefert wie die 4h-Ebene, waere ein zweiter
    Einstiegsversuch reine Doppelarbeit — die Zonen waeren identisch. Verglichen werden
    die Preise, nicht die Zeitstempel: dieselbe Struktur hat auf 4h und 1D verschiedene
    Kerzen-ts, aber dieselben Hoch-/Tiefpunkte.
    """
    if a is None or b is None:
        return False
    return (abs(a.start.price - b.start.price) < 0.01
            and abs(a.end.price - b.end.price) < 0.01)


def trend_intakt(alt: Impulse, neu: Impulse) -> bool:
    """Setzt `neu` den Trend von `alt` intakt fort? (E30)

    Long-Bein (start=Tief, end=Hoch): hoeheres Tief UND hoeheres Hoch.
    Short-Bein (start=Hoch, end=Tief): tieferes Hoch UND tieferes Tief.
    Richtungswechsel gilt nie als intakt.
    """
    if alt is None or neu is None or alt.up != neu.up:
        return False
    if alt.up:
        return neu.start.price > alt.start.price and neu.end.price > alt.end.price
    return neu.start.price < alt.start.price and neu.end.price < alt.end.price


def gegen_zonen(candles: list[Candle], pivots: list[Pivot], long_side: bool,
                k_atr: float = 2.0, min_pct: float = 0.03) -> Optional[FibZones]:
    """Das ZWEITE Fib-Raster: das juengste signifikante Bein GEGEN die Handelsrichtung.

    E20 (Furkan-Videos 02./03.08.2026, in den Frames sichtbar): Er fuehrt zwei Raster
    gleichzeitig. Das Bein in Handelsrichtung liefert die Kaufzonen UNTER dem Kurs; das
    Gegen-Bein — bei einem Long also die letzte Abwaertsbewegung — liefert die
    Widerstandsmarken UEBER dem Kurs, an denen er Teilgewinne nimmt:

        "Die ersten wichtigen Bereiche in dieser Gegenbewegung wird natuerlich das Golden
         Pocket sein. 64.200 bis 64.300. Da koennte Widerstand vorherrschen. Idealerweise
         kommen wir hier drueber und nehmen das raus." (03.08.2026, 3:00-3:16)

    Unsere Engine hatte am Vorabend fuer dasselbe Bein 64.212-64.312 gerechnet — zwoelf
    Dollar daneben. Sie sah die Zone also, benutzte sie nur nicht.

    Die zurueckgegebenen Levels lesen sich fuer einen Long von unten nach oben:
    level_05 < gp_upper (0.618) < gp_lower (0.65) < level_0786 < invalidation (= altes
    Hoch, darueber ist der Widerstand weg). Bei einem Short spiegelbildlich.

    Bewusst OHNE min_bein_pct: Diese Mindestlaenge gehoert zur Einstiegs-Seite (Abstand
    zum Stop). Ein Gegen-Bein ist naturgemaess kleiner und dient nur der Orientierung.
    """
    imp = last_significant_impulse(candles, pivots, k_atr=k_atr, min_pct=min_pct,
                                   nur_auf=not long_side)
    return fib_zones(imp) if imp is not None else None


# --------------------------------------------------- Order-Flow-Kompass


def _slope(vals: list[float]) -> float:
    """Relative Veraenderung ueber das Fenster (robust gegen Skalenunterschiede)."""
    if len(vals) < 2 or vals[0] == 0:
        return 0.0
    return (vals[-1] - vals[0]) / abs(vals[0])


def _muster2_dollar(c: list[Candle], f: list[FlowPoint]) -> Optional[tuple[float, float]]:
    """(Spot-Delta, Futures-Delta) im Fenster, beide in DOLLAR (E43.3, Befund A2).

    Der alte Vergleich in Muster 2 teilte die Veraenderung im Fenster durch den STAND
    der kumulierten Summe am Fensteranfang. Dieser Stand ist willkuerlich - er haengt
    nur davon ab, wo die Summe zu laufen begann (live: ab der ersten geladenen Kerze,
    im Backtest: ab Datenbeginn). Dieselbe Marktlage ergab so je nach Startpunkt
    "gesunder Trend" oder "Derivate-Pump".

    Hier zaehlen nur Differenzen INNERHALB des Fensters - ein konstanter Startwert
    kuerzt sich heraus:
      - Spot: Binance-Reihe als USD-Fensterdifferenz. Bei aggregiertem Spot-CVD
        (BTC) jedes Kerzen-Delta mit dem Schlusskurs derselben Kerze in USD.
      - Futures: jedes Kerzen-Delta (BTC) mal Schlusskurs DERSELBEN Kerze, dann
        aufsummiert - dieselbe Umrechnung wie _fut_cvd_usd (E43.1), nur fensterlokal.
    Beide decken dieselben Kerzen ab (die Deltas von Kerze 2 bis 12 des Fensters).

    None, wenn zu einem Flow-Punkt keine Kerze existiert: lieber kein Derivate-Pump
    als einer mit falschem Kurs. Live und im Backtest entstehen Kerze und Flow-Punkt in
    derselben Schleife, der Fall tritt dort nicht auf.
    """
    kurs = {x.ts: x.close for x in c}
    fut = 0.0
    for vorher, p in zip(f, f[1:]):
        k = kurs.get(p.ts)
        if k is None:
            return None
        fut += (p.fut_cvd - vorher.fut_cvd) * k
    spot_sources = {p.provenance.get("spot_cvd", {}).get("source", "binance_spot_taker_usd")
                    for p in f}
    if len(spot_sources) != 1:
        return None
    if spot_sources == {"aggregate_spot_delta_btc"}:
        spot = 0.0
        for vorher, p in zip(f, f[1:]):
            k = kurs.get(p.ts)
            if k is None:
                return None
            spot += (p.spot_cvd - vorher.spot_cvd) * k
    else:
        spot = f[-1].spot_cvd - f[0].spot_cvd
    return spot, fut


def oi_in_btc(oi_usd: dict, kurs: dict) -> dict:
    """{ts: Open Interest in BTC} aus {ts: Open Interest in USD} (E43.4, Befund A3).

    Coinalyze liefert das OI mit convert_to_usd=true, also Kontrakte x Kurs. Ein
    Dollar-OI steigt deshalb schon mit dem Kurs, auch wenn niemand eine Position
    eroeffnet. Jeder OI-Punkt wird mit dem Schlusskurs DERSELBEN Kerze (gleiche ts,
    `kurs` = {ts: close}) in BTC umgerechnet - beide gelten zum Kerzenschluss.

    Umgerechnet wird HIER, am Datenpunkt, und erst danach aufgefuellt (live
    main.fetch_market_data, im Backtest backtest.build_series - beide rufen diese
    Funktion). Wer erst die Dollar-Werte auffuellt und dann durch den Kurs teilt, teilt
    einen alten Wert durch einen neuen Kurs und erfindet eine OI-Bewegung in Hoehe der
    Kursbewegung - genau den Fehler, den E43.4 beheben soll.

    Punkte ohne Kerze gleicher ts entfallen: lieber kein Wert als einer mit falschem
    Kurs (Haltung wie _fut_cvd_usd).
    """
    return {ts: v / kurs[ts] for ts, v in oi_usd.items() if kurs.get(ts)}


def oi_aenderung(f: list[FlowPoint], muster_oi: str = "usd") -> float:
    """Veraenderung des Open Interest im Fenster `f` als Anteil (0.03 = +3 %).

    muster_oi (E43.4): "usd" (Default, bisheriges Verhalten) rechnet mit dem Dollar-OI,
    "btc" mit den Kontrakten (FlowPoint.oi_btc). Die Formel ist dieselbe, nur die
    Einheit wechselt. Ohne Reihe (Wert 0 am Rand des Fensters) ist die Aenderung 0 -
    dieselbe neutrale Antwort, die "usd" ohne OI-Daten gibt.

    "usd" rechnet Zeichen fuer Zeichen wie vor E43.4 (auch im Randfall OI am Ende 0).
    """
    if muster_oi == "btc":
        if not (usable(f[0], "oi_btc") and usable(f[-1], "oi_btc")):
            return 0.0
        a, b = f[0].oi_btc, f[-1].oi_btc
        return (b - a) / a if a and b else 0.0
    if not (usable(f[0], "oi") and usable(f[-1], "oi")):
        return 0.0
    return (f[-1].oi - f[0].oi) / f[0].oi if f[0].oi else 0.0


# ------------------------------------------------ E32: Lage-Bericht (reine Information)

# Klartext zu den Kompass-Mustern. Pattern.UNGESUNDER_ABVERKAUF sagt einem Menschen
# nichts; in der Telegram-Nachricht muss stehen, was es bedeutet.
MUSTER_KLARTEXT = {
    "GESUNDER_TREND": "gesunder Trend (Spot traegt die Bewegung)",
    "DERIVATE_PUMP": "Derivate-Pump (Hebel treibt, Spot fehlt)",
    "SHORT_COVERING": "Short-Covering (Shorts decken sich ein)",
    "CAPITULATION_RESET": "Kapitulation (der Markt ist ausgeraeumt)",
    # E38 (21.09.2026, Kaisers Wahl): Der alte Text "ungesunder Abverkauf (der Dip wird
    # nicht gekauft)" klang nach Warnung - gemessen folgte auf diese Lage aber meist eine
    # Gegenbewegung nach OBEN. Jetzt steht da, WAS passiert, ohne Wertung. "haelt oder
    # steigt" statt nur "steigt": classify_pattern verlangt oi_chg >= -1 %, nicht mehr.
    "UNGESUNDER_ABVERKAUF": ("Abverkauf mit neuen Short-Wetten - Kurs faellt, Spot wird "
                             "verkauft, Open Interest haelt oder steigt, noch keine "
                             "Liquidationswelle"),
    "NEUTRAL": "neutral",
}

# E38 (21.09.2026): Was nach einem Muster GEMESSEN wurde - in Worten, ohne Bruchzahlen
# (Kaiser: "20 von 26 Faellen" ist schlecht). Nur dort, wo es eine Messung gibt; die
# anderen Muster bekommen keinen Hinweis, statt einen erfundenen.
#   Der Zeitraum steht BEWUSST fest im Text: Die Messung ist eine Momentaufnahme
# (E38.1, Fenster 13.01.-21.09.2026) und wird nicht laufend nachgerechnet. "Seit Januar"
# wuerde in einem Jahr etwas behaupten, das niemand geprueft hat.
#   Grundlage (je Episode, 26 Faelle): 1 Tag 77 %, 2 Tage 69 %, 4 Tage 58 % hoeher;
# Median +1,2 Punkte ueber dem Fensterdurchschnitt. "meist" und "nicht immer" sind
# beide wahr - das zweite ist der Grund, warum die Ampel das Muster NEUTRAL zaehlt.
MUSTER_HINWEIS = {
    "UNGESUNDER_ABVERKAUF": ("Jan-Sep 2026 folgte darauf meist eine Gegenbewegung nach "
                             "oben (1-4 Tage) - nicht immer."),
}

SPOT_FENSTER = 3          # Kerzen je Vergleichsfenster (3 x 4h = 12 Stunden)


# --- E36: Furkans Rohwerte -------------------------------------------------------
# Kaiser 19.09.2026: "Koennen wir die Indikatoren aus seinem Video fuer diesen manuellen
# Button freischalten?" - Die Engine holt diese Groessen laengst und rechnet daraus die
# Muster; gezeigt wurde bisher nur das Ergebnis. Der Abruf handelt nicht, also kostet
# eine Zahl dort nichts (docs/PLAN-E36-ORDERFLOW-ROHWERTE.md).

OF_FENSTER = 12        # Kerzen - dasselbe Fenster wie classify_pattern (2 Tage)
# Anteil der typischen Fensterbewegung, unter dem eine Groesse als "flach" gilt.
# SETZUNG, keine Messung: Furkan sagt "flach" nach Augenmass. Der Median der
# vergangenen Fensteraenderungen als Massstab kalibriert sich selbst und kommt ohne
# eine USD-Zahl aus, die bei anderem Kursniveau falsch waere.
OF_FLACH_ANTEIL = 1.0 / 3.0


def _median(werte: list[float]) -> float:
    s = sorted(werte)
    n = len(s)
    if not n:
        return 0.0
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def _of_richtung(aenderung: float, verlauf: list[float]) -> str:
    """steigt / faellt / flach - gemessen an der typischen Bewegung dieser Groesse."""
    massstab = _median([abs(v) for v in verlauf]) if verlauf else 0.0
    if massstab > 0 and abs(aenderung) < massstab * OF_FLACH_ANTEIL:
        return "flach"
    if aenderung > 0:
        return "steigt"
    if aenderung < 0:
        return "faellt"
    return "flach"


def _of_reihe(werte: list[float], fenster: int) -> Optional[dict]:
    """Aenderung einer KUMULIERTEN Reihe (CVD, OI) im letzten Fenster + Richtung.

    Gibt None, wenn die Reihe durchgehend 0 ist: Dann liegen keine Daten vor (ohne
    Coinalyze-Schluessel bleiben fut_cvd und long_pct bei 0). Eine Zeile
    "Futures-CVD 0 $ - flach" waere in dem Fall FALSCH - sie behauptet Stillstand,
    wo in Wahrheit nichts bekannt ist.
    """
    if len(werte) < fenster + 1 or all(v == 0 for v in werte):
        return None
    jetzt = werte[-1] - werte[-1 - fenster]
    # Massstab: die frueheren Fensteraenderungen derselben Groesse
    verlauf = [werte[i] - werte[i - fenster]
               for i in range(fenster, len(werte) - fenster)]
    return {"aenderung": jetzt, "richtung": _of_richtung(jetzt, verlauf)}


def spot_nachfrage(flow: list[FlowPoint], fenster: int = SPOT_FENSTER) -> Optional[dict]:
    """Wie steht die Spot-Nachfrage? (E32, Kaiser 12.09.2026)

    Furkan im Video vom 10.09.2026 (17:22): "auch Spot ist verkauft worden in dieser
    Bewegung rein. Was wir jetzt sehen wollen, ist bei einer Gegenbewegung nach oben,
    dass wieder die Spot Nachfrage kommt und nicht nur ein Short Liquidierungsevent
    stattfindet."

    spot_cvd ist ein KUMULIERTES Delta - die Differenz ueber ein Fenster ist damit die
    Netto-Nachfrage in diesem Fenster. Verglichen wird das juengste Fenster mit dem
    davor. Daraus die vier Zustaende, die Kaiser lesen will.

    Gibt None zurueck, wenn zu wenige Punkte vorliegen - dann steht in der Nachricht
    nichts, statt etwas Erfundenes.
    """
    if len(flow) < 2 * fenster + 1 or not all(
            usable(p, "spot_cvd") for p in flow[-2 * fenster - 1:]):
        return None
    werte = [p.spot_cvd for p in flow]
    jetzt = werte[-1] - werte[-1 - fenster]
    davor = werte[-1 - fenster] - werte[-1 - 2 * fenster]
    if jetzt > 0:
        stand = "stabil" if davor > 0 else "zurueckgekehrt"
    else:
        stand = "nachgelassen" if davor > 0 else "schwach"
    text = {
        "stabil": "Spot-Nachfrage stabil (Nachfrage traegt)",
        "zurueckgekehrt": "Spot-Nachfrage zurueckgekehrt (Kaeufer sind zurueck)",
        "nachgelassen": "Spot-Nachfrage nachgelassen (die Nachfrage hat gedreht)",
        "schwach": "Spot-Nachfrage schwach (Verkaufsdruck haelt an)",
    }[stand]
    return {"stand": stand, "text": text, "jetzt": jetzt, "davor": davor,
            "fenster": fenster}


def _p(v: float) -> str:
    """Preis fuer die Lage-Texte: deutsche Tausenderpunkte, keine Nachkommastellen."""
    return f"{v:,.0f}".replace(",", ".") + " $"


def _usd_kurz(v: float, vorzeichen: bool = True) -> str:
    """USD-Betrag kompakt: 12.400.000 -> '12,4 Mio $'.

    `vorzeichen=False` fuer reine Summen (Liquidationen): Die sind immer positiv,
    ein "+" davor sieht dort nach einer Veraenderung aus, die es nicht gibt.
    """
    vz = ("+" if v > 0 else ("-" if v < 0 else "")) if vorzeichen else ""
    a = abs(v)
    if a >= 1e9:
        return f"{vz}{a / 1e9:.1f} Mrd $".replace(".", ",")
    if a >= 1e6:
        return f"{vz}{a / 1e6:.1f} Mio $".replace(".", ",")
    if a >= 1e3:
        return f"{vz}{a / 1e3:.0f} Tsd $"
    return f"{vz}{a:.0f} $"


def _fut_cvd_usd(candles: list[Candle], flow: list[FlowPoint]) -> list[float]:
    """Das kumulierte Futures-Delta in DOLLAR (E43.1).

    `FlowPoint.fut_cvd` ist in BTC (Coinalyze `ohlcv-history` liefert die Einheit des
    Marktes und ignoriert `convert_to_usd`). Jedes Kerzen-Delta wird mit dem
    Schlusskurs DERSELBEN Kerze umgerechnet und erst dann aufsummiert - ein einziger
    Kurs fuer die ganze Reihe waere bei 10 % Kursbewegung im Fenster 10 % daneben.

    Gibt [] zurueck, wenn zu einem Flow-Punkt keine Kerze existiert: lieber keine Zeile
    als eine mit falschem Kurs gerechnete. Reine Anzeige - `classify_pattern` liest
    weiterhin `fut_cvd` in BTC.
    """
    kurs = {c.ts: c.close for c in candles}
    out: list[float] = []
    summe = vorher = 0.0
    for p in flow:
        k = kurs.get(p.ts)
        if k is None:
            return []
        summe += (p.fut_cvd - vorher) * k
        vorher = p.fut_cvd
        out.append(summe)
    return out


def orderflow_detail(candles: list[Candle], flow: list[FlowPoint],
                     fenster: int = OF_FENSTER) -> list[dict]:
    """Die Rohwerte, die Furkan im Video abliest - als Liste von Zeilen (E36).

    REINE ANZEIGE. Diese Funktion wird von `evaluate()` nicht aufgerufen und kann das
    Handelsverhalten nicht beeinflussen.

    Furkans Leseweise (Transkript 8:05-11:26): Spot-CVD ist "echte Nachfrage ... ohne
    Hebel", Futures-CVD der "gehebelte Flow". Steigt Spot und bleibt Futures flach, ist
    die Bewegung gesund; treibt Futures allein, ist sie "anfaellig fuer einen Long
    Flush". Open Interest sagt, ob NEUES GELD hereinkommt (steigt) oder ob Positionen
    zwangsgeschlossen werden (faellt). Funding zeigt Ueberhebelung.

    Jede Zeile: {name, wert (Klartext), richtung, hinweis}. Groessen ohne Daten fehlen
    ganz, statt als 0 zu erscheinen.
    """
    if len(flow) < fenster + 1 or len(candles) < fenster + 1:
        return []
    zeilen: list[dict] = []

    # --- Preis: Furkans Regeln lauten immer "Preis X UND Open Interest Y"
    p_jetzt, p_davor = candles[-1].close, candles[-1 - fenster].close
    p_pct = (p_jetzt - p_davor) / p_davor * 100 if p_davor else 0.0
    zeilen.append({
        "name": "Preis", "wert": f"{p_pct:+.1f} %".replace(".", ","),
        "richtung": "steigt" if p_pct > 0 else ("faellt" if p_pct < 0 else "flach"),
        "hinweis": "",
    })

    # --- Spot-CVD: die echte Nachfrage
    sp = (_of_reihe([p.spot_cvd for p in flow], fenster)
          if all(usable(p, "spot_cvd") for p in flow[-fenster-1:]) else None)
    if sp:
        zeilen.append({"name": "Spot-CVD", "wert": _usd_kurz(sp["aenderung"]),
                       "richtung": sp["richtung"],
                       "hinweis": "echte Nachfrage, ohne Hebel"})
        # E36.2 (19.09.2026, Kaisers Fund): Die Lage-Zeile "Spot-Nachfrage ..."
        # rechnet mit SPOT_FENSTER (12 h), dieser Block mit OF_FENSTER (48 h).
        # In der Nachricht standen dadurch zwei Zahlen unter fast demselben Namen,
        # die sich zu widersprechen schienen ("steigt" gegen "nachgelassen") -
        # beides richtig, nur ueber verschiedene Zeitraeume. Die kurze Ebene steht
        # jetzt daneben: Der Unterschied IST die Information (Tempoverlust).
        kurz = _of_reihe([p.spot_cvd for p in flow], SPOT_FENSTER)
        if kurz:
            # Richtung hier nach dem VORZEICHEN, nicht nach dem Massstab: Diese Zeile
            # existiert nur wegen der Drehung, und ein Vorzeichenwechsel ist die
            # Aussage. Nach Massstab gerechnet hiesse -11 Mio nach +48 Mio "flach" -
            # richtig gerechnet, aber am Punkt vorbei.
            vz = ("steigt" if kurz["aenderung"] > 0
                  else "faellt" if kurz["aenderung"] < 0 else "flach")
            lang_vz = ("steigt" if sp["aenderung"] > 0
                       else "faellt" if sp["aenderung"] < 0 else "flach")
            if vz != lang_vz:
                zeilen.append({
                    "name": f"... letzte {SPOT_FENSTER * 4} h",
                    "wert": _usd_kurz(kurz["aenderung"]),
                    "richtung": vz, "hinweis": "zuletzt gedreht",
                })

    # --- Futures-CVD: der gehebelte Flow
    # E43.1 (Gesamtpruefung 26.09.2026, Befund A1): fut_cvd kommt von Coinalyze in BTC,
    # spot_cvd in Dollar. Bis dahin stand der BTC-Wert hier mit "$" - um den Faktor
    # Kurs zu klein -, und der Anteil am Spot-Flow teilte BTC durch Dollar. Deshalb
    # jetzt erst in Dollar umrechnen (_fut_cvd_usd), dann vergleichen.
    fu = (_of_reihe(_fut_cvd_usd(candles, flow), fenster)
          if all(usable(p, "fut_cvd") for p in flow[-fenster-1:]) else None)
    if fu:
        # E36.2: Die Groessenordnung ist die eigentliche Aussage - das Verhaeltnis zum
        # Spot sagt, ob der Hebel die Bewegung traegt (Transkript 9:00-9:31).
        # KORREKTUR E43.1: Das Beispiel, mit dem E36.2 begruendet wurde ("+7 Tsd $"
        # neben "+177,7 Mio $" = der Hebel spielt keine Rolle), war der Einheitenfehler
        # selbst: 7.000 BTC waren rund 540 Mio $, also etwa das Dreifache des Spot-Flows.
        hinweis = "gehebelter Flow, oft kurzfristig"
        if sp and sp["aenderung"]:
            anteil = abs(fu["aenderung"]) / abs(sp["aenderung"]) * 100
            if anteil < 0.5:
                # "0,0 % des Spot-Flows" sieht nach einem Rechenfehler aus. Gemeint
                # ist: der Hebel spielt keine Rolle - Furkans "gesunder Trend".
                hinweis = "verschwindend gegen den Spot"
            elif anteil < 10:
                hinweis = f"nur {anteil:.1f} % des Spot-Flows".replace(".", ",")
            else:
                hinweis = f"{anteil:.0f} % des Spot-Flows"
        zeilen.append({"name": "Futures-CVD", "wert": _usd_kurz(fu["aenderung"]),
                       "richtung": fu["richtung"], "hinweis": hinweis})

    # --- Open Interest: kommt neues Geld herein?
    # E43.4b (Befund A3 in der Anzeige): Das Dollar-OI ist Kontrakte x Kurs. Faellt der
    # Kurs um 5 % und niemand schliesst eine Position, fiel es trotzdem um 5 % - und hier
    # stand "Positionen werden geschlossen". Deshalb folgen Richtung und Hinweis jetzt
    # den KONTRAKTEN (oi_btc, dieselbe Reihe wie muster_oi in E43.4). Der Dollar-Wert
    # bleibt stehen; zeigen beide in verschiedene Richtungen, sagt der Hinweis das dazu.
    # Ohne Kontrakt-Reihe (Kraken-Rueckfall) bleibt alles wie vorher.
    oi = (_of_reihe([p.oi for p in flow], fenster)
          if all(usable(p, "oi") for p in flow[-fenster-1:]) else None)
    if oi:
        oi_davor = flow[-1 - fenster].oi
        pct = (oi["aenderung"] / oi_davor * 100) if oi_davor else 0.0
        pct_txt = f"{pct:+.1f}".replace(".", ",")
        wert = f"{_usd_kurz(oi['aenderung'])} ({pct_txt} %)"
        richtung = oi["richtung"]
        kt = (_of_reihe([p.oi_btc for p in flow], fenster)
              if all(usable(p, "oi_btc") for p in flow[-fenster-1:]) else None)
        kt_davor = flow[-1 - fenster].oi_btc
        if kt and kt_davor:
            kt_pct = kt["aenderung"] / kt_davor * 100
            wert += f", Kontrakte {kt_pct:+.1f} %".replace(".", ",")
            richtung = kt["richtung"]
        if richtung == "steigt":
            hin = "neues Geld kommt herein"
        elif richtung == "faellt":
            hin = "Positionen werden geschlossen"
        else:
            hin = "unveraendert"
        if richtung != oi["richtung"]:
            hin += {"steigt": " - der Dollar-Anstieg kommt nur vom Kurs",
                    "faellt": " - der Dollar-Rueckgang kommt nur vom Kurs"}.get(
                        oi["richtung"], " - in Dollar vom Kurs verdeckt")
        zeilen.append({"name": "Open Interest", "wert": wert,
                       "richtung": richtung, "hinweis": hin})

    # --- Funding: Ueberhebelung. Kein kumulierter Wert - der Stand zaehlt.
    fund = [p.funding for p in flow[-fenster:] if usable(p, "funding")]
    if usable(flow[-1], "funding") and fund:
        jetzt = fund[-1]
        mittel = sum(fund) / len(fund)
        zeilen.append({
            "name": "Funding", "wert": f"{jetzt * 100:+.4f} %".replace(".", ","),
            "richtung": "steigt" if jetzt > mittel else (
                "faellt" if jetzt < mittel else "flach"),
            "hinweis": ("Longueberhang" if jetzt > 0 else
                        "Shortueberhang" if jetzt < 0 else "neutral"),
        })

    # --- Liquidationen: Summe im Fenster, je Seite
    ll = sum(p.long_liq for p in flow[-fenster:])
    sl = sum(p.short_liq for p in flow[-fenster:])
    if (ll or sl) and all(usable(p, "long_liq") and usable(p, "short_liq")
                          for p in flow[-fenster:]):
        zeilen.append({"name": "Long-Liquidationen",
                       "wert": _usd_kurz(ll, vorzeichen=False),
                       "richtung": "", "hinweis": ""})
        zeilen.append({"name": "Short-Liquidationen",
                       "wert": _usd_kurz(sl, vorzeichen=False),
                       "richtung": "", "hinweis": ""})

    # --- Positionierung: 0.0 heisst laut FlowPoint ausdruecklich "keine Daten"
    lp = flow[-1].long_pct
    if lp and usable(flow[-1], "long_pct"):
        zeilen.append({
            "name": "Positionierung", "wert": f"{lp:.0f} % long",
            "richtung": "",
            "hinweis": "mehrheitlich long" if lp > 50 else "mehrheitlich short",
        })
    return zeilen


def lage_bericht(candles: list[Candle], flow: list[FlowPoint],
                 imp: Optional[Impulse] = None,
                 pos_impulse: Optional[Impulse] = None,
                 pattern: Optional[Pattern] = None,
                 fenster: int = SPOT_FENSTER,
                 trend_period: int = 0) -> dict:
    """Der Marktzustand in Klartext - REINE INFORMATION, keine Handelsregel (E32).

    Anlass (Kaiser 12.09.2026): "ich bekomme die info zur struktur nur, wenn ich eine
    nachricht fuer ein nachkauf erhalte. doch das ist zu spaet, weil ich doch die limits
    vorher setze." Die Engine wertet den kompletten Order-Flow aus und behielt das
    Ergebnis bisher fuer sich.

    Zwei GETRENNTE Aussagen, weil Furkan sie getrennt haelt:
      - Struktur intakt?  -> am PREIS (hoeheres Tief und hoeheres Hoch, wie trend_intakt)
      - Bewegung gesund?  -> am ORDER-FLOW (Spot-Nachfrage)

    Dieser Bericht aendert NICHTS am Verhalten der Engine. Er wird nirgends abgefragt,
    um ein Signal zu erzeugen oder zu unterdruecken.
    """
    lage: dict = {}

    # --- Uebergeordneter Trend (E33-B): steht ZUERST, weil er den Rahmen setzt.
    # Furkans Reihenfolge (Transkript 19:16): "uebergeordnet erstmal ein Bias ... dann
    # gehe ich rein, schaue mir vor allem die Orderflow Daten an".
    if trend_period:
        _tl = trend_lage(candles, trend_period)
        if _tl is not None:
            lage["trend"] = _tl["stand"]
            lage["trend_text"] = _tl["text"]

    # --- Struktur (Preis)
    if imp is not None:
        if pos_impulse is not None:
            if (imp.start.ts, imp.end.ts) == (pos_impulse.start.ts, pos_impulse.end.ts):
                lage["struktur"] = "unveraendert"
                lage["struktur_text"] = (
                    f"Struktur unveraendert - Bein {_p(pos_impulse.start.price)} -> "
                    f"{_p(pos_impulse.end.price)}")
            elif trend_intakt(pos_impulse, imp):
                hoch_tief = "hoeheres" if imp.up else "tieferes"
                lage["struktur"] = "intakt"
                lage["struktur_text"] = (
                    f"Struktur intakt - {hoch_tief} Tief {_p(imp.start.price)}, "
                    f"{hoch_tief} Hoch {_p(imp.end.price)}")
            else:
                lage["struktur"] = "gebrochen"
                lage["struktur_text"] = (
                    f"Struktur gebrochen - neues Bein {_p(imp.start.price)} -> "
                    f"{_p(imp.end.price)} setzt den Trend nicht fort")
        else:
            lage["struktur"] = "neu"
            lage["struktur_text"] = (
                f"Aktuelles Bein {_p(imp.start.price)} -> {_p(imp.end.price)}")

    # --- Spot-Nachfrage (Order-Flow)
    sn = spot_nachfrage(flow, fenster)
    if sn is not None:
        lage["spot"] = sn["stand"]
        lage["spot_text"] = sn["text"]

    # --- Muster
    if pattern is not None and pattern != Pattern.NEUTRAL:
        lage["muster"] = pattern.name
        lage["muster_text"] = MUSTER_KLARTEXT.get(pattern.name, pattern.name)
        if pattern.name in MUSTER_HINWEIS:
            lage["muster_hinweis"] = MUSTER_HINWEIS[pattern.name]

    return lage


# --- E34: die Ampel --------------------------------------------------------------
# Kaiser 13.09.2026: "Ich brauche einen genauen Plan, wonach ich handele, ohne selbst
# entscheiden zu muessen." Die vier Lage-Angaben sind richtig, verlangen aber eine
# Abwaegung. Die Ampel nimmt die Abwaegung ab - und NUR die. Sie schreibt keine
# Handlung vor (docs/PLAN-E34-AMPEL.md).

# Je Kriterium: welche Werte sprechen DAFUER, dass die LONG-Richtung weiter traegt.
# Alles, was weder hier noch in _AMPEL_DAGEGEN steht, gilt als "keine Aussage" -
# Schweigen ist nie ein Gegenargument.
_AMPEL_DAFUER = {
    "trend":    {"ueber"},
    "struktur": {"intakt", "unveraendert"},
    "spot":     {"stabil", "zurueckgekehrt"},
    # Kapitulation ist in Furkans Methode kein Warnzeichen, sondern die Einstiegslage -
    # deshalb steht live flush_entry="core". Short-Covering dagegen ist eine Aufwaerts-
    # bewegung OHNE echte Nachfrage und spricht nicht fuer einen Long.
    "muster":   {"GESUNDER_TREND", "CAPITULATION_RESET"},
}
_AMPEL_DAGEGEN = {
    "trend":    {"unter"},
    "struktur": {"gebrochen"},
    "spot":     {"nachgelassen", "schwach"},
    # UNGESUNDER_ABVERKAUF steht seit E38 (21.09.2026, Kaisers Wahl) NICHT mehr hier.
    # Die Ampel zaehlte es gegen den Long - gemessen folgte darauf aber meist eine
    # Gegenbewegung nach oben. Bewusst NEUTRAL statt "dafuer": Kursverlauf ist nicht
    # Ertrag (derselbe Lauf zeigte Muster 4 mit dem schlechtesten Nachlauf und trotzdem
    # profitabel), und 26 Ereignisse sind wenig. Neutral heisst: es zaehlt fuer keine
    # Seite, auch nicht fuer einen Short.
    "muster":   {"DERIVATE_PUMP", "SHORT_COVERING"},
}
# Kriterien, die NICHT gespiegelt werden, weil sie schon in der Richtung der Position
# sprechen. "Struktur intakt" kommt aus trend_intakt() und heisst: das Bein der Position
# setzt sich fort - bei einem Short also tieferes Tief und tieferes Hoch. Das spricht
# FUER den Short. Trend, Spot-Nachfrage und Muster sind dagegen absolut (steigend /
# fallend) und kehren sich bei einem Short um.
_AMPEL_RELATIV = {"struktur"}
AMPEL_NAMEN = {"trend": "Trend", "struktur": "Struktur",
               "spot": "Spot-Nachfrage", "muster": "Muster"}
AMPEL_REIHENFOLGE = ("trend", "struktur", "spot", "muster")
AMPEL_SCHLUSSSATZ = ("Der Plan oben bleibt unveraendert. Die Engine handelt die Lage "
                     "NICHT — die Ampel ist eine Beobachtung, keine Anweisung.")


def ampel_richtung(bias_long: bool, bias_short: bool, bein_auf: Optional[bool]) -> bool:
    """Fuer WELCHE Richtung die Ampel rechnet (E35).

    Der Fehler, den das behebt (17.09.2026): `zonen_vorschau` haengte die Ampel an
    `imp.up` - die Richtung des gefundenen Beins. Live steht `bias_short: false`, die
    Engine ist also reine Long-Engine; sobald sie aber ein ABWAERTS-Bein fand (bei
    `bein_richtung: "auto"` der Normalfall), rechnete die Ampel fuer einen Short, den
    sie nie eingehen wuerde. Kaiser las am 17.09. "UNGUENSTIG - 0 von 2", waehrend
    dieselben Daten fuer seine Long-Position "GUENSTIG - 2 von 2" ergaben.

    Keine falsche Zahl, sondern eine richtige mit verkehrtem Vorzeichen - der
    gefaehrlichste Fehlertyp, den dieses Projekt bisher hatte.

    Regel: Ist genau EINE Richtung erlaubt, gilt sie. Sind beide erlaubt, entscheidet
    das Bein (dann kann die Engine beides, und das Bein ist die beste Auskunft).
    Fehlt auch das Bein, wird Long angenommen - die Grundeinstellung des Projekts.
    """
    if bias_long != bias_short:
        return bias_long
    return True if bein_auf is None else bein_auf


def ampel(lage: dict, long_side: bool = True) -> Optional[dict]:
    """Fasst die vier Lage-Angaben zu EINER Aussage zusammen (E34).

    Jedes Kriterium sagt dafuer, dagegen oder nichts; die einfache Mehrheit unter
    denen, die etwas sagen, ergibt die Stufe. Alle vier zaehlen gleich viel - eine
    Setzung, keine Messung, im Bauplan offen als solche benannt.

    Bei einer SHORT-Position kehrt sich jede Zeile um: was fuer einen Long spricht,
    spricht gegen einen Short. Live steht bias_short=false; die Seite existiert, damit
    die Ampel nicht Unsinn sagt, falls sie je eingeschaltet wird.

    Liefert None, wenn weniger als zwei Kriterien etwas sagen - lieber keine Aussage
    als eine aus einem einzigen Datenpunkt.
    """
    if not lage:
        return None
    dafuer, dagegen = [], []
    for feld in AMPEL_REIHENFOLGE:
        wert = lage.get(feld)
        if wert is None:
            continue
        spiegeln = not long_side and feld not in _AMPEL_RELATIV
        if wert in _AMPEL_DAFUER[feld]:
            (dagegen if spiegeln else dafuer).append(AMPEL_NAMEN[feld])
        elif wert in _AMPEL_DAGEGEN[feld]:
            (dafuer if spiegeln else dagegen).append(AMPEL_NAMEN[feld])
    gezaehlt = len(dafuer) + len(dagegen)
    if gezaehlt < 2:
        return None
    if len(dafuer) > len(dagegen):
        stufe = "guenstig"
    elif len(dagegen) > len(dafuer):
        stufe = "unguenstig"
    else:
        stufe = "gemischt"
    return {
        "stufe": stufe,
        # E35: Die Richtung gehoert INS ERGEBNIS, nicht nur in den Aufruf. Wer die
        # Ampel liest, muss sehen, wofuer sie gilt - dieselbe Lage ergibt fuer Long und
        # Short das genaue Gegenteil.
        "richtung": "LONG" if long_side else "SHORT",
        "dafuer": dafuer,
        "dagegen": dagegen,
        "gezaehlt": gezaehlt,
        "text": f"{stufe.upper()} — {len(dafuer)} von {gezaehlt} spricht dafuer"
                if len(dafuer) == 1 else
                f"{stufe.upper()} — {len(dafuer)} von {gezaehlt} sprechen dafuer",
    }


def classify_pattern(candles: list[Candle], flow: list[FlowPoint],
                     window: int = 12,
                     oi_wipeout_pct: float = 0.05,
                     sharp_move_pct: float = 0.04,
                     funding_hot: float = 0.0001,
                     liq_spike_mult: float = 3.0,
                     muster_cvd: str = "usd",
                     muster_oi: str = "usd") -> Pattern:
    """Ordnet die juengste Marktphase einem der 4 Kompass-Muster zu.

    muster_cvd (A2-Default "usd"): "usd" ersetzt in Muster 2
    den Vergleich zweier relativer Slopes durch einen Vergleich zweier Dollar-Betraege
    im Fenster (_muster2_dollar). Nur Muster 2 aendert sich - die Vorzeichen-Pruefungen
    der anderen Muster haengen nicht vom Startwert der Summe ab.

    muster_oi (E43.4, Default "usd" = bisheriges Verhalten): "btc" misst die
    OI-Veraenderung in Kontrakten statt in Dollar (oi_aenderung). Das wirkt in ALLEN
    Mustern, die das OI lesen (1 bis 5) - es gibt nur ein oi_chg, und zwei Einheiten in
    derselben Einordnung waeren schlimmer als eine falsche. Die Schwellen bleiben.

    `window` = Anzahl Kerzen (12 x 4h = 2 Tage). Schwellen sind Startwerte.
    E9.1: echte Liquidationen (Coinalyze) verstaerken Muster 3/4 — eine Long-Liq-
    Kaskade belegt die Kapitulation direkt (Muster 4), eine Short-Liq-Kaskade das
    Short-Covering (Muster 3). Rueckwaertskompatibel: ohne Liq-Daten (=0) gilt die
    bisherige OI-Proxy-Logik.
    """
    if len(candles) < window or len(flow) < window:
        return Pattern.NEUTRAL
    c, f = candles[-window:], flow[-window:]
    price_chg = (c[-1].close - c[0].close) / c[0].close
    spot_valid = all(usable(p, "spot_cvd") for p in f)
    spot = ((f[-1].spot_cvd - f[0].spot_cvd) if muster_cvd == "usd" else
            _slope([p.spot_cvd for p in f])) if spot_valid else 0.0
    fut = _slope([p.fut_cvd for p in f])
    # E43.4 (Befund A3): In Dollar steckt die Kursbewegung im OI - bei +3 % Kurs erfuellt
    # schon ein unveraendertes OI die Pump-Schwelle. "btc" zaehlt Kontrakte.
    oi_chg = oi_aenderung(f, muster_oi)
    oi_valid = usable(f[0], "oi_btc" if muster_oi == "btc" else "oi") and usable(
        f[-1], "oi_btc" if muster_oi == "btc" else "oi") and (
            f[0].oi_btc > 0 and f[-1].oi_btc > 0 if muster_oi == "btc" else
            f[0].oi > 0 and f[-1].oi > 0)
    funding_valid = usable(f[-1], "funding")
    funding_now = f[-1].funding if funding_valid else 0.0
    funding_rising = (funding_valid and usable(f[0], "funding")
                      and f[-1].funding > f[0].funding)

    def _liq_spike(get, field_name) -> bool:
        if not all(usable(p, field_name) for p in f):
            return False
        vals = [get(p) for p in f]
        base = sum(vals[:-1]) / (len(vals) - 1) if len(vals) > 1 else 0.0
        return base > 0 and vals[-1] >= liq_spike_mult * base
    long_liq_spike = _liq_spike(lambda p: p.long_liq, "long_liq")
    short_liq_spike = _liq_spike(lambda p: p.short_liq, "short_liq")

    # 4: Capitulation/Flush + Reset — Preis scharf runter, Spot-CVD dreht,
    #    dazu OI-Wipeout ODER echte Long-Liquidations-Kaskade
    if price_chg <= -sharp_move_pct:
        spot_turning = (spot_valid and len(flow) >= 3
                        and flow[-1].spot_cvd > flow[-3].spot_cvd)
        if spot_turning and ((oi_valid and oi_chg <= -oi_wipeout_pct) or long_liq_spike):
            return Pattern.CAPITULATION_RESET
    # 5: Ungesunder Abverkauf (E13) — das Spiegelbild von Muster 4. Der Kurs faellt, aber
    #    der Markt ist NICHT ausgeraeumt: Spot-CVD faellt mit (der Dip wird nicht gekauft),
    #    OI haelt oder steigt (die gehebelten Longs sind noch drin und laden nach),
    #    Funding noch positiv (Long-Ueberhang unveraendert), keine Long-Liquidations-
    #    Kaskade (die Zwangsverkaeufe stehen noch bevor). Alle vier zusammen = Furkans
    #    Konfluenz-Prinzip, nur negativ: genau die Lage, in der er NICHT kauft.
    #    Halbe Schwelle beim Preis, weil dieser Zustand typischerweise VOR dem scharfen
    #    Einbruch vorliegt — er soll warnen, bevor der Flush kommt, nicht danach.
    if (price_chg <= -sharp_move_pct / 2 and spot_valid and spot < 0
            and oi_valid and oi_chg >= -0.01 and funding_valid
            and funding_now > 0 and not long_liq_spike
            and liq_evidence.known(f, ('long_liq',), 'suppressed_missing_m5_no_long_cascade',
                                   'long', f[-1].ts + FOUR_HOURS_MS)):
        return Pattern.UNGESUNDER_ABVERKAUF
    # 3: Short-Covering — Preis hoch, OI runter ODER echte Short-Liquidations-Kaskade
    if price_chg >= sharp_move_pct / 2 and ((oi_valid and oi_chg <= -0.02)
                                            or short_liq_spike):
        return Pattern.SHORT_COVERING
    # 2: Derivate-Pump — Futures-CVD stark hoch, Spot flach/runter, OI deutlich hoch, Funding zieht an
    has_fut = all(usable(p, "fut_cvd") for p in f) and (
        any(b.fut_cvd != a.fut_cvd for a, b in zip(f, f[1:]))
        if muster_cvd == "usd" else any(p.fut_cvd for p in f))
    if has_fut:
        if muster_cvd == "usd":
            # E43.3: zwei Dollar-Betraege im Fenster statt zweier Anteile an einer
            # willkuerlich begonnenen Summe. "Futures steigt" ebenfalls in Dollar,
            # damit beide Seiten des Vergleichs dieselbe Einheit haben.
            d = _muster2_dollar(c, f)
            cvd_pump = spot_valid and d is not None and d[1] > 0 and d[0] <= d[1] / 3
        else:
            cvd_pump = fut > 0 and spot <= fut / 3
        if (price_chg > 0 and cvd_pump and oi_valid and oi_chg >= 0.03
                and (funding_rising or (funding_valid and funding_now >= funding_hot))):
            return Pattern.DERIVATE_PUMP
    else:
        # Ohne Futures-CVD-Quelle (US-Geo-Block): Pump-Erkennung ueber die uebrigen
        # Merkmale aus Furkans Notizen — OI deutlich hoch, Funding zieht an, Spot flach
        if (price_chg > 0 and oi_valid and oi_chg >= 0.03 and spot_valid
                and spot <= (0 if muster_cvd == "usd" else 0.01) and (funding_rising or
                                       (funding_valid and funding_now >= funding_hot))):
            return Pattern.DERIVATE_PUMP
    # 1: Gesunder Trend — Preis hoch, Spot-CVD traegt, Funding unauffaellig
    if (price_chg > 0 and spot_valid and spot > 0 and funding_valid
            and abs(funding_now) < funding_hot and oi_valid and 0 <= oi_chg <= 0.10):
        return Pattern.GESUNDER_TREND
    return Pattern.NEUTRAL


# --------------------------------------------------- Zustandsmaschine


class PosState(Enum):
    FLAT = "FLAT"
    T1 = "T1"        # 25 % (0.5-Level)
    CORE = "CORE"    # 75 % (Golden Pocket)
    FULL = "FULL"    # 100 % (0.786-Nachkauf)
    TP1 = "TP1"      # nach Teilverkauf 1
    TP2 = "TP2"      # nach Teilverkauf 2


@dataclass
class Position:
    direction: str = "NONE"          # "LONG" | "SHORT" | "NONE"
    state: PosState = PosState.FLAT
    zones: Optional[FibZones] = None
    retrace_extreme: Optional[float] = None  # tiefster/hoechster Punkt der Korrektur
    last_signal_ts: int = -1                 # Dedupe: nur 1 Signal-Batch je Kerze
    tp_rungs: int = 0                        # Anzahl gefeuerter Leiter-Zwischenverkaeufe
    dip_buys: int = 0                        # Anzahl bedingter Nachkaeufe unter Invalidierung (E9.3)
    buy_rungs: int = 0                       # Anzahl Mehrtages-Kaufleiter-Tranchen (E9.5)
    entry_ref: Optional[float] = None        # Signalanker; V1: Restkosten/BTC des Basisteils
    # Stage 4: only V1 fill feedback supplies cost-bearing inventory. The live
    # observer remains an explicitly labelled signal reference, never broker BTC.
    inventory_source: str = "signal_reference"
    lots: list = field(default_factory=list)
    cost_basis_complete: bool = True
    migration_notes: list = field(default_factory=list)
    valid_stop: Optional[float] = None
    valid_stop_reason: str = ""
    entry_pct: int = 0                       # Summe der eingestiegenen Tranchen-Prozente
    liq_exits: int = 0                       # Anzahl Teilverkaeufe an Liquidationen (E9.11)
    high_exits: int = 0                      # Anzahl Teilverkaeufe am letzten Hoch (E10.2)
    liq_entries: int = 0                     # Anzahl Konfluenz-Nachkaeufe an Liq-Zonen (E10.3)
    ziel_extrem: Optional[float] = None      # eingefrorene Zielreferenz nach dem 1. Teilgewinn (E18.3)
    be_aktiv: bool = False                   # Break-even-Stop scharf, seit die Position im Plus war (E19.3)
    widerstand_exits: int = 0                # Anzahl Teilverkaeufe an Widerstaenden des Gegen-Beins (E20)
    # E41 (Kaisers Rueckeroberungs-Regel): wie viele Kerzen in Folge unter der
    # Invalidierung geschlossen haben, ohne dass gestoppt wurde - und an WELCHER Marke.
    stop_wartet: int = 0
    stop_wartet_inv: Optional[float] = None
    # Die Invalidierung, die schon einmal unterschritten UND zurueckerobert wurde. Sie
    # gilt als geprueft: der naechste Schluss darunter ist ein echter Bruch.
    stop_geprueft: Optional[float] = None
    # Zeitpunkt des letzten Stops (E13, cooldown_h). Gehoert BEWUSST NICHT in
    # _reset_position: Er ist die Erinnerung ZWISCHEN zwei Positionen und muss den
    # Positions-Reset ueberleben, sonst wuesste die Sperre nach dem Stop nichts mehr.
    last_stop_ts: int = -1
    # --- E44.3 (E42, Ausbruch mit Ruecktest), siehe ruecktest_schritt() -------------
    # Die Beobachtung einer Marke. Gehoert BEWUSST NICHT in _reset_position: Sie beginnt
    # gerade nach einem Rest-Verkauf, also im Moment des Resets. Beendet wird sie durch
    # Rueckkauf, Stop, abgelaufenes Fenster oder eine neue Marke (_e42_ende in evaluate).
    e42_marke: Optional[float] = None        # beobachtete Marke (None = keine Beobachtung)
    e42_richtung: str = "NONE"               # "LONG" | "SHORT"
    e42_start_ts: int = -1                   # Kerze, in der die Beobachtung begann
    e42_ausbruch_ts: int = -1                # Kerze des Ausbruchs (-1 = wartet darauf)
    # Marke des letzten Rueckkaufs: kein zweiter Rueckkauf auf dieselbe Marke. Ueberlebt
    # den Reset wie last_stop_ts.
    e42_gekauft: Optional[float] = None
    # Der offene Rueckkauf-Teil und sein eigener Stop (Kaiser 27.09.2026: der Stop an der
    # Marke gilt NUR fuer die zurueckgekauften 25 %). Die drei Merker sind die E41-Merker
    # dieses Teils - gerechnet von derselben stop_entscheidung() wie der Hauptstop.
    e42_teil_marke: Optional[float] = None
    e42_teil_wartet: int = 0
    e42_teil_wartet_inv: Optional[float] = None
    e42_teil_geprueft: Optional[float] = None
    # Investierter Anteil in Prozent der Position: Kaeufe +, Teilverkaeufe -, voller
    # Ausstieg 0, nie ueber 100. Grundlage fuer "nur, wenn nicht schon voll investiert".
    # entry_pct taugt dafuer nicht - es zaehlt nur Kaeufe und sinkt nie.
    bestand_pct: int = 0
    # NUR die Meldungen der zuletzt bewerteten Kerze (fuer Telegram). Nicht in state.json:
    # run_engine liest sie direkt nach jedem evaluate()-Aufruf.
    e42_meldungen: list = field(default_factory=list)


def _reset_position(pos: "Position") -> None:
    """Position schliessen: Zustand und alle Zaehler zurueck auf FLAT."""
    pos.direction, pos.state, pos.zones, pos.retrace_extreme = "NONE", PosState.FLAT, None, None
    pos.tp_rungs = 0
    pos.dip_buys = 0
    pos.buy_rungs = 0
    pos.entry_ref = None
    pos.lots = []
    pos.cost_basis_complete = True
    pos.valid_stop = None
    pos.valid_stop_reason = ""
    pos.migration_notes = [n for n in pos.migration_notes if n != 'prior_stop_maximum_unknown']
    pos.entry_pct = 0
    pos.liq_exits = 0
    pos.high_exits = 0
    pos.liq_entries = 0
    pos.ziel_extrem = None
    pos.be_aktiv = False
    pos.widerstand_exits = 0
    pos.stop_wartet = 0
    pos.stop_wartet_inv = None
    pos.stop_geprueft = None
    # E44.3: Der Rueckkauf-Teil gehoert zur Position und geht mit ihr. Die Beobachtung
    # (e42_marke ...) und e42_gekauft bleiben - siehe Position.
    pos.e42_teil_marke = None
    pos.e42_teil_wartet = 0
    pos.e42_teil_wartet_inv = None
    pos.e42_teil_geprueft = None
    pos.bestand_pct = 0


def resolve_stop(pos, cur, pivots, *, trail_stop=False, be_im_plus=False,
                 commit=False, stored_only=False):
    """Single stop source for engine and plan. Long boundaries survive breaches.

    A NEW structure candidate must be below the close; an existing boundary has
    no such filter. Reading a plan cannot move the stop or activate break-even.
    Short behavior is retained as historical diagnostics, outside V1.
    """
    if pos.state == PosState.FLAT or pos.zones is None:
        return None, ""
    long_side = pos.direction == "LONG"
    if stored_only and long_side and pos.valid_stop is not None:
        return pos.valid_stop, pos.valid_stop_reason
    level, reason = pos.zones.invalidation, "Invalidierung"
    active = be_im_plus and pos.be_aktiv and pos.entry_ref is not None
    if trail_stop and (pos.state in (PosState.TP1, PosState.TP2)
                       or pos.tp_rungs > 0 or active):
        candidates = [(level, reason)]
        if pos.entry_ref is not None:
            candidates.append((pos.entry_ref, "Einstand (nachgezogen)"))
        if long_side:
            lows = [p.price for p in pivots if p.kind == "L" and p.price < cur.close]
            if lows:
                candidates.append((max(lows), "Struktur-Tief"))
            level, reason = max(candidates, key=lambda x: x[0])
        else:
            highs = [p.price for p in pivots if p.kind == "H" and p.price > cur.close]
            if highs:
                candidates.append((min(highs), "Struktur-Hoch"))
            level, reason = min(candidates, key=lambda x: x[0])
    if long_side and pos.valid_stop is not None and pos.valid_stop >= level:
        level, reason = pos.valid_stop, pos.valid_stop_reason
    if commit and long_side:
        pos.valid_stop, pos.valid_stop_reason = level, reason
    return level, reason


# Einstiegs-Signaltypen je Richtung — daraus wird der Durchschnitts-Einstand gebildet
# (Basis fuer den nachgezogenen Break-even-Stop, E9.10).
_ENTRY_TYPES = {SignalType.KAUF_1, SignalType.KAUF_2, SignalType.NACHKAUF,
                SignalType.SHORT_1, SignalType.SHORT_2, SignalType.SHORT_NACHLEGEN}
# E34: welche davon eine LONG-Position aufbauen. Die Ampel bewertet je Signal in
# dessen eigener Richtung - was fuer einen Long spricht, spricht gegen einen Short.
_LONG_ENTRY_TYPES = {SignalType.KAUF_1, SignalType.KAUF_2, SignalType.NACHKAUF}

# E34: Faktor, mit dem die Ampel eine Einstiegs-Tranche verkleinert. 0.5 ist eine
# Setzung - halbe Position ist die einfachste Abstufung, die es gibt. Ob sie etwas
# bringt, beantwortet der Backtest; der Wert selbst wird NICHT mitoptimiert, solange
# nicht feststeht, dass die Ampel ueberhaupt etwas misst.
AMPEL_TRANCHE = 0.5


def kuerze_einstiege(signals: list, halbieren) -> None:
    """Verkleinert die EINSTIEGE einer Kerze; alles andere bleibt unangetastet (E34).

    `halbieren(long_side: bool) -> bool` entscheidet je Richtung.

    Eigene Funktion, weil sich die wichtigste Regel dieses Ausbaus sonst nicht pruefen
    laesst: In `evaluate` kann heute kein Ausstieg mit Tranche in derselben Kerze wie
    ein Einstieg stehen - die Ausstiegs-Zweige kehren vorher zurueck (nachgesehen ueber
    9.000 Kerzen: nur WARNUNG mit Tranche 0 kommt zusammen mit Einstiegen vor). Der
    Schutz waere damit zwar vorhanden, aber tote Absicherung, die kein Test erreicht -
    und eine kuenftige Aenderung koennte ihn unbemerkt entfernen. Hier ist er direkt
    pruefbar: ein halbierter STOPLOSS liesse die halbe Position im fallenden Markt
    liegen, der gefaehrlichste denkbare Fehler dieses Ausbaus.
    """
    for s in signals:
        if s.type in _ENTRY_TYPES and s.tranche_pct > 0 \
                and halbieren(s.type in _LONG_ENTRY_TYPES):
            s.tranche_pct = max(1, int(s.tranche_pct * AMPEL_TRANCHE))

# E18.2: Signale, die eine bestehende Position VERGROESSERN bzw. VERKLEINERN. Nur diese
# beiden Gruppen schliessen sich bei no_flip innerhalb einer Kerze gegenseitig aus.
# Bewusst NICHT dabei: STOPLOSS, VERKAUF_REST und ihre Short-Gegenstuecke — ein
# vollstaendiger Ausstieg darf nie unterdrueckt werden, egal was vorher in der Kerze
# passiert ist. Ersteinstiege aus FLAT sind ebenfalls nicht betroffen (anderer Zweig).
_AUFBAU_TYPES = {SignalType.NACHKAUF, SignalType.SHORT_NACHLEGEN,
                 SignalType.KAUF_2, SignalType.SHORT_2}
_TEILVERKAUF_TYPES = {SignalType.TEILVERKAUF_LADDER, SignalType.TEILVERKAUF_1,
                      SignalType.TEILVERKAUF_2, SignalType.SHORT_TP_LADDER,
                      SignalType.SHORT_TP_1, SignalType.SHORT_TP_2}
# E44.3 (E42): Rueckkauf nach Ruecktest und sein eigener Stop. Bewusst NICHT in
# _ENTRY_TYPES bzw. _TEILVERKAUF_TYPES: Der Rueckkauf-Teil hat einen eigenen Stop und
# soll weder den Durchschnitts-Einstand (und damit den nachgezogenen Stop der uebrigen
# Position) noch die Ampel oder freeze_targets beruehren (Plan E44, Abschnitt 7: "nichts
# an den Stops"). no_flip sieht beide trotzdem - siehe _darf_aufstocken/_darf_teilverkaufen.
_RUECKKAUF_TYPES = {SignalType.RUECKKAUF, SignalType.SHORT_RUECKTEST}
_RUECKKAUF_STOP_TYPES = {SignalType.RUECKKAUF_STOP, SignalType.SHORT_RUECKTEST_STOP}
# Vollstaendige Ausstiege: danach ist der investierte Anteil 0.
_VOLL_RAUS_TYPES = {SignalType.STOPLOSS, SignalType.SHORT_STOPLOSS,
                    SignalType.VERKAUF_REST, SignalType.SHORT_COVER_REST}


TRANCHEN = {"T1": 25, "CORE": 50, "FULL": 25, "TP1": 40, "TP2": 40}

# Bedingter Stop/Nachkauf (E9.3): statt pauschalem Stop bei Verlust nachkaufen, solange
# der Order-Flow den Trend bestaetigt (Furkan: "bei Verlust nachgekauft, weil vom
# Aufwaertstrend ueberzeugt"). MAX_DIP_BUYS begrenzt die Leiter; DIP_FLOOR_PCT ist der
# harte Boden — bricht der Kurs so weit durch, wird trotz Flow gestoppt (echter Bruch).
MAX_DIP_BUYS = 2
DIP_FLOOR_PCT = 0.05
DIP_TRANCHE = 20

# Mehrtages-Kaufleiter (E9.5): Furkan kauft in Tranchen ueber mehrere Tage in die
# Schwaeche nach, solange die Struktur intakt ist (nie all in; z. B. 27.-30.10.,
# 29.-31.01.). Jede neue Tiefkerze IN der Retracement-Zone (ueber Invalidierung, unter
# 0.5) mit Order-Flow-Bestaetigung = eine kleine Tranche, hoechstens MAX_BUY_RUNGS.
MAX_BUY_RUNGS = 3
BUY_LADDER_TRANCHE = 15

# Gestaffelte Teilgewinne (E8.2): Zwischenziele als Extension-Faktoren VOR dem
# 1.0-Ziel — Furkan verkauft in Leitern in die Staerke (z. B. 08.-22.04.). Je Stufe
# eine kleine Tranche; max. eine Stufe je Kerze, damit sich der Abbau ueber mehrere
# Tage verteilt (nie all out). Schaltbar ueber tp_ladder, per Backtest kalibriert.
LADDER_FACTORS = (0.8, 0.9)
LADDER_TRANCHE = 15

# Teilverkaeufe an Liquidationen (E9.11). Furkan verkauft erst, wenn der Kurs die
# Liquidationszonen erreicht — nicht schon vorher an einem rechnerischen Fib-Ziel.
# Seine Heatmap (wo Liquiditaet JETZT liegt) haben wir nicht; aus Coinalyze kennen wir
# aber, WIE VIEL je 4h-Kerze liquidiert wurde. Das Preisniveau liefert die Kerze selbst:
# Shorts werden am Hoch gerissen, Longs am Tief. Daraus zwei testbare Varianten:
#   "spike" = reaktiv, in die laufende Kaskade verkaufen (nur aktuelle Daten)
#   "zone"  = Preisniveaus vergangener Kaskaden als Magnete, Kurs laeuft wieder hinein
# WICHTIG (Kausalitaet): die Zonen werden ausschliesslich aus Kerzen VOR der aktuellen
# gebildet — sonst wuesste der Backtest die Zukunft und das Ergebnis waere wertlos.
MAX_LIQ_EXITS = 3          # hoechstens so viele Liquidations-Teilverkaeufe je Position
LIQ_SPIKE_MULT = 3.0       # Kaskade = mindestens 3x der Durchschnitt des Fensters
LIQ_LOOKBACK = 180         # Kerzen fuer die Zonen-Historie (180 x 4h = 30 Tage)
LIQ_ZONE_TOL = 0.005       # 0,5 % Toleranz: so nah muss der Kurs an die Zone
LIQ_ZONE_MIN_MULT = 3.0    # Zone = Kerze mit mindestens 3x der mittleren Liquidation


# Teilverkauf am letzten Hoch (E10.2, Furkan-Update 19:52: "die weiteren Gewinne werde
# ich bei ueber 66.600 HIER UNTER DIESEM HOCH rausnehmen"). Er verkauft am Struktur-
# Niveau, nicht am rechnerischen Fib-Ziel: Am alten Hoch sitzen die Kaeufer von damals,
# die bei plus/minus null aussteigen wollen — dort staut sich Angebot.
MAX_WIDERSTAND_EXITS = 2   # hoechstens so viele Teilverkaeufe am Gegen-Bein je Position (E20)
MAX_HIGH_EXITS = 2         # hoechstens so viele Struktur-Teilverkaeufe je Position
HIGH_EXIT_TOL = 0.005      # 0,5 % darunter reicht — vor der Masse raus

# Befund A5 (26.09.2026, beim Bau von E43.5 gefunden): next_pivot_beyond() sucht ueber
# ALLE geladenen Kerzen. Live laedt main.LIMIT_HAUPT (1.300) Spotkerzen (gleitendes
# Fenster); der Backtest rechnet ab Datenbeginn (wachsendes Fenster) - dieselbe
# Fehlerklasse wie A2/A3 (live != Backtest). high_exit_hist="live" bildet das gleitende
# Fenster im Backtest nach, indem die Pivotsuche NUR fuer den high_exit-Teilverkauf auf
# die letzten HIGH_EXIT_LIVE_KERZEN Kerzen beschraenkt wird - kein Eingriff in die
# uebrigen Pivot-Verwender (Impuls, Gegenzonen, 1D-Ebene).
HIGH_EXIT_LIVE_KERZEN = 1300   # main.LIMIT_HAUPT

# Liquidationszonen fuer den EINSTIEG (E10.3, Furkan-Update 18:27: "unter uns liegt
# deutlich mehr, viele Long-Positionen ab 61.700 runter bis 60.500 — HIER liegt dann auch
# aktuell das Golden Pocket, wo ich die Position wieder aufstocken wuerde").
# Er kauft, wo Longs liquidiert werden, und sucht die Konfluenz mit dem Golden Pocket.
# Bisher haben wir Liquidationsdaten nur fuer AUSSTIEGE getestet (E9.11) — dort kosteten
# sie Rendite. Fuer Einstiege ist es ungemessen.
MAX_LIQ_ENTRIES = 2        # hoechstens so viele Konfluenz-Nachkaeufe je Position
LIQ_ENTRY_TRANCHE = 20     # Tranche je Konfluenz-Nachkauf in %


def confirm_ok(pattern: "Pattern", flow: list[FlowPoint], long_side: bool,
               strict_confirm: bool = False, muster5_entry: bool = False) -> bool:
    """Order-Flow-Bestaetigung fuer einen Einstieg (E8.4/E38.2) - einzige Rechenstelle.

    evaluate() (Handel) UND die E43.6-Vorproben in backtest.py (Messung) lesen von hier,
    damit Messung nie eine andere Bestaetigung sieht als der Handel.
    """
    if long_side:
        strong = pattern == Pattern.CAPITULATION_RESET or (
            muster5_entry and pattern == Pattern.UNGESUNDER_ABVERKAUF)
        cvd_up = (len(flow) >= 3 and usable(flow[-1], "spot_cvd")
                  and usable(flow[-3], "spot_cvd")
                  and flow[-1].spot_cvd > flow[-3].spot_cvd)
        fund_ok = bool(flow) and usable(flow[-1], "funding") and flow[-1].funding <= 0
        return strong or (cvd_up and fund_ok) if strict_confirm else strong or fund_ok or cvd_up
    strong = pattern == Pattern.DERIVATE_PUMP
    cvd_dn = (len(flow) >= 3 and usable(flow[-1], "spot_cvd")
              and usable(flow[-3], "spot_cvd")
              and flow[-1].spot_cvd < flow[-3].spot_cvd)
    fund_hot = bool(flow) and usable(flow[-1], "funding") and flow[-1].funding > 0
    return strong or (cvd_dn and fund_hot) if strict_confirm else strong or fund_hot or cvd_dn


def next_pivot_beyond(pivots: list[Pivot], price: float, long_side: bool) -> Optional[float]:
    """Naechstes bestaetigtes Pivot-Hoch UEBER dem Preis (long) bzw. Pivot-Tief darunter."""
    if long_side:
        cands = [p.price for p in pivots if p.kind == "H" and p.price > price]
        return min(cands) if cands else None
    cands = [p.price for p in pivots if p.kind == "L" and p.price < price]
    return max(cands) if cands else None


def liq_cascade(flow: list[FlowPoint], side: str, window: int = 12,
                mult: float = LIQ_SPIKE_MULT) -> bool:
    """Laeuft in der JUENGSTEN Kerze eine Liquidations-Kaskade?

    side="short" = Short-Liquidationen (Squeeze nach oben -> gut fuer Long-Teilgewinne),
    side="long"  = Long-Liquidationen (Flush nach unten -> gut fuer Short-Teilgewinne).
    """
    f = flow[-window:]
    if not liq_evidence.known(f, ('long_liq', 'short_liq'), 'suppressed_missing_liq_cascade',
                            side, f[-1].ts + FOUR_HOURS_MS if f else None):
        return False
    if len(flow) < 3:
        return False
    vals = [(p.short_liq if side == "short" else p.long_liq) for p in f]
    if len(vals) < 2:
        return False
    base = sum(vals[:-1]) / (len(vals) - 1)
    return base > 0 and vals[-1] >= mult * base


def liq_levels(candles: list[Candle], flow: list[FlowPoint], side: str,
               lookback: int = LIQ_LOOKBACK,
               min_mult: float = LIQ_ZONE_MIN_MULT) -> list[tuple[float, float]]:
    """Preisniveaus, an denen historisch aussergewoehnlich viel liquidiert wurde.

    Gibt [(Preis, Liquidationsmasse USD)] zurueck, absteigend nach Masse. Der Preis ist
    das Kerzen-Hoch (side="short") bzw. -Tief (side="long") der Kaskaden-Kerze.
    Erwartet bereits beschnittene Listen (nur Kerzen VOR der Entscheidung).
    """
    n = min(len(candles), len(flow), lookback)
    if n == 0:
        return []
    cs, fs = candles[-n:], flow[-n:]
    decision_at = cs[-1].ts + 2 * FOUR_HOURS_MS
    if [c.ts for c in cs] != [f.ts for f in fs]:
        liq_evidence.record('suppressed_missing_liq_levels', side, decision_at,
                            detail='misaligned_history')
        return []
    if not liq_evidence.known(fs, ('long_liq', 'short_liq'), 'suppressed_missing_liq_levels',
                            side, decision_at):
        return []
    if n < 10:
        return []
    vals = [(p.short_liq if side == "short" else p.long_liq) for p in fs]
    total = sum(vals)
    if total <= 0:
        return []
    avg = total / len(vals)
    out = [((c.high if side == "short" else c.low), v)
           for c, v in zip(cs, vals) if v >= min_mult * avg]
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def in_liq_zone(price: float, levels: list[tuple[float, float]],
                tol: float = LIQ_ZONE_TOL) -> Optional[float]:
    """Liegt `price` innerhalb der Toleranz an einem der Niveaus? Gibt das Niveau zurueck."""
    for lvl, _mass in levels:
        if lvl > 0 and abs(price - lvl) / lvl <= tol:
            return lvl
    return None


def stop_entscheidung(pos: "Position", cur: "Candle", inv: float, long_side: bool,
                      puffer_pct: float = 0.0, rueckeroberung: int = 0,
                      auf_docht: bool = False) -> tuple:
    """E41: Loest der URSPRUENGLICHE Stop an der Invalidierung in dieser Kerze aus?

    Rueckgabe (stop, preis, grund). `grund` ist None, wenn der uebliche Text gilt.
    Wird nur aufgerufen, wenn mindestens einer der drei Schalter an ist; ein nach
    Teilgewinnen nachgezogener Stop kommt hier nie an (dort ist Gewinn gesichert).

    Auf Modulebene statt in evaluate() - die Lehre aus E34: Die Faelle (Schluss knapp
    darunter, Rueckeroberung, zweiter Bruch, harter Boden) lassen sich so einzeln
    pruefen, statt sie muehsam ueber ganze Kursverlaeufe herbeizufuehren.

    Die drei Varianten (docs/PLAN-E41-STOP.md):
      puffer_pct     Stop erst, wenn der Schluss mehr als puffer_pct jenseits liegt.
      rueckeroberung Kaisers Regel. Erster Schluss jenseits -> noch kein Stop. Wird die
                     Marke binnen `rueckeroberung` Kerzen zurueckerobert, gilt sie als
                     GEPRUEFT; der naechste Schluss jenseits stoppt sofort. Wird sie nicht
                     zurueckerobert -> Stop. Liegt der Schluss mehr als DIP_FLOOR_PCT
                     jenseits -> sofort Stop (der harte Boden aus E9.3, nicht neu gewaehlt).
      auf_docht      Gegenprobe, die STRENGERE Richtung: Stop schon, wenn das Kerzentief
                     die Marke beruehrt. Ausstieg zum Stopkurs, bei einer Luecke zum
                     Eroeffnungskurs - der Schlusskurs waere hier geschoent.
    """
    def jenseits(preis: float, abstand: float = 0.0) -> bool:
        if long_side:
            return preis < inv * (1 - abstand)
        return preis > inv * (1 + abstand)

    if auf_docht:
        if jenseits(cur.low if long_side else cur.high):
            fill = min(cur.open, inv) if long_side else max(cur.open, inv)
            return True, fill, "Kerzen{} {} Invalidierung {:.0f} (Docht) - Ausstieg zum Stopkurs".format(
                "tief" if long_side else "hoch", "unter" if long_side else "ueber", inv)
        return False, cur.close, None

    unter = jenseits(cur.close, puffer_pct)
    if rueckeroberung <= 0:
        grund = None
        if unter and puffer_pct > 0:
            grund = "Kerzenschluss mehr als {:.1f} % {} Invalidierung {:.0f} (Puffer)".format(
                puffer_pct * 100, "unter" if long_side else "ueber", inv).replace(".", ",", 1)
        return unter, cur.close, grund

    # --- Rueckeroberung ---------------------------------------------------------------
    if pos.stop_wartet and pos.stop_wartet_inv != inv:
        pos.stop_wartet = 0                      # neue Marke (Zonen nachgezogen): neu zaehlen
    if not unter:
        if pos.stop_wartet > 0:
            pos.stop_geprueft = inv              # zurueckerobert -> Marke ist geprueft
        pos.stop_wartet, pos.stop_wartet_inv = 0, None
        return False, cur.close, None
    seite = "unter" if long_side else "ueber"
    if pos.stop_geprueft == inv:
        return True, cur.close, ("Kerzenschluss {} Invalidierung {:.0f} - die Marke war schon "
                                 "einmal unterschritten und zurueckerobert, jetzt echter Bruch"
                                 ).format(seite, inv)
    if jenseits(cur.close, DIP_FLOOR_PCT):
        return True, cur.close, ("Kerzenschluss mehr als {} % {} Invalidierung {:.0f} - harter "
                                 "Boden, kein Warten").format(int(DIP_FLOOR_PCT * 100), seite, inv)
    pos.stop_wartet += 1
    pos.stop_wartet_inv = inv
    if pos.stop_wartet > rueckeroberung:
        return True, cur.close, ("Kerzenschluss {} Invalidierung {:.0f} - nach {} Kerze(n) nicht "
                                 "zurueckerobert").format(seite, inv, rueckeroberung)
    return False, cur.close, None


def muster5_haelt_zurueck(modus: str, pattern: "Pattern", richtung: str,
                          ziel: bool) -> bool:
    """E38.3: Haelt Muster 5 diesen Teilverkauf zurueck? (Default "off" = nie.)

    Eigene Funktion auf Modulebene, nicht innerhalb von evaluate() — die Lehre aus
    E34: Eine Regel, deren Faelle sich im laufenden System nur muehsam herbeifuehren
    lassen (hier: Muster 5 UND gleichzeitig ein faelliger Teilverkauf), wird sonst nie
    vollstaendig geprueft. So laesst sich jede Kombination direkt pruefen.

    `ziel=True` meint die geplanten Ziele an 1.0 und 1.618, `ziel=False` die
    Zwischenverkaeufe (Leiter, letztes Hoch, Liquidations- und Widerstandszone).

    NICHT hier abgefangen, weil sie an dieser Pruefung ohnehin vorbeilaufen: STOPLOSS
    und VERKAUF_REST. Ein vollstaendiger Ausstieg darf nie unterdrueckt werden — das
    waere der gefaehrlichste denkbare Fehler dieses Ausbaus (die Position bliebe im
    fallenden Markt liegen, weil ein Muster gerade "halten" sagt).
    """
    if modus == "off" or pattern != Pattern.UNGESUNDER_ABVERKAUF:
        return False
    if richtung != "LONG":
        # Bei einem Short ist Liquiditaet oberhalb ein Grund, EHER zu decken, nicht
        # spaeter — die Treibstoff-Lesart wirkt fuer den Short in die Gegenrichtung.
        return False
    return modus == "alle" or not ziel


# ------------------------------------------------ E44.3 (E42): Ausbruch mit Ruecktest
# Kaisers Regel, Werte von ihm am 26.09.2026 bestaetigt (docs/PLAN-E44-KOMBINATIONEN.md,
# Abschnitt 6 K1 und 10). Default aus (ausbruch_ruecktest=False) bis zur Messung E44.5.
RUECKTEST_FENSTER = 12                 # Kerzen nach dem Ausbruch = 2 Tage (Kaiser: "ja")
RUECKTEST_TOL = HIGH_EXIT_TOL          # Zone Marke +0,5 % - derselbe Abstand wie high_exit
RUECKKAUF_TRANCHE = TRANCHEN["T1"]     # 25 %, die Groesse von KAUF 1 (Kaiser: "ja")


def ruecktest_schritt(marke: float, lang: bool, seit: Optional[int], cur: "Candle",
                      fenster: int = RUECKTEST_FENSTER) -> str:
    """E44.3: Was bedeutet diese Kerze fuer die beobachtete Marke? (rein, ohne Zustand)

    `seit` = Kerzen seit der Ausbruchs-Kerze, None = es gab noch keinen Ausbruch.
    Rueckgabe:
      "ausbruch"    Die Kerze SCHLIESST jenseits der Marke. Ein Docht allein zaehlt nicht.
      "ruecktest"   Innerhalb des Fensters beruehrt das Tief die Zone Marke +0,5 % (Long)
                    und die Kerze schliesst NICHT darunter ("hoechstens mit dem Docht").
      "gescheitert" Schluss wieder diesseits der Marke - der Ausbruch hat nicht gehalten.
                    evaluate() wartet danach auf einen neuen Ausbruch.
      "verfallen"   Das Fenster ist ohne Ruecktest abgelaufen (die letzte Fensterkerze
                    ist bewertet). evaluate() beendet dann die Beobachtung.
      ""            nichts davon.
    Die Ausbruchs-Kerze selbst ist nie der Ruecktest ("innerhalb der NAECHSTEN 12").
    Auf Modulebene statt in evaluate() - die Lehre aus E34: jeder Fall einzeln pruefbar.
    """
    if lang:
        jenseits, diesseits = cur.close > marke, cur.close < marke
        beruehrt = cur.low <= marke * (1 + RUECKTEST_TOL)
    else:
        jenseits, diesseits = cur.close < marke, cur.close > marke
        beruehrt = cur.high >= marke * (1 - RUECKTEST_TOL)
    if seit is None:
        return "ausbruch" if jenseits else ""
    if seit > fenster:
        return "verfallen"
    if diesseits:
        return "gescheitert"
    if beruehrt:
        return "ruecktest"
    return "verfallen" if seit >= fenster else ""


def kerzen_seit(candles: list["Candle"], ts: int, grenze: int) -> int:
    """Wie viele Kerzen liegen nach der Kerze `ts`? Hoechstens `grenze`.

    Gezaehlt in der Kerzenliste, nicht ueber die Zeit - strategy_core ist
    Timeframe-agnostisch. Wird `ts` in den letzten `grenze` Kerzen nicht gefunden,
    ist sie aelter: Rueckgabe `grenze`.
    """
    for k in range(1, min(len(candles), grenze) + 1):
        if candles[-k].ts == ts:
            return k - 1
        if candles[-k].ts < ts:
            break
    return grenze


@dataclass
class _StopMerker:
    """Die drei E41-Merker, damit stop_entscheidung() auch den Stop des Rueckkauf-Teils
    rechnet (eine Rechenstelle fuer beide Stops, keine zweite Fassung der Regel)."""
    stop_wartet: int = 0
    stop_wartet_inv: Optional[float] = None
    stop_geprueft: Optional[float] = None


def ruecktest_teil_stop(pos: "Position", cur: "Candle", rueckeroberung: int) -> tuple:
    """E44.3: Loest der Stop des Rueckkauf-Teils in dieser Kerze aus?

    Stop bei Kerzenschluss jenseits der Marke, mit der live geschalteten Rueckeroberung
    (stop_rueckeroberung, heute 1 Kerze) und dem harten Boden - genau wie der Hauptstop.
    Aktualisiert die Merker in `pos`. Rueckgabe (stop, grund, wartet_neu, zurueck).
    """
    lang = pos.direction != "SHORT"
    m = _StopMerker(pos.e42_teil_wartet, pos.e42_teil_wartet_inv, pos.e42_teil_geprueft)
    vorher = m.stop_wartet
    marke = pos.e42_teil_marke
    hit, _preis, grund = stop_entscheidung(m, cur, marke, lang,
                                           rueckeroberung=rueckeroberung)
    pos.e42_teil_wartet, pos.e42_teil_wartet_inv, pos.e42_teil_geprueft = \
        m.stop_wartet, m.stop_wartet_inv, m.stop_geprueft
    seite = "unter" if lang else "ueber"
    if grund:
        grund = grund.replace("Invalidierung", "Ausbruchsmarke")
    else:
        grund = f"Kerzenschluss {seite} Ausbruchsmarke {marke:.0f}"
    wartet_neu = (not hit) and m.stop_wartet > vorher
    zurueck = (not hit) and vorher > 0 and m.stop_wartet == 0 and m.stop_geprueft == marke
    return hit, grund, wartet_neu, zurueck


def bestand_nach(start: int, signals: list) -> int:
    """Investierter Anteil nach diesen Signalen (E44.3, "nur, wenn nicht voll investiert").

    Kaeufe (auch der Rueckkauf) +Tranche, hoechstens 100; Teilverkaeufe (auch der Stop des
    Rueckkauf-Teils) -Tranche, mindestens 0; ein voller Ausstieg setzt auf 0. Eine
    Naeherung an simulate() (das Teilverkaeufe am Hoechstbestand bemisst) - fuer die Frage
    "voll oder nicht" genuegt sie.
    """
    b = start
    for s in signals:
        if s.type in _VOLL_RAUS_TYPES:
            b = 0
        elif s.type in _ENTRY_TYPES or s.type in _RUECKKAUF_TYPES:
            b = min(100, b + s.tranche_pct)
        elif s.type in _TEILVERKAUF_TYPES or s.type in _RUECKKAUF_STOP_TYPES:
            b = max(0, b - s.tranche_pct)
    return b


def evaluate(candles: list[Candle], flow: list[FlowPoint], pos: Position,
             bias_long: bool = True, bias_short: bool = True,
             pivot_n: int = 5, k_atr: float = 2.0,
             flush_entry: str = "core", tp_ladder: bool = True,
             trend_filter: bool = False, trend_ema: int = 200,
             strict_confirm: bool = False, confluence: bool = False,
             conditional_stop: bool = False, buy_ladder: bool = True,
             release_stale_rest: bool = False, trail_stop: bool = False,
             liq_exit: str = "off", high_exit: str = "off",
             liq_entry: str = "off", block_unhealthy: bool = False,
             muster5_entry: bool = False, muster5_halten: str = "off",
             stop_puffer_pct: float = 0.0, stop_rueckeroberung: int = 0,
             stop_auf_docht: bool = False,
             confirm_t1: bool = False, cooldown_h: float = 0.0,
             min_stop_pct: float = 0.0,
             no_flip: bool = False, freeze_targets: bool = False,
             min_bein_pct: float = 0.0, bein_wahl: str = "juengstes",
             be_im_plus: bool = False, bein_richtung: str = "auto",
             widerstand_exit: str = "off",
             rest_halten: bool = False,
             neustart_mit_rest: bool = False,
             zonen_1d: bool = False,
             zonen_nachziehen: bool = False,
             pivot_n_1d: int = 0,
             ampel_filter: str = "off",
             muster_cvd: str = "usd",
             muster_oi: str = "usd",
             high_exit_hist: str = "voll",
             ausbruch_ruecktest: bool = False,
             ruecktest_fenster: int = RUECKTEST_FENSTER) -> list[Signal]:
    """Live signal observer / historical diagnostic, without broker fills.

    Offline V1 uses execution_v1.run_v1 and confirms each trading block only
    after a booked fill. This wrapper deliberately preserves the live API.
    """
    return _evaluate(**locals())


def _evaluate(candles: list[Candle], flow: list[FlowPoint], pos: Position,
             bias_long: bool = True, bias_short: bool = True,
             pivot_n: int = 5, k_atr: float = 2.0,
             flush_entry: str = "core", tp_ladder: bool = True,
             trend_filter: bool = False, trend_ema: int = 200,
             strict_confirm: bool = False, confluence: bool = False,
             conditional_stop: bool = False, buy_ladder: bool = True,
             release_stale_rest: bool = False, trail_stop: bool = False,
             liq_exit: str = "off", high_exit: str = "off",
             liq_entry: str = "off", block_unhealthy: bool = False,
             muster5_entry: bool = False, muster5_halten: str = "off",
             stop_puffer_pct: float = 0.0, stop_rueckeroberung: int = 0,
             stop_auf_docht: bool = False,
             confirm_t1: bool = False, cooldown_h: float = 0.0,
             min_stop_pct: float = 0.0,
             no_flip: bool = False, freeze_targets: bool = False,
             min_bein_pct: float = 0.0, bein_wahl: str = "juengstes",
             be_im_plus: bool = False, bein_richtung: str = "auto",
             widerstand_exit: str = "off",
             rest_halten: bool = False,
             neustart_mit_rest: bool = False,
             zonen_1d: bool = False,
             zonen_nachziehen: bool = False,
             pivot_n_1d: int = 0,
             ampel_filter: str = "off",
             muster_cvd: str = "usd",
             muster_oi: str = "usd",
             high_exit_hist: str = "voll",
             ausbruch_ruecktest: bool = False,
             ruecktest_fenster: int = RUECKTEST_FENSTER,
             _execution_gate=None, _execution_sizes=None) -> list[Signal]:
    # AKTUELLE DEFAULTS (Stand 2026-07-24, gemessen im Voll-Daten-Fenster mit echtem
    # Coinalyze-OI, BACKTEST.md): n=5, k_atr=2.0, tp_ladder=True, buy_ladder=True,
    # flush_entry='core'. Beste gemessene Kombination war "nur Long + Flush core +
    # Kaufleiter": Recall 55 %, Praezision 30 %, Rendite +38,9 % (Buy&Hold -19,3 %).
    # Die RICHTUNG kommt nicht von hier, sondern aus site/data/config.json — live steht
    # bias_short=false (nur Long), weil mechanische Shorts ohne Makro-Bias verlieren.
    # HISTORIE (nicht mehr gueltig): vor E9.1 war flush_entry='off' der beste Wert —
    # damals fehlte echtes OI, Muster 4 war blind. Mit echten Liquidationsdaten dreht
    # sich das Ergebnis. tp_ladder (E8.2) bildet Furkans gestaffelte Gewinnmitnahme ab
    # (Recall/Praezision unveraendert, Rendite leicht besser). Recall != Gewinn.
    # E33 (13.09.2026): trend_ema Vorgabe von 50 auf 200 gehoben. 50 Tage sind kein
    # "uebergeordneter" Trend - Furkans Bias-Ebene sind Monate (Transkript 16:03).
    # Gefahrlos, weil trend_filter ueberall aus ist: kein Gitter-Eintrag und keine
    # Live-Einstellung nutzt ihn. Mit LIMIT_HAUPT=1300 reicht die Historie jetzt dafuer.
    # E8.5-Filter fuer bessere Einstiege (alle Furkans Methode, schaltbar, Default aus
    # bis per Backtest gemessen): trend_filter = nur Setups in Richtung des 1D-Trends
    # (Furkans Schritt 1, Preis vs. Tages-EMA); strict_confirm = KAUF 2 nur mit
    # Konfluenz (Spot-CVD dreht UND Funding stimmt, statt eines von beiden);
    # confluence = Einstieg nur, wenn die 4h-Zone in der 1D-Retracement-Zone liegt.
    # release_stale_rest (E9.9): gibt die Restposition in TP1/TP2 frei, sobald ein NEUER
    # signifikanter Impuls bestaetigt ist — die eingefrorenen Fib-Zonen der Position sind
    # dann veraltet. Behebt die Blockade, dass ein Rest von 20 % beliebig lange liegen
    # bleibt (Stop weit weg, Gegen-Muster tritt nicht ein) und dabei JEDE neue Einstiegs-
    # pruefung verhindert, weil der Einstiegs-Block nur bei state==FLAT laeuft.
    # Deckt Grundregel 1 ab: Zonen sind dynamisch, nie starr.
    # trail_stop (E9.10): zieht den Stop nach, sobald Teilgewinne realisiert sind —
    # auf Break-even (Durchschnitts-Einstand) bzw. hinter die Struktur, je nachdem was
    # hoeher liegt. Furkan (laut Kaiser): "Stop ueber den Kauf gezogen, dann kann ich
    # nichts mehr verlieren", Motto Kapital schuetzen. Loest zugleich die TP2-Blockade,
    # ohne den Rest wie bei release_stale_rest zum Marktpreis wegzuwerfen.
    # liq_exit (E9.11, Kaisers Beobachtung "Furkan faengt erst an zu verkaufen, wenn der
    # Kurs die Liquidationszonen erreicht"): "off" | "spike" (in die laufende Kaskade
    # verkaufen) | "zone" (Preisniveaus vergangener Kaskaden als Magnete) | "both".
    # Ergaenzt die Fib-Teilgewinne, ersetzt sie nicht; hoechstens MAX_LIQ_EXITS je Position.
    # high_exit (E10.2): Teilverkauf kurz UNTER dem letzten bestaetigten Pivot-Hoch statt
    # nur am Fib-Ziel. "off" | "on" | "weak". "weak" verkauft nur, wenn der Anlauf auf das
    # Hoch OHNE Spot-Nachfrage passiert (Furkan 20:17: "bei Breakouts muesste man
    # spaetestens da Spot-Nachfrage sehen") — so wird aus einer blossen Warnung eine
    # Handlung, die man messen kann.
    # liq_entry (E10.3): Liquidationszonen auf der EINSTIEGS-Seite. "off" | "boost" |
    # "filter". "boost" = zusaetzliche Nachkauf-Tranche, wenn die Fib-Zone mit einem
    # historischen Long-Liquidations-Cluster zusammenfaellt (Furkans Konfluenz).
    # "filter" = Einstiege NUR bei dieser Konfluenz (restriktiv, Gegenprobe).
    # ---------------------------------------------------------------- E13 (alle Default aus)
    # Vier Hebel gegen Einstiege in einen ungesunden Markt bzw. gegen zu viele Trades.
    # Befund, der sie ausgeloest hat (docs/VERLUST-ANALYSE-2026-07-27.md, Abschnitt 6c):
    # von 34 Ersteinstiegen hatten 16 GAR KEINE Flow-Pruefung (0.5-Level) und 16 das
    # Muster NEUTRAL — 32 von 34 ohne ein einziges gesundes Signal.
    # block_unhealthy: sperrt Einstiege UND Nachkaeufe, solange der Order-Flow gegen die
    #   Richtung laeuft — Long bei Muster 5 (ungesunder Abverkauf), Short bei Muster 1
    #   (gesunder Trend, also echte Spot-Nachfrage; in die shortet Furkan nicht).
    # ---------------------------------------------------------------- E38 (Default aus)
    # Muster 5 (UNGESUNDER_ABVERKAUF) hat heute KEINE Wirkung: der einzige Schalter daran
    # war block_unhealthy, und der ist seit E13 aus. E38.1 (20.09.2026) hat gemessen, was
    # nach dem Muster passiert: auf 1-2 Tage +1,03 bzw. +1,02 Punkte ueber der Grundrate
    # bei 76 % / 71 % hoeher geschlossenen Faellen (Grundrate 50 %), 26 Episoden. Nach 4
    # Tagen dreht es unter die Grundrate — ein KURZFRISTIGES Signal.
    #   Der Anlass ist Furkans Lesart (Video 13.09.2026, 15:44): neue aggressive Shorts
    # sind die Liquiditaet, die den Kurs spaeter nach oben zieht. Die Engine kennt den
    # Zustand bisher nur als Warnung.
    #   WICHTIGE WARNUNG AUS DERSELBEN MESSUNG: CAPITULATION_RESET hat den SCHLECHTESTEN
    # Nachlauf im ganzen Feld (-1,61 gegen Grundrate) — und genau darauf kauft die Engine
    # live und profitabel. Die Engine kauft eben nicht zum Musterzeitpunkt, sondern an der
    # Fib-Zone mit Stop. Ein Nachlauf-Median ist ein Hinweis, wo zu suchen ist, und NIE
    # ein Beleg, dass ein Schalter verdient. Deshalb stehen beide hier auf Default aus.
    # muster5_entry: Muster 5 zaehlt in _confirm_long() als starke Bestaetigung, genau wie
    #   Muster 4. Kein eigener Trigger — der Einstieg bleibt an die Fib-Zone gebunden.
    # muster5_halten: "off" | "leiter" | "alle". Bei Muster 5 werden Teilverkaeufe
    #   zurueckgehalten. "leiter" nur die Zwischenverkaeufe (Leiter, letztes Hoch,
    #   Liquidations- und Widerstandszone), "alle" auch die Ziel-Teilverkaeufe an 1.0/1.272.
    #   NUR bei Long: Bei einem Short ist Liquiditaet oberhalb ein Grund, EHER zu decken.
    #   NIE der Stop und nie ein vollstaendiger Ausstieg — die laufen an _darf_teilverkaufen()
    #   ohnehin vorbei (siehe _TEILVERKAUF_TYPES).
    #   Ob der Schalter ueberhaupt greift, zeigt die Spalte "Signale" im Gitter: gleiche
    #   Signalzahl wie die Basis heisst, er hat nie gegriffen (Lehre aus neustart_mit_rest,
    #   das in acht Monaten dreimal ansprang und deshalb nicht messbar war).
    # ---------------------------------------------------------------- E41 (Default aus)
    # Wie empfindlich der URSPRUENGLICHE Stop an der Invalidierung ausloest. Befund aus
    # E39: zehn Stops in acht Monaten, der Schluss lag im Median nur 0,28 % unter der
    # Marke, zwei Tage spaeter stand der Kurs in neun von zehn Faellen wieder darueber.
    # stop_puffer_pct:     Stop erst, wenn der Schluss mehr als diesen Anteil darunter liegt.
    # stop_rueckeroberung: Kaisers Regel (21.09.2026). Anzahl Kerzen, die fuer die
    #   Rueckeroberung bleiben (0 = aus). Zurueckerobert -> Marke geprueft -> der naechste
    #   Schluss darunter stoppt sofort. Waehrend des Wartens wird NICHT nachgekauft.
    # stop_auf_docht:      Gegenprobe - Stop schon beim Kerzentief.
    # Alle drei lassen den nachgezogenen Stop unberuehrt. Einzelheiten: stop_entscheidung().
    # confirm_t1: verlangt auch fuer den 0.5-Level-Einstieg eine Order-Flow-Bestaetigung.
    #   Dieser Zweig hatte bisher als einziger KEINE — er feuerte allein auf Preisberuehrung.
    # cooldown_h: Sperrfrist in Stunden nach einem Stop (0 = aus). Gegen die Saegeblatt-
    #   Serien: die laengste war 10 Stops in Folge, medianer Abstand zum Wiedereinstieg 68 h,
    #   kuerzester Fall 4 h. Furkan wartet nach einem Stop auf eine neue Struktur.
    # min_stop_pct: Mindestabstand Einstieg->Invalidierung als Anteil (0 = aus). 15 von 34
    #   Positionen lagen unter 2 %, eine bei 0,02 % — solche Stops loest schon das normale
    #   Rauschen aus. Furkans eigenes Video-Beispiel liegt bei 3,59 %.
    # ------------------------------------------------- E18 (Durchsicht 27.08.2026, Default aus)
    # no_flip: In einer Kerze wird nur in EINE Richtung gehandelt. Befund: 16 von 214
    #   Signalen der Live-Variante fielen auf Kerzen, in denen gleichzeitig aufgestockt
    #   UND teilverkauft wurde — meist zum selben Preis, weil der Nachkauf das TIEF der
    #   Kerze prueft und der Teilgewinn am letzten Hoch ihr HOCH. Zwei Gebuehren fuer ein
    #   Geschaeft, das sich selbst aufhebt, dazu zwei widersprechende Telegram-Nachrichten.
    #   Es entscheidet, was zuerst kommt; vollstaendige Ausstiege sind nie betroffen.
    # freeze_targets: Haelt die Zielreferenz fest, sobald der erste Teilgewinn realisiert
    #   ist. Bisher wanderte pos.retrace_extreme weiter, wenn der Kurs danach ein tieferes
    #   Tief machte, ohne den Stop auszuloesen — ext1/ext2 sanken mit. Nachgestellt: Ziel
    #   1.618 von 301,8 auf 257,8, also unter das urspruengliche 1.0-Ziel. Passt zum Befund,
    #   dass TEILVERKAUF_2 im ganzen Messfenster genau einmal vorkam.
    # ------------------------------------------- E19 (Furkan-Video 02.08.2026, Default aus)
    # min_bein_pct / bein_wahl: siehe last_significant_impulse — die Engine zeichnete Beine,
    #   die fuer ihren eigenen Mindest-Stopabstand zu klein sind (Median 4,0 %, noetig ~5,5 %).
    # ---------------------------------------------- E21 (Kaisers Frage 2026-08-27, Default aus)
    # Beobachtung: Furkan hat seit Anfang Juli EINE Position, stockt auf und nimmt
    # Teilgewinne — er steigt nie ganz aus und hat den August-Anstieg deshalb voll
    # mitgenommen. Unsere Engine fuehrt 22 Positionen in 8 Monaten und ist 55 % der Zeit
    # GANZ draussen; 12 der 21 abgeschlossenen Positionen wurden nicht vom Stop beendet,
    # sondern von der Regel "Gegen-Muster am Ziel", die den Rest zum Marktpreis abgibt.
    # ALLE bisher gemessenen Verkaufs-Mechanismen (be_im_plus, widerstand_exit,
    # freeze_targets, no_flip, liq_exit, high_exit) machen die Engine SCHNELLER draussen
    # und kosteten jedes Mal Rendite. Die Gegenrichtung wurde nie geprueft.
    # rest_halten: Der Rest wird bei Gegen-Muster NICHT mehr verkauft — er laeuft bis zum
    #   Stop. Die Engine ist damit investiert, solange die Struktur haelt.
    # neustart_mit_rest: Erlaubt einen neuen Einstieg, WAEHREND der Rest noch laeuft
    #   (Zustand TP1/TP2). Ohne das waere rest_halten eine Blockade — die Engine steigt
    #   sonst nur aus FLAT ein (das war der Befund von E9.9). Der alte Bestand bleibt im
    #   Durchschnitts-Einstand erhalten, die Zaehler des neuen Zyklus starten bei null.
    # widerstand_exit (E20, Default aus): Teilgewinn am Golden Pocket des GEGEN-Beins —
    #   also an der Widerstandszone, die Furkan im zweiten Fib-Raster fuehrt. Ergaenzt
    #   high_exit (letztes Pivot-Hoch); die Widerstandszone liegt typischerweise DARUNTER
    #   und wird damit frueher erreicht. Hoechstens MAX_WIDERSTAND_EXITS je Position.
    # bein_richtung="bias": Es wird nur ein Bein in der HANDELBAREN Richtung gesucht. Steht
    #   bias_short=false (live), sucht die Engine also ein Aufwaerts-Bein und ignoriert das
    #   juengere Abwaerts-Bein, das sie ohnehin nicht handeln duerfte. An 17 von 30 Tagen
    #   (27.07.-27.08.2026) war genau das der Grund fuer Stillstand. Wirkt nur, wenn
    #   genau eine Richtung erlaubt ist; bei Long UND Short aendert sich nichts.
    # be_im_plus: zieht den Stop auf Break-even, sobald die Position im Plus steht — nicht
    #   erst nach einem Teilgewinn. Furkan am 02.08. (16:07-16:25): "wenn ich die Order
    #   gefuellt bekomme, wuerde ich meinen Stop hochsetzen auf das neue Entry. Mit der
    #   Position moechte ich nicht mehr in Verlust gehen." Deckt zugleich den nie gebauten
    #   Punkt 2 aus docs/VERLUST-ANALYSE-2026-07-27.md ab (44 % der Verlustsumme). Wirkt nur
    #   zusammen mit trail_stop und nur, wenn der Einstand UNTER dem Kurs liegt — sonst
    #   waere der Stop im selben Moment ausgeloest.
    # muster_cvd (E43.3, Befund A2 der Gesamtpruefung 26.09.2026): "alt" | "usd". Bei
    #   "usd" vergleicht Muster 2 (Derivate-Pump) Spot- und Futures-Delta als Dollar-
    #   Betraege im Fenster statt als Anteile an einer willkuerlich begonnenen Summe.
    #   Muster 2 sperrt Einstiege und loest den Restverkauf aus - die Korrektur aendert
    #   also Signale. Default "alt", bis der Backtest gegen die Entscheidungsregel in
    #   docs/PLAN-E43-PRUEFUNGS-KORREKTUREN.md gemessen hat.
    # muster_oi (E43.4, Befund A3 der Gesamtpruefung 26.09.2026): "usd" | "btc". Bei "btc"
    #   misst die Mustererkennung die OI-Veraenderung in Kontrakten statt in Dollar - in
    #   Dollar erfuellt schon die Kursbewegung die Schwellen von Muster 2 und 4. Wirkt
    #   ueber die Muster auf Einstiegssperre (2), Bestaetigung (4) und Restverkauf (2, 3).
    #   Default "usd", bis der Backtest gegen die Entscheidungsregel gemessen hat.
    # ---------------------------------------- E44.3 (E42, Kaisers Regel, Default aus)
    # ausbruch_ruecktest: Nach einem Teilverkauf am letzten Hoch (high_exit) oder einem
    #   Rest-Verkauf (nicht nach einem Stop) beobachtet die Engine die Marke - dieselbe,
    #   die high_exit benutzt (next_pivot_beyond). Schliesst eine Kerze darueber
    #   (Ausbruch) und beruehrt eine der naechsten `ruecktest_fenster` Kerzen die Zone
    #   Marke +0,5 %, ohne darunter zu schliessen (Ruecktest), kauft sie 25 % zurueck,
    #   zum Schluss dieser Kerze. Stop NUR fuer diesen Teil: Schluss unter der Marke, mit
    #   der live geschalteten Rueckeroberung (Kaiser 27.09.2026: "nur die 25 %"). Ziele
    #   ueber die vorhandene Extension-Logik. Nicht, wenn schon voll investiert; kein
    #   zweiter Rueckkauf auf dieselbe Marke. Anlass: high_exit verkauft unter dem Hoch
    #   und hinterlaesst einen Zustand, den niemand aufloest - die Engine ist draussen,
    #   das Hoch ist durchbrochen (Plan E44, Abschnitt 6 K1). Spiegelbildlich fuer Short.
    # ruecktest_fenster: Kerzen fuer den Ruecktest (Kaiser: 12 = 2 Tage). Die
    #   Robustheitszeile in E44.5 misst 6 - sie entscheidet nichts.
    """Bewertet die juengste ABGESCHLOSSENE Kerze und liefert neue Signale.

    Idempotent: dieselbe Kerze (ts) erzeugt nie zweimal Signale (pos.last_signal_ts).
    `pos` wird mutiert (Zustandsmaschine); Aufrufer persistiert `pos` in state.json.
    """
    if not candles:
        return []

    # E34: das Bein der Position, wie es zu BEGINN dieser Kerze feststand. Die Ampel
    # bewertet die Lage, in der die Entscheidung faellt - nicht die Lage danach.
    # Ohne diesen Merker vergliche sie bei einem frischen Einstieg das gerade gesetzte
    # Bein mit sich selbst ("Struktur unveraendert") und bekaeme ein Argument dafuer
    # geschenkt, das keine Information enthaelt. Steht hier ganz oben, weil sowohl
    # _versuche_einstieg() als auch zonen_nachziehen pos.zones unterwegs ersetzen.
    _pos_imp_vorher = pos.zones.impulse if pos.zones is not None else None
    # E44.3: Meldungen gelten nur fuer DIESE Kerze - auch eine schon bewertete Kerze darf
    # keine alten Meldungen stehen lassen, sonst sendete run_engine sie ein zweites Mal.
    pos.e42_meldungen = []
    cur = candles[-1]
    if cur.ts <= pos.last_signal_ts:
        return []
    _bestand_start = pos.bestand_pct

    signals: list[Signal] = []
    # V1/offline: gate each complete trading block BEFORE its state writes.
    # Default None preserves the live observer and historical diagnostic API.
    # A declined block leaves its trading markers untouched. Time observations
    # (E41, extremes, dedupe) still advance. No broker confirmation is implied.
    _actions = []

    def _allow(action: str) -> bool:
        if _execution_gate is None or _execution_gate(action):
            _actions.append(action)
            return True
        return False
    # E41: in dieser Kerze kein Aufstocken, weil auf eine Rueckeroberung gewartet wird
    # oder gerade zurueckerobert wurde. Wird im Stop-Block gesetzt.
    _e41_sperre = False

    def _darf_aufstocken() -> bool:
        """E18.2: Nach einem Teilgewinn in derselben Kerze wird nicht nachgelegt.
        E41: Waehrend auf eine Rueckeroberung gewartet wird, auch nicht - ein Nachkauf
        unter der Invalidierung waere der durchgefallene conditional_stop (E9.3) durch
        die Hintertuer."""
        if _e41_sperre:
            return False
        # E44.3: Der Stop des Rueckkauf-Teils ist ein Verkauf - no_flip sieht ihn wie einen
        # Teilverkauf. Ohne Signale dieser Art ist die Bedingung Wort fuer Wort die alte.
        return not (no_flip and any(x.type in _TEILVERKAUF_TYPES
                                    or x.type in _RUECKKAUF_STOP_TYPES for x in signals))

    def _darf_teilverkaufen(ziel: bool = False) -> bool:
        """E18.2: Nach einem Nachkauf in derselben Kerze wird nicht teilverkauft.
        E38.3: Bei Muster 5 werden Teilverkaeufe zurueckgehalten (Default aus).

        Der Waechter sitzt VOR dem Erzeugen des Signals. Das ist der Grund, warum E38.3
        hier ansetzt und nicht hinterher aufraeumt: Wer ein fertiges Teilverkauf-Signal
        wieder entfernt, muss tp_rungs, high_exits, liq_exits, widerstand_exits UND
        pos.state zurueckdrehen — jeder vergessene Zaehler waere ein stiller Fehler
        (eine Leiterstufe gilt als verbraucht, ohne dass verkauft wurde).
        """
        # E44.3: Der Rueckkauf steht hier bewusst NICHT - er wird erst NACH allen
        # Teilverkaeufen der Kerze bewertet, ein Teilverkauf nach ihm ist unmoeglich. Die
        # umgekehrte Folge (erst Teilverkauf, dann kein Rueckkauf) regelt _darf_aufstocken.
        if no_flip and any(x.type in _AUFBAU_TYPES for x in signals):
            return False
        return not muster5_haelt_zurueck(muster5_halten, pattern, pos.direction, ziel)

    # --- E44.3 (E42) Helfer --------------------------------------------------------------
    def _e42_melden(art: str, marke: float, lang: bool, **extra) -> None:
        pos.e42_meldungen.append({"art": art, "ts": cur.ts, "kurs": cur.close,
                                  "marke": marke, "lang": lang, **extra})

    def _e42_zone(marke: float, lang: bool) -> float:
        return marke * (1 + RUECKTEST_TOL) if lang else marke * (1 - RUECKTEST_TOL)

    def _e42_ende() -> None:
        pos.e42_marke, pos.e42_richtung = None, "NONE"
        pos.e42_start_ts = pos.e42_ausbruch_ts = -1

    def _piv_marke():
        # dieselbe Pivot-Liste wie high_exit (A5: high_exit_hist)
        return pivots if high_exit_hist != "live" \
            else find_pivots(candles[-HIGH_EXIT_LIVE_KERZEN:], n=pivot_n)

    def _e42_beobachten(marke: Optional[float], long_side: bool) -> str:
        """Beobachtung einer Marke beginnen. Rueckgabe: Zusatz fuer den Verkaufsgrund.

        Ohne brauchbare Marke (kein Pivot jenseits, oder auf diese Marke wurde schon
        zurueckgekauft) bleibt eine laufende Beobachtung unberuehrt."""
        if not ausbruch_ruecktest or marke is None or marke == pos.e42_gekauft:
            return ""
        pos.e42_marke, pos.e42_richtung = marke, ("LONG" if long_side else "SHORT")
        pos.e42_start_ts = cur.ts
        jenseits = cur.close > marke if long_side else cur.close < marke
        pos.e42_ausbruch_ts = cur.ts if jenseits else -1
        if jenseits:
            # Die Verkaufskerze selbst schliesst schon jenseits: das IST der Ausbruch.
            _e42_melden("ausbruch", marke, long_side, fenster=ruecktest_fenster,
                        zone=_e42_zone(marke, long_side), tranche=RUECKKAUF_TRANCHE)
        return (" - beobachte {} {:.0f} ({} nach Ruecktest)".format(
            "Ausbruch ueber" if long_side else "Durchbruch unter", marke,
            "Rueckkauf" if long_side else "Short"))

    # E43.3/E43.4: muster_cvd und muster_oi MUESSEN hier ankommen - sonst misst die
    # Gitterzeile die Live-Zeile.
    pattern = classify_pattern(candles, flow, muster_cvd=muster_cvd,
                               muster_oi=muster_oi) if flow else Pattern.NEUTRAL
    pivots = find_pivots(candles, n=pivot_n)
    _nur_auf = None
    if bein_richtung == "bias" and bias_long != bias_short:
        _nur_auf = bias_long                     # genau eine Richtung erlaubt -> nur deren Beine
    imp = last_significant_impulse(candles, pivots, k_atr=k_atr,
                                   min_bein_pct=min_bein_pct, bein_wahl=bein_wahl,
                                   nur_auf=_nur_auf)

    # --- E8.5-Kontext (nur berechnen, wenn ein Filter aktiv ist)
    _trend = daily_trend(candles, trend_ema, streng=True) if trend_filter else None
    _dzone = daily_fib_zone(candles, min_bein_pct=min_bein_pct,
                            bein_wahl=bein_wahl) if confluence else None

    # E23: die 1D-Ebene als EIGENER Zonensatz (STRATEGIE.md 4.1 Punkt 4: "beide Ebenen
    # ueberwachen"). Der _dzone-Aufruf darueber bleibt bewusst unveraendert — er gehoert
    # zu confluence (1D als FILTER auf 4h-Setups) und ist bereits gemessen.
    # Hier werden pivot_n und k_atr aus den Live-Werten durchgereicht, damit beide Ebenen
    # dieselbe Signifikanz-Schwelle benutzen. 66 Tage Kontext (400 4h-Kerzen, main.py)
    # reichen: nachgerechnet ueber 394 Tage ist das 1D-Bein aus 66 Tagen in 100 % der
    # Faelle identisch mit dem aus voller Historie (docs/PRUEFUNG-1D-EBENE.md).
    _imp_1d = None
    if zonen_1d:
        _z1d = daily_fib_zone(candles, pivot_n=pivot_n, k_atr=k_atr,
                              min_bein_pct=min_bein_pct, bein_wahl=bein_wahl,
                              pivot_n_1d=pivot_n_1d)
        if _z1d is not None:
            _imp_1d = _z1d.impulse

    def _trend_ok(long_side: bool) -> bool:
        # E33: _trend wird streng berechnet - reicht die Historie fuer den verlangten
        # EMA nicht, ist _trend None und es wird NICHTS blockiert. Unbekannt heisst
        # nicht verboten.
        if not trend_filter or _trend is None or _trend[1] is None:
            return True                                  # unbekannt -> nicht blockieren
        close, e = _trend
        return close >= e if long_side else close <= e

    # Liquidations-Niveaus je Seite nur einmal je Kerze berechnen (E10.3). Ausschliesslich
    # aus Kerzen VOR der aktuellen — sonst wuesste der Backtest die Zukunft.
    _liq_cache: dict = {}

    def _liq_hit(price: float, side: str):
        if side not in _liq_cache:
            _liq_cache[side] = liq_levels(candles[:-1], flow[:-1], side)
        return in_liq_zone(price, _liq_cache[side])

    def _liq_entry_ok(price: float, long_side: bool) -> bool:
        """Nur im Modus 'filter' eine Bedingung; sonst nie blockierend."""
        if liq_entry != "filter":
            return True
        return _liq_hit(price, "long" if long_side else "short") is not None

    def _confluence_ok(price: float) -> bool:
        if not confluence or _dzone is None:
            return True                                  # 1D-Zone unbekannt -> nicht blockieren
        lo, hi = sorted((_dzone.level_05, _dzone.level_0786))
        return lo <= price <= hi

    def _confirm_long() -> bool:
        # E38.2: Muster 5 wird zur starken Bestaetigung wie Muster 4 — nicht zu einem
        # eigenen Trigger. Der Einstieg bleibt an die Fib-Zone gebunden, sonst kauft die
        # Engine im Nichts. Rechnung: confirm_ok() (einzige Rechenstelle, E43.6).
        return confirm_ok(pattern, flow, True, strict_confirm, muster5_entry)

    def _confirm_short() -> bool:
        return confirm_ok(pattern, flow, False, strict_confirm, muster5_entry)

    # --- E13-Helfer -----------------------------------------------------------------
    def _healthy(long_side: bool) -> bool:
        """Warnlicht: laeuft der Order-Flow gerade GEGEN die geplante Richtung?"""
        if not block_unhealthy:
            return True
        if long_side:
            return pattern != Pattern.UNGESUNDER_ABVERKAUF
        return pattern != Pattern.GESUNDER_TREND      # nicht in echte Spot-Nachfrage shorten

    def _t1_ok(long_side: bool) -> bool:
        """Order-Flow-Bestaetigung auch fuer den 0.5-Level-Einstieg (sonst ungeprueft)."""
        if not confirm_t1:
            return True
        return _confirm_long() if long_side else _confirm_short()

    def _cooldown_ok() -> bool:
        """Sperrfrist nach einem Stop abgelaufen?"""
        if cooldown_h <= 0 or pos.last_stop_ts <= 0:
            return True
        return (cur.ts - pos.last_stop_ts) >= cooldown_h * 3600 * 1000

    def _stop_weit_genug(preis: float, zonen: FibZones) -> bool:
        """Liegt die Invalidierung weit genug vom Einstieg entfernt?"""
        if min_stop_pct <= 0 or preis <= 0:
            return True
        return abs(preis - zonen.invalidation) / preis >= min_stop_pct

    # --- Einstiegs-Logik: Referenz-Impuls noetig
    # Als Funktion, weil sie an ZWEI Stellen gebraucht wird: aus FLAT (Normalfall) und —
    # wenn neustart_mit_rest an ist — bei laufendem Rest nach den Teilgewinnen (E21).
    def _versuche_einstieg(imp_arg: Optional[Impulse] = None) -> None:
        # imp_arg=None -> der 4h-Referenz-Impuls (Normalfall, Verhalten wie bisher).
        # Mit Impuls -> der zweite Durchlauf fuer die 1D-Ebene (E23); die Signal-Gruende
        # bekommen dann " [1D]" angehaengt, damit im Bericht und in Telegram sichtbar
        # ist, welche Ebene den Einstieg gestellt hat.
        _imp = imp if imp_arg is None else imp_arg
        _mark = "" if imp_arg is None else " [1D]"
        z = fib_zones(_imp)
        if _imp.up and bias_long and pattern != Pattern.DERIVATE_PUMP and _healthy(True):
            if (cur.low <= z.level_05 and cur.low > z.gp_upper
                    and _trend_ok(True) and _confluence_ok(cur.low)
                    and _liq_entry_ok(cur.low, True)
                    and _t1_ok(True) and _stop_weit_genug(z.level_05, z)
                    and _allow("entry_t1")):
                pos.direction, pos.state, pos.zones = "LONG", PosState.T1, z
                pos.retrace_extreme = cur.low
                signals.append(Signal(cur.ts, SignalType.KAUF_1, z.level_05, TRANCHEN["T1"],
                                      f"0.5-Retracement des Impulses {_imp.start.price:.0f}->{_imp.end.price:.0f}{_mark}",
                                      stop_ref=z.invalidation))
            elif z.gp_lower <= cur.low <= z.gp_upper:
                if (_confirm_long() and _trend_ok(True) and _confluence_ok(cur.low)
                        and _liq_entry_ok(cur.low, True)
                        and _stop_weit_genug(z.gp_upper, z) and _allow("entry_gp")):
                    pos.direction, pos.state, pos.zones = "LONG", PosState.CORE, z
                    pos.retrace_extreme = cur.low
                    signals.append(Signal(cur.ts, SignalType.KAUF_2, z.gp_upper,
                                          TRANCHEN["T1"] + TRANCHEN["CORE"],
                                          f"Golden Pocket {z.gp_lower:.0f}-{z.gp_upper:.0f} + Bestaetigung ({pattern.name}){_mark}",
                                          stop_ref=z.invalidation))
            elif (flush_entry != "off" and cur.low < z.gp_lower
                  and cur.close > z.invalidation):
                # Capitulation-Einstieg (E8.1): Kerze durchschlaegt das GP nach unten
                # (Flush-Tage wie 10.10./04.11.), schliesst aber ueber der Invalidierung
                if (_confirm_long() and _trend_ok(True) and _liq_entry_ok(cur.low, True)
                        and _stop_weit_genug(cur.close, z) and _allow("entry_flush")):
                    small = flush_entry == "t1"
                    st = PosState.T1 if small else PosState.CORE
                    sig_t = SignalType.KAUF_1 if small else SignalType.KAUF_2
                    tr = TRANCHEN["T1"] if small else TRANCHEN["T1"] + TRANCHEN["CORE"]
                    pos.direction, pos.state, pos.zones = "LONG", st, z
                    pos.retrace_extreme = cur.low
                    signals.append(Signal(cur.ts, sig_t, cur.close, tr,
                                          f"Capitulation: GP durchschlagen (Tief {cur.low:.0f}), Schluss ueber Invalidierung ({pattern.name}){_mark}",
                                          stop_ref=z.invalidation, tag="FLUSH"))
        elif ((not _imp.up) and bias_short and pattern != Pattern.CAPITULATION_RESET
                and _healthy(False)):
            if (cur.high >= z.level_05 and cur.high < z.gp_upper
                    and _trend_ok(False) and _confluence_ok(cur.high)
                    and _liq_entry_ok(cur.high, False)
                    and _t1_ok(False) and _stop_weit_genug(z.level_05, z)
                    and _allow("entry_t1")):
                pos.direction, pos.state, pos.zones = "SHORT", PosState.T1, z
                pos.retrace_extreme = cur.high
                signals.append(Signal(cur.ts, SignalType.SHORT_1, z.level_05, TRANCHEN["T1"],
                                      f"0.5-Retracement des Abwaerts-Impulses {_imp.start.price:.0f}->{_imp.end.price:.0f}{_mark}",
                                      stop_ref=z.invalidation))
            elif z.gp_upper <= cur.high <= z.gp_lower:  # Short: 0.65 liegt OBEN
                if (_confirm_short() and _trend_ok(False) and _confluence_ok(cur.high)
                        and _liq_entry_ok(cur.high, False)
                        and _stop_weit_genug(z.gp_upper, z) and _allow("entry_gp")):
                    pos.direction, pos.state, pos.zones = "SHORT", PosState.CORE, z
                    pos.retrace_extreme = cur.high
                    signals.append(Signal(cur.ts, SignalType.SHORT_2, z.gp_upper,
                                          TRANCHEN["T1"] + TRANCHEN["CORE"],
                                          f"Golden Pocket {z.gp_upper:.0f}-{z.gp_lower:.0f} + Bestaetigung ({pattern.name}){_mark}",
                                          stop_ref=z.invalidation))
            elif (flush_entry != "off" and cur.high > z.gp_lower
                  and cur.close < z.invalidation):
                # Squeeze-Einstieg (E8.1, Spiegelbild): Kerze durchschlaegt das GP nach
                # oben, schliesst aber unter der Invalidierung
                if (_confirm_short() and _trend_ok(False) and _liq_entry_ok(cur.high, False)
                        and _stop_weit_genug(cur.close, z) and _allow("entry_flush")):
                    small = flush_entry == "t1"
                    st = PosState.T1 if small else PosState.CORE
                    sig_t = SignalType.SHORT_1 if small else SignalType.SHORT_2
                    tr = TRANCHEN["T1"] if small else TRANCHEN["T1"] + TRANCHEN["CORE"]
                    pos.direction, pos.state, pos.zones = "SHORT", st, z
                    pos.retrace_extreme = cur.high
                    signals.append(Signal(cur.ts, sig_t, cur.close, tr,
                                          f"Squeeze: GP durchschlagen (Hoch {cur.high:.0f}), Schluss unter Invalidierung ({pattern.name}){_mark}",
                                          stop_ref=z.invalidation, tag="FLUSH"))

    # Bei zonen_1d=False ist _imp_1d immer None — die Bedingung ist dann Wort fuer Wort
    # die alte, das Verhalten bitgleich.
    if pos.state == PosState.FLAT and (imp is not None or _imp_1d is not None) \
            and _cooldown_ok():
        if imp is not None:
            _versuche_einstieg()
        # E23: erst wenn die 4h-Ebene nichts hergibt, bekommt die 1D-Ebene ihren Versuch.
        # 4h behaelt also den Vorrang; 1D ergaenzt, ersetzt nicht.
        if (pos.state == PosState.FLAT and _imp_1d is not None
                and not gleiches_bein(imp, _imp_1d)):
            _versuche_einstieg(_imp_1d)

    # --- Positions-Management
    elif pos.state != PosState.FLAT and pos.zones is not None:
        # E30: Zonen nachziehen, solange der Trend intakt ist. Bis hierher waren die
        # Zonen einer laufenden Position hart eingefroren — im Widerspruch zu
        # Grundregel 1 ("Zonen sind dynamisch, nie starr") und in sich asymmetrisch:
        # Ziele (retrace_extreme) und Stop (trail_stop) wanderten laengst mit, nur die
        # Kaufbereiche nicht. Nachgezogen wird NUR bei intaktem Trend (hoeheres Tief
        # UND hoeheres Hoch, bei Short spiegelbildlich) — bricht die Struktur, bleiben
        # die alten Zonen stehen, damit die Engine keiner kaputten Struktur nachkauft.
        # Der Stop kann dadurch nur steigen: invalidation == impuls.start.
        if zonen_nachziehen and imp is not None:
            _alt = pos.zones.impulse
            if (imp.start.ts, imp.end.ts) != (_alt.start.ts, _alt.end.ts) \
                    and trend_intakt(_alt, imp):
                pos.zones = fib_zones(imp)
        z = pos.zones
        long_side = pos.direction == "LONG"
        # Retracement-Extrem fortschreiben (fuer Extension-Ziele); neues Extrem merken
        prev_extreme = pos.retrace_extreme
        if long_side:
            pos.retrace_extreme = min(pos.retrace_extreme or cur.low, cur.low)
            made_new_extreme = prev_extreme is not None and cur.low < prev_extreme
        else:
            pos.retrace_extreme = max(pos.retrace_extreme or cur.high, cur.high)
            made_new_extreme = prev_extreme is not None and cur.high > prev_extreme
        # E18.3: Nach dem ersten Teilgewinn zaehlt die eingefrorene Referenz — sonst
        # wandern die Ziele mit jedem neuen Tief nach unten. pos.retrace_extreme laeuft
        # unveraendert weiter, damit die Mehrtages-Kaufleiter davon unberuehrt bleibt.
        ziel_ref = pos.ziel_extrem if (freeze_targets and pos.ziel_extrem is not None) \
            else pos.retrace_extreme
        ext1 = z.ext_target(ziel_ref, 1.0)
        ext2 = z.ext_target(ziel_ref, 1.618)

        # Nachgezogener Stop (E9.10, Kaisers Furkan-Zitat: "Stop ueber den Kauf gezogen,
        # dann kann ich nichts mehr verlieren" — Motto Kapital schuetzen). Sobald
        # Teilgewinne realisiert sind (TP1/TP2 ODER eine Leiter-Stufe gefeuert), wandert
        # der Stop auf den hoechsten der drei Bezugspunkte: urspruengliche Invalidierung,
        # Durchschnitts-Einstand (Break-even) und letztes bestaetigtes Pivot-Tief unter
        # dem Kurs (Struktur). Er kann dadurch NUR steigen, nie lockerer werden.
        # E19.3: Break-even schon, sobald die Position EINMAL im Plus stand (nicht erst nach
        # einem Teilgewinn). Der Merker ist noetig, weil die Bedingung genau in der Kerze,
        # in der der Stop greifen soll, nicht mehr erfuellt waere — der Kurs ist dann ja
        # zurueck unter dem Einstand. Er wird gesetzt, solange der Kurs darueber steht.
        if be_im_plus and pos.entry_ref is not None and not pos.be_aktiv:
            if (cur.close > pos.entry_ref) if long_side else (cur.close < pos.entry_ref):
                pos.be_aktiv = True
        stop_level, trail_note = resolve_stop(pos, cur, pivots, trail_stop=trail_stop,
                                             be_im_plus=be_im_plus, commit=True)
        stop_hit = (cur.close < stop_level) if long_side else (cur.close > stop_level)
        stop_preis, stop_grund = cur.close, None
        # E41: nur der URSPRUENGLICHE Stop - ein nachgezogener sichert Gewinn und bleibt.
        if trail_note in ("", "Invalidierung") and (
                stop_puffer_pct > 0 or stop_rueckeroberung > 0 or stop_auf_docht):
            _wartete = pos.stop_wartet > 0
            stop_hit, stop_preis, stop_grund = stop_entscheidung(
                pos, cur, stop_level, long_side, puffer_pct=stop_puffer_pct,
                rueckeroberung=stop_rueckeroberung, auf_docht=stop_auf_docht)
            # Gesperrt in der Wartekerze UND in der Kerze der Rueckeroberung: Die
            # Bestaetigung steht erst mit deren Schluss fest.
            _e41_sperre = _wartete or pos.stop_wartet > 0
        # Bedingter Stop (E9.3): bei Verlust nachkaufen statt stoppen, solange der
        # Order-Flow den Trend weiter bestaetigt (Furkan) — aber nur bis zum harten
        # Boden (DIP_FLOOR_PCT) und hoechstens MAX_DIP_BUYS mal.
        # Nur beim urspruenglichen Stop nachkaufen — einen nachgezogenen Gewinn-Stop
        # darf der bedingte Nachkauf nicht aushebeln.
        if stop_hit and conditional_stop and trail_note in ("", "Invalidierung"):
            if long_side:
                hard_break = cur.close < stop_level * (1 - DIP_FLOOR_PCT)
                flow_ok = _confirm_long()
            else:
                hard_break = cur.close > stop_level * (1 + DIP_FLOOR_PCT)
                flow_ok = _confirm_short()
            if flow_ok and not hard_break and pos.dip_buys < MAX_DIP_BUYS:
                # The strategy replaces the main stop even if this buy is rejected.
                stop_hit = False
            if flow_ok and not hard_break and pos.dip_buys < MAX_DIP_BUYS and _allow("dip"):
                nk = SignalType.NACHKAUF if long_side else SignalType.SHORT_NACHLEGEN
                signals.append(Signal(cur.ts, nk, cur.close, DIP_TRANCHE,
                                      f"Bedingter Nachkauf: Dip haelt, Order-Flow bestaetigt Trend ({pattern.name})",
                                      stop_ref=z.invalidation))
                pos.dip_buys += 1
                stop_hit = False                             # kein Stop diese Kerze
        # E44.3: der eigene Stop des Rueckkauf-Teils. Nur wenn der Hauptstop NICHT
        # ausloest - sonst geht die ganze Position, der Teil mit ihr. Unabhaengig vom
        # Schalter: Ein offener Teil behaelt seinen Stop, auch wenn ausbruch_ruecktest
        # inzwischen aus ist (ohne Schalter entsteht nie ein Teil, also bitgleich).
        _teil_stop, _teil_grund = False, None
        if pos.e42_teil_marke is not None and not stop_hit:
            _teil_marke = pos.e42_teil_marke
            _teil_stop, _teil_grund, _t_wartet, _t_zurueck = ruecktest_teil_stop(
                pos, cur, stop_rueckeroberung)
            if _t_wartet:
                _n = max(1, stop_rueckeroberung)
                _melde_boden = _teil_marke * (1 - DIP_FLOOR_PCT) if long_side \
                    else _teil_marke * (1 + DIP_FLOOR_PCT)
                _e42_melden("teil_wartet", _teil_marke, long_side,
                            kerze_nr=pos.e42_teil_wartet, von=_n,
                            noch=_n - pos.e42_teil_wartet + 1, boden=_melde_boden,
                            nur_rueckkauf=pos.entry_pct == 0)
            elif _t_zurueck:
                _e42_melden("teil_zurueck", _teil_marke, long_side,
                            nur_rueckkauf=pos.entry_pct == 0)
        if stop_hit:
            if _allow("stop"):
                st = SignalType.STOPLOSS if long_side else SignalType.SHORT_STOPLOSS
                if trail_note and trail_note != "Invalidierung":
                    reason = ("Nachgezogener Stop ({}) {:.0f} — Kerzenschluss {}, "
                              "Gewinn gesichert".format(trail_note, stop_level,
                                                        'darunter' if long_side else 'darueber'))
                elif stop_grund:
                    reason = stop_grund                          # E41: warum genau jetzt
                else:
                    reason = ("Kerzenschluss {} Invalidierung {:.0f}".format(
                        'unter' if long_side else 'ueber', stop_level)
                        + (" — harter Boden/Flow gekippt" if conditional_stop else ""))
                signals.append(Signal(cur.ts, st, stop_preis, 100, reason))
                # BUGFIX 2026-07-27: hier stand eine handgeschriebene Teil-Ruecksetzung, die
                # entry_ref/entry_pct/liq_exits/high_exits/liq_entries VERGESSEN hat. Folge:
                # (1) entry_pct wuchs ueber alle gestoppten Positionen hinweg immer weiter, der
                # Durchschnitts-Einstand blieb am Preis einer laengst geschlossenen Position
                # haengen -> der nachgezogene Stop (trail_stop) rechnete mit einem falschen
                # Break-even. (2) Die Zaehler liq_exits/high_exits/liq_entries liefen gegen ihr
                # Maximum und schalteten die zugehoerigen Mechanismen still ab, bis zufaellig
                # einmal ueber VERKAUF_REST geschlossen wurde. Jetzt derselbe Reset wie ueberall.
                _reset_position(pos)
                pos.last_stop_ts = cur.ts        # E13: Merker fuer die Sperrfrist (cooldown_h)
                _e42_ende()                      # E44.3: nach einem Stop wird nicht beobachtet
        elif _teil_stop and pos.entry_pct == 0:
            if _allow("part_full"):
                # E44.3: Die Position besteht NUR aus dem Rueckkauf (aus FLAT eroeffnet, seither
                # kein anderer Kauf) - dann ist der Stop des Teils ein vollstaendiger Ausstieg.
                # Sonst bliebe eine leere Position mit Zonen stehen, die spaeter "aufstockt".
                st = SignalType.STOPLOSS if long_side else SignalType.SHORT_STOPLOSS
                signals.append(Signal(cur.ts, st, cur.close, 100,
                                      ("Rueckkauf-Teil: " if long_side else "Short-Rueckteil: ")
                                      + _teil_grund))
                _reset_position(pos)
                pos.last_stop_ts = cur.ts
                _e42_ende()
        else:
            if _teil_stop:
                if _allow("part_stop"):
                    # E44.3: nur der Rueckkauf-Teil geht, der Rest behaelt seinen Stop.
                    ts_ = SignalType.RUECKKAUF_STOP if long_side else SignalType.SHORT_RUECKTEST_STOP
                    signals.append(Signal(cur.ts, ts_, cur.close, RUECKKAUF_TRANCHE,
                                          ("Rueckkauf-Teil: " if long_side else "Short-Rueckteil: ")
                                          + _teil_grund
                                          + " - der Rest der Position behaelt seinen Stop"))
                    pos.e42_teil_marke = None
                    pos.e42_teil_wartet, pos.e42_teil_wartet_inv = 0, None
                    pos.e42_teil_geprueft = None
            # Mehrtages-Kaufleiter (E9.5): neue Tiefkerze IN der Retracement-Zone (ueber
            # Invalidierung, unter 0.5) mit Flow-Bestaetigung -> kleine Tranche nachlegen.
            if buy_ladder and made_new_extreme and pos.buy_rungs < MAX_BUY_RUNGS \
                    and pos.state in (PosState.T1, PosState.CORE, PosState.FULL) \
                    and _darf_aufstocken():
                if long_side:
                    in_zone = z.invalidation < cur.low <= z.level_05
                    ladder_ok = _confirm_long()
                    nk = SignalType.NACHKAUF
                else:
                    in_zone = z.level_05 <= cur.high < z.invalidation
                    ladder_ok = _confirm_short()
                    nk = SignalType.SHORT_NACHLEGEN
                # E13: In einen ungesunden Abverkauf wird auch nicht NACHgekauft. Genau
                # das war Kaisers Beispiel 16.06.: Ersteinstieg, dann drei Nachkaeufe in
                # einen weiter fallenden Markt, dann Stop.
                if in_zone and ladder_ok and _healthy(long_side) and _allow("buy_ladder"):
                    signals.append(Signal(cur.ts, nk, cur.close, BUY_LADDER_TRANCHE,
                                          f"Mehrtages-Leiter: Nachkauf in die Schwaeche, Struktur intakt ({pattern.name})",
                                          stop_ref=z.invalidation))
                    pos.buy_rungs += 1
            # Konfluenz-Nachkauf an der Liquidationszone (E10.3): Fib-Zone UND historisches
            # Liquidations-Cluster fallen zusammen — Furkans "hier liegt auch das Golden
            # Pocket, da wuerde ich aufstocken". Struktur muss intakt sein (ueber der
            # Invalidierung, im Retracement-Bereich) und der Order-Flow bestaetigen.
            if liq_entry == "boost" and pos.liq_entries < MAX_LIQ_ENTRIES \
                    and pos.state in (PosState.T1, PosState.CORE, PosState.FULL) \
                    and _darf_aufstocken():
                if long_side:
                    treffer = _liq_hit(cur.low, "long")
                    in_struct = z.invalidation < cur.low <= z.level_05
                    flow_ok = _confirm_long()
                    nk = SignalType.NACHKAUF
                else:
                    treffer = _liq_hit(cur.high, "short")
                    in_struct = z.level_05 <= cur.high < z.invalidation
                    flow_ok = _confirm_short()
                    nk = SignalType.SHORT_NACHLEGEN
                if treffer is not None and in_struct and flow_ok and _allow("liq_buy"):
                    signals.append(Signal(cur.ts, nk, cur.close, LIQ_ENTRY_TRANCHE,
                                          f"Konfluenz: Fib-Zone + Liquidationszone {treffer:.0f} "
                                          f"— aufstocken ({pattern.name})",
                                          stop_ref=z.invalidation))
                    pos.liq_entries += 1
            # Teilverkauf an Liquidationen (E9.11): erst verkaufen, wenn der Kurs die
            # Liquidationszone erreicht — nicht schon am rechnerischen Fib-Ziel.
            if liq_exit != "off" and pos.liq_exits < MAX_LIQ_EXITS \
                    and pos.state in (PosState.T1, PosState.CORE, PosState.FULL) \
                    and _darf_teilverkaufen():
                # Long verkauft in Short-Liquidationen (Squeeze nach oben), Short in
                # Long-Liquidationen (Flush nach unten).
                seite = "short" if long_side else "long"
                grund = None
                if liq_exit in ("spike", "both") and liq_cascade(flow, seite):
                    grund = f"Liquidations-Kaskade laeuft ({pattern.name})"
                if grund is None and liq_exit in ("zone", "both"):
                    # NUR Kerzen VOR der aktuellen -> keine Kenntnis der Zukunft
                    lv = liq_levels(candles[:-1], flow[:-1], seite)
                    treffer = in_liq_zone(cur.high if long_side else cur.low, lv)
                    if treffer is not None:
                        grund = f"Liquidationszone {treffer:.0f} erreicht (historische Kaskade)"
                if grund is not None and _allow("liq_sell"):
                    lt = SignalType.TEILVERKAUF_LADDER if long_side else SignalType.SHORT_TP_LADDER
                    signals.append(Signal(cur.ts, lt, cur.close, LADDER_TRANCHE,
                                          f"Teilgewinn an Liquidationen: {grund}"))
                    pos.liq_exits += 1
            # Teilverkauf an der Widerstandszone des Gegen-Beins (E20). Das ist die Marke,
            # die Furkan als erste nennt, wenn eine Position im Plus laeuft — sie liegt
            # unter dem alten Hoch und wird deshalb frueher erreicht als high_exit.
            if widerstand_exit != "off" and pos.widerstand_exits < MAX_WIDERSTAND_EXITS \
                    and pos.state in (PosState.T1, PosState.CORE, PosState.FULL) \
                    and _darf_teilverkaufen():
                gz = gegen_zonen(candles, pivots, long_side, k_atr=k_atr)
                if gz is not None:
                    # Golden Pocket des Gegen-Beins; bei Long liegt gp_upper (0.618)
                    # unter gp_lower (0.65) — die Zone selbst ist die Marke.
                    lo, hi = sorted((gz.gp_upper, gz.gp_lower))
                    erreicht = (cur.high >= lo) if long_side else (cur.low <= hi)
                    # nur sinnvoll, wenn die Zone ueberhaupt vor uns liegt
                    davor = (lo > cur.open) if long_side else (hi < cur.open)
                    if erreicht and davor and _allow("resistance"):
                        wt = SignalType.TEILVERKAUF_LADDER if long_side else SignalType.SHORT_TP_LADDER
                        signals.append(Signal(cur.ts, wt, lo if long_side else hi,
                                              LADDER_TRANCHE,
                                              f"Teilgewinn an der Widerstandszone {lo:.0f}-{hi:.0f} "
                                              f"(Golden Pocket der Gegenbewegung)"))
                        pos.widerstand_exits += 1
            # Teilverkauf am letzten Hoch (E10.2): Kurs laeuft an das letzte bestaetigte
            # Pivot-Hoch heran -> dort sitzt das Angebot, ein Stueck davor raus.
            if high_exit != "off" and pos.high_exits < MAX_HIGH_EXITS \
                    and pos.state in (PosState.T1, PosState.CORE, PosState.FULL) \
                    and _darf_teilverkaufen():
                ref = candles[-2].close if len(candles) >= 2 else cur.open
                piv_hx = pivots if high_exit_hist != "live" \
                    else find_pivots(candles[-HIGH_EXIT_LIVE_KERZEN:], n=pivot_n)
                lvl = next_pivot_beyond(piv_hx, ref, long_side)
                if lvl is not None:
                    nah = (cur.high >= lvl * (1 - HIGH_EXIT_TOL)) if long_side \
                        else (cur.low <= lvl * (1 + HIGH_EXIT_TOL))
                    # "weak": nur verkaufen, wenn der Anlauf OHNE Spot-Nachfrage passiert
                    if long_side:
                        spot_traegt = len(flow) >= 3 and flow[-1].spot_cvd > flow[-3].spot_cvd
                    else:
                        spot_traegt = len(flow) >= 3 and flow[-1].spot_cvd < flow[-3].spot_cvd
                    ok = nah and (high_exit == "on" or not spot_traegt)
                    if ok and _allow("high"):
                        ht = SignalType.TEILVERKAUF_LADDER if long_side else SignalType.SHORT_TP_LADDER
                        zusatz = "" if high_exit == "on" else ", Anlauf ohne Spot-Nachfrage"
                        signals.append(Signal(cur.ts, ht, cur.close, LADDER_TRANCHE,
                                              f"Teilgewinn am letzten {'Hoch' if long_side else 'Tief'} "
                                              f"{lvl:.0f}{zusatz}"))
                        pos.high_exits += 1
                        # E44.3: ab jetzt die Marke beobachten (dieselbe wie high_exit)
                        signals[-1].reason += _e42_beobachten(lvl, long_side)
            # Upgrade T1 -> CORE: Kernposition im Golden Pocket (KAUF 2 / SHORT 2)
            if pos.state == PosState.T1 and _darf_aufstocken():
                in_gp = (z.gp_lower <= cur.low <= z.gp_upper) if long_side \
                    else (z.gp_upper <= cur.high <= z.gp_lower)
                if in_gp:
                    if long_side:
                        if _confirm_long() and _allow("upgrade_gp"):
                            signals.append(Signal(cur.ts, SignalType.KAUF_2, z.gp_upper,
                                                  TRANCHEN["CORE"],
                                                  f"Golden Pocket {z.gp_lower:.0f}-{z.gp_upper:.0f} + Bestaetigung ({pattern.name})",
                                                  stop_ref=z.invalidation))
                            pos.state = PosState.CORE
                    else:
                        if _confirm_short() and _allow("upgrade_gp"):
                            signals.append(Signal(cur.ts, SignalType.SHORT_2, z.gp_upper,
                                                  TRANCHEN["CORE"],
                                                  f"Golden Pocket {z.gp_upper:.0f}-{z.gp_lower:.0f} + Bestaetigung ({pattern.name})",
                                                  stop_ref=z.invalidation))
                            pos.state = PosState.CORE
            # Nachkauf am 0.786
            if pos.state in (PosState.T1, PosState.CORE) and _darf_aufstocken():
                touch = (cur.low <= z.level_0786) if long_side else (cur.high >= z.level_0786)
                if touch and _allow("upgrade_full"):
                    nk = SignalType.NACHKAUF if long_side else SignalType.SHORT_NACHLEGEN
                    # E41: Nach einer Rueckeroberung kommt der Kurs von UNTEN an die
                    # 0.786-Zone. Eine Limit-Order dort waere zum Eroeffnungskurs gefuellt
                    # worden, nicht zum Levelpreis - der Levelpreis waere teurer als
                    # alles, was erreichbar war. Nur wenn E41 eine Marke als geprueft
                    # gefuehrt hat; ohne E41 ist stop_geprueft immer None und nichts aendert
                    # sich (die Live-Zahlen bleiben dieselben).
                    preis_nk = z.level_0786
                    if pos.stop_geprueft is not None:
                        preis_nk = (min(preis_nk, cur.open) if long_side
                                    else max(preis_nk, cur.open))
                    signals.append(Signal(cur.ts, nk, preis_nk, TRANCHEN["FULL"],
                                          "0.786-Zone erreicht, Struktur intakt",
                                          stop_ref=z.invalidation))
                    pos.state = PosState.FULL
            # Gestaffelte Zwischen-Teilgewinne (E8.2): kleine Tranchen an 0.8/0.9-Ext
            # VOR dem 1.0-Ziel, hoechstens eine Stufe je Kerze (Leiter ueber Tage)
            if tp_ladder and pos.state in (PosState.T1, PosState.CORE, PosState.FULL) \
                    and pos.tp_rungs < len(LADDER_FACTORS) and _darf_teilverkaufen():
                rung_ext = z.ext_target(pos.retrace_extreme, LADDER_FACTORS[pos.tp_rungs])
                rung_hit = (cur.high >= rung_ext) if long_side else (cur.low <= rung_ext)
                if rung_hit and _allow("tp_ladder"):
                    lt = SignalType.TEILVERKAUF_LADDER if long_side else SignalType.SHORT_TP_LADDER
                    signals.append(Signal(cur.ts, lt, rung_ext, LADDER_TRANCHE,
                                          f"Leiter-Teilgewinn an Extension {LADDER_FACTORS[pos.tp_rungs]:.1f} ({rung_ext:.0f})"))
                    pos.tp_rungs += 1
            # Teilgewinne an Extensions. ziel=True: das sind die geplanten Ziele, nicht
            # die Zwischenverkaeufe — muster5_halten="leiter" laesst sie durch.
            if pos.state in (PosState.T1, PosState.CORE, PosState.FULL) \
                    and _darf_teilverkaufen(ziel=True):
                hit1 = (cur.high >= ext1) if long_side else (cur.low <= ext1)
                if hit1 and _allow("tp1"):
                    tp = SignalType.TEILVERKAUF_1 if long_side else SignalType.SHORT_TP_1
                    signals.append(Signal(cur.ts, tp, ext1, TRANCHEN["TP1"],
                                          f"Extension 1.0 erreicht ({ext1:.0f})"))
                    pos.state = PosState.TP1
            if pos.state == PosState.TP1 and _darf_teilverkaufen(ziel=True):
                hit2 = (cur.high >= ext2) if long_side else (cur.low <= ext2)
                if hit2 and _allow("tp2"):
                    tp = SignalType.TEILVERKAUF_2 if long_side else SignalType.SHORT_TP_2
                    signals.append(Signal(cur.ts, tp, ext2, TRANCHEN["TP2"],
                                          f"Extension 1.618 erreicht ({ext2:.0f})"))
                    pos.state = PosState.TP2
            # Rest schliessen bei Gegen-Muster/Divergenz nach TP1
            if pos.state in (PosState.TP1, PosState.TP2):
                exit_pat = (pattern in (Pattern.DERIVATE_PUMP, Pattern.SHORT_COVERING)) if long_side \
                    else (pattern in (Pattern.CAPITULATION_RESET, Pattern.GESUNDER_TREND))
                if exit_pat and not rest_halten and _allow("rest_pattern"):
                    ex = SignalType.VERKAUF_REST if long_side else SignalType.SHORT_COVER_REST
                    signals.append(Signal(cur.ts, ex, cur.close, 20,
                                          f"Gegen-Muster am Ziel: {pattern.name}"))
                    _reset_position(pos)
                    # E44.3: Rest-Verkauf (kein Stop) -> naechstes Pivot jenseits beobachten
                    signals[-1].reason += _e42_beobachten(
                        next_pivot_beyond(_piv_marke(), cur.close, long_side), long_side)
            # Rest freigeben, wenn die Struktur veraltet ist (E9.9). Nur nach Teilgewinnen
            # (TP1/TP2) — beim Positionsaufbau bleibt der Stop zustaendig.
            if release_stale_rest and pos.state in (PosState.TP1, PosState.TP2) \
                    and imp is not None and pos.zones is not None:
                alt = (pos.zones.impulse.start.ts, pos.zones.impulse.end.ts)
                if (imp.start.ts, imp.end.ts) != alt and _allow("rest_stale"):
                    ex = SignalType.VERKAUF_REST if long_side else SignalType.SHORT_COVER_REST
                    signals.append(Signal(cur.ts, ex, cur.close, 20,
                                          f"Struktur veraltet: neuer Impuls bestaetigt "
                                          f"({imp.start.price:.0f}->{imp.end.price:.0f}) — Rest freigegeben"))
                    _reset_position(pos)
                    signals[-1].reason += _e42_beobachten(                    # E44.3
                        next_pivot_beyond(_piv_marke(), cur.close, long_side), long_side)
            # Warnung waehrend offener Long-Position
            if long_side and pos.state in (PosState.T1, PosState.CORE, PosState.FULL) \
                    and pattern == Pattern.DERIVATE_PUMP:
                signals.append(Signal(cur.ts, SignalType.WARNUNG, cur.close, 0,
                                      "Derivate-Pump: anfaellig fuer Long-Flush"))

    # E21: Neuer Einstieg, waehrend der Rest noch laeuft. Steht NACH dem
    # Positions-Management (ein Stop in derselben Kerze hat Vorrang) und VOR der
    # Einstands-Fortschreibung — sonst wuerde die neue Tranche gleich wieder
    # ueberschrieben, wenn der alte Bestand zurueckgesetzt wird.
    # E30.2b (05.09.2026): _darf_aufstocken() gilt auch hier. Der Neustart laeuft NACH
    # dem Positions-Management im selben Aufruf und konnte deshalb in dieselbe Kerze
    # fallen wie ein Teilverkauf — ein Gegengeschaeft, das no_flip bis dahin nicht sah,
    # weil es nur die Nachkauf-Pfade absicherte. Betrifft die Live-Einstellung
    # unabhaengig von zonen_nachziehen; im gemessenen Fenster war es bisher Zufall,
    # dass die Live-Zeile 0 Gegengeschaefte zeigte. Der Neustart wird dadurch nur um
    # eine Kerze verschoben, nicht verhindert.
    if (neustart_mit_rest and pos.state in (PosState.TP1, PosState.TP2)
            and (imp is not None or _imp_1d is not None) and _cooldown_ok()
            and _darf_aufstocken()):
        _bestand = (pos.entry_ref, pos.entry_pct)
        _vorher = pos.state
        if imp is not None:
            _versuche_einstieg()
        if (pos.state == _vorher and _imp_1d is not None
                and not gleiches_bein(imp, _imp_1d)):
            _versuche_einstieg(_imp_1d)          # E23, auch hier 4h zuerst
        if pos.state != _vorher:
            # Einstieg hat stattgefunden: neuer Zyklus, aber der alte Bestand bleibt im
            # Durchschnitts-Einstand — die neue Tranche wird gleich dazugerechnet.
            pos.entry_ref, pos.entry_pct = _bestand
            pos.tp_rungs = pos.buy_rungs = pos.dip_buys = 0
            pos.liq_entries = pos.liq_exits = pos.high_exits = pos.widerstand_exits = 0
            pos.ziel_extrem = None

    # --- E44.3 (E42): die beobachtete Marke in dieser Kerze -------------------------------
    # NACH Stop, Teilverkaeufen und Einstiegen: Ein Stop in dieser Kerze hat die
    # Beobachtung schon beendet; eine in dieser Kerze BEGONNENE Beobachtung wird erst ab
    # der naechsten Kerze bewertet (ihr Ausbruch-Fall ist in _e42_beobachten erledigt).
    if pos.e42_marke is not None and pos.e42_start_ts != cur.ts:
        _m, _lang = pos.e42_marke, pos.e42_richtung == "LONG"
        _seit = None if pos.e42_ausbruch_ts < 0 else kerzen_seit(
            candles, pos.e42_ausbruch_ts, ruecktest_fenster + 1)
        _was = ruecktest_schritt(_m, _lang, _seit, cur, ruecktest_fenster)
        if _was == "ausbruch":
            pos.e42_ausbruch_ts = cur.ts
            _e42_melden("ausbruch", _m, _lang, fenster=ruecktest_fenster,
                        zone=_e42_zone(_m, _lang), tranche=RUECKKAUF_TRANCHE)
        elif _was == "gescheitert":
            pos.e42_ausbruch_ts = -1                     # auf einen neuen Ausbruch warten
            _e42_melden("gescheitert", _m, _lang)
        elif _was == "verfallen":
            _e42_ende()
            _e42_melden("verfallen", _m, _lang, fenster=ruecktest_fenster)
        elif _was == "ruecktest":
            _flat = pos.state == PosState.FLAT
            _hindernis = None
            if not (bias_long if _lang else bias_short):
                _hindernis = "die Richtung ist in den Einstellungen abgeschaltet"
            elif not _flat and pos.direction != ("LONG" if _lang else "SHORT"):
                _hindernis = "eine Gegenposition ist offen"
            elif _m == pos.e42_gekauft:
                _hindernis = "auf diese Marke wurde schon zurueckgekauft"
            elif bestand_nach(_bestand_start, signals) >= 100:
                _hindernis = "die Engine ist schon voll investiert"
            elif _flat and (imp is None or imp.up != _lang):
                _hindernis = "es gibt kein Bein fuer die Ziele"
            if _hindernis:
                _e42_ende()
                _e42_melden("ohne_kauf", _m, _lang, grund=_hindernis)
            elif not _darf_aufstocken():
                # no_flip/E41: in DIESER Kerze kein Kauf - das Fenster laeuft weiter
                if _seit is not None and _seit >= ruecktest_fenster:
                    _e42_ende()
                    _e42_melden("verfallen", _m, _lang, fenster=ruecktest_fenster)
            elif _allow("e42_buy"):
                if _flat:
                    # Aus FLAT: neue Position auf dem aktuellen Bein, damit die vorhandene
                    # Extension-Logik die Ziele stellt (vom Ruecktest-Extrem aus).
                    pos.direction = "LONG" if _lang else "SHORT"
                    pos.state, pos.zones = PosState.T1, fib_zones(imp)
                    pos.retrace_extreme = cur.low if _lang else cur.high
                rt = SignalType.RUECKKAUF if _lang else SignalType.SHORT_RUECKTEST
                gegen = "unter" if _lang else "ueber"
                signals.append(Signal(
                    cur.ts, rt, cur.close, RUECKKAUF_TRANCHE,
                    "{} {:.0f}, Ruecktest gehalten ({} {:.0f}, Schluss {:.0f}) - "
                    "Stop fuer diesen Teil bei Schluss {} {:.0f}".format(
                        "Ausbruch ueber" if _lang else "Durchbruch unter", _m,
                        "Tief" if _lang else "Hoch",
                        cur.low if _lang else cur.high, cur.close, gegen, _m),
                    stop_ref=_m))
                pos.e42_teil_marke = _m
                pos.e42_teil_wartet, pos.e42_teil_wartet_inv = 0, None
                pos.e42_teil_geprueft = None
                pos.e42_gekauft = _m
                _e42_ende()

    # E34: die Ampel darf die GROESSE eines Einstiegs aendern - mehr nicht.
    # Bewusst HIER, nach allen Einstiegspfaden und VOR der Einstands-Rechnung:
    # der Zustandsautomat haengt am Signaltyp, nicht an der Tranche, also stoert eine
    # kleinere Tranche den Ablauf nicht. Ausstiege bleiben unberuehrt - ein Ausstieg
    # muss immer durchkommen. Default "off": live und in jeder bestehenden Gitterzeile
    # passiert hier nichts, auch nicht die Rechenarbeit.
    if ampel_filter != "off" and any(s.type in _ENTRY_TYPES for s in signals):
        _lage_jetzt = None if ampel_filter == "immer" else lage_bericht(
            candles, flow, imp=imp,
            pos_impulse=_pos_imp_vorher,
            pattern=pattern, trend_period=trend_ema)
        _amp_cache: dict = {}

        def _halbieren(long_side: bool) -> bool:
            if ampel_filter == "immer":
                return True                      # Nullhypothese: gar keine Ampel
            if long_side not in _amp_cache:
                _amp_cache[long_side] = ampel(_lage_jetzt, long_side=long_side)
            a = _amp_cache[long_side]
            if a is None:
                return False                     # keine Aussage -> nichts aendern
            return a["stufe"] == ("guenstig" if ampel_filter == "gross" else "unguenstig")

        kuerze_einstiege(signals, _halbieren)

    if _execution_gate is not None:
        trades = [s for s in signals if s.type != SignalType.WARNUNG]
        assert len(trades) == len(_actions), (trades, _actions)
        for sig, action in zip(trades, _actions):
            sig.execution_action = action
            if _execution_sizes is not None and action in _execution_sizes:
                sig.tranche_pct = _execution_sizes[action]

    # Historical signal reference, NOT remaining-inventory cost. V1 replaces this
    # anchor from filled base lots in Decision.confirm before the next decision.
    # The live observer has no manual fill evidence and must not invent any.
    for s in signals:
        if s.type in _ENTRY_TYPES and s.tranche_pct > 0:
            tot = pos.entry_pct + s.tranche_pct
            base = pos.entry_ref if pos.entry_ref is not None else s.price
            pos.entry_ref = (base * pos.entry_pct + s.price * s.tranche_pct) / tot
            pos.entry_pct = tot

    # E18.3: Mit dem ERSTEN realisierten Teilgewinn wird die Zielreferenz festgehalten.
    if freeze_targets and pos.ziel_extrem is None and pos.retrace_extreme is not None \
            and any(s.type in _TEILVERKAUF_TYPES for s in signals):
        pos.ziel_extrem = pos.retrace_extreme

    # E44.3: investierter Anteil nach dieser Kerze (immer gefuehrt, damit er stimmt,
    # wenn ausbruch_ruecktest spaeter eingeschaltet wird; aendert kein Signal).
    pos.bestand_pct = bestand_nach(_bestand_start, signals)

    if pos.direction == "LONG" and pos.zones is not None and pos.valid_stop is None:
        pos.valid_stop, pos.valid_stop_reason = pos.zones.invalidation, "Invalidierung"
    if _execution_gate is None and pos.direction == "LONG" and pos.zones is not None:
        # A signal-state TP transition arms the prospective plan now. In V1 this
        # must instead wait for the fill AND its cost feedback (Decision.confirm).
        resolve_stop(pos, cur, pivots, trail_stop=trail_stop, be_im_plus=be_im_plus, commit=True)

    pos.last_signal_ts = cur.ts
    return signals
