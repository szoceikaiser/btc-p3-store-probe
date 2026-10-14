"""Coinalyze-Datenanbindung (E9.1): historisches OI, Funding, Liquidationen.

Nur Standardbibliothek (urllib) — laeuft ohne Zusatzpakete auf GitHub Actions.
Key aus Umgebungsvariable COINALYZE_API_KEY (GitHub-Secret). Doku:
https://api.coinalyze.net/v1/doc/  ·  Rate-Limit 40 Abrufe/Min.

STAND E9.1: Dies ist zunaechst eine PROBE — sie holt eine kleine Stichprobe und schreibt
das ROHE Antwortformat nach site/data/coinalyze_probe.json, damit das echte Format
verifiziert werden kann, bevor der Parser gebaut wird. Kein Blind-Parsen.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://api.coinalyze.net/v1"
# ACHTUNG, hier stand bis 19.09.2026 etwas Falsches: ".A" ist NICHT "aggregiert".
# In /exchanges steht A fuer BINANCE (per Probe 19.09.2026 nachgeprueft; die Liste hat
# 28 Boersen und KEINEN Code fuer ein Aggregat). Alles, was die Engine von Coinalyze
# bezieht — Open Interest, Funding, Liquidationen, Futures-CVD, Long-Short — kommt
# also von einer einzigen Boerse. Wer das aendern will, muss mehrere Symbole holen und
# selbst zusammenrechnen; siehe spot_probe() weiter unten.
SYMBOL = "BTCUSDT_PERP.A"        # BTC/USDT-Perpetual auf Binance (A = Binance)
INTERVAL = "4hour"               # beim ersten Lauf verifizieren (evtl. "H4"/"4h")
ROOT = Path(__file__).resolve().parent.parent

# Endpoints (Name -> Pfad); beim ersten Live-Lauf gegen die Antwort abgleichen.
ENDPOINTS = {
    "open_interest": "open-interest-history",
    "funding": "funding-rate-history",
    "liquidations": "liquidation-history",
}

# E15: Was bietet Coinalyze AUSSER dem, was wir schon nutzen? Zwei offene Luecken:
#   (1) Futures-CVD fehlt komplett — fapi.binance.com sperrt US-Runner (HTTP 451).
#       Wenn eine dieser Historien Taker-Kauf-/Verkaufsvolumen liefert, ist Muster 2
#       (Derivate-Pump) erstmals mit echten Daten pruefbar statt ueber Hilfsmerkmale.
#   (2) Long-Short-Verhaeltnis — bei Furkan Teil der Positionierungs-Einschaetzung.
# Kein Blind-Parsen (Projektregel): erst die ROHE Antwort holen und ansehen, dann bauen.
# Namen sind Kandidaten aus der Coinalyze-Doku; nicht existierende geben 404 und werden
# als solche protokolliert — der Lauf scheitert daran nicht.
KANDIDATEN = {
    "ohlcv": "ohlcv-history",
    "buy_sell_volume": "buy-sell-volume-history",
    "long_short_ratio": "long-short-ratio-history",
    "predicted_funding": "predicted-funding-rate-history",
    "future_markets": "future-markets",
    "exchanges": "exchanges",
}


def build_url(endpoint: str, params: dict) -> str:
    """Reine URL-Konstruktion (offline testbar)."""
    return f"{BASE}/{endpoint}?{urllib.parse.urlencode(params)}"


def get_json(endpoint: str, params: dict, api_key: str,
             opener=urllib.request.urlopen, timeout: int = 30):
    """GET auf einen Coinalyze-Endpoint; Key im Header. `opener` injizierbar (Tests)."""
    req = urllib.request.Request(build_url(endpoint, params),
                                 headers={"api_key": api_key})
    with opener(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_history(endpoint: str, api_key: str, symbol: str = SYMBOL,
                  interval: str = INTERVAL, days: int = 7,
                  frm: int | None = None, to: int | None = None, **kw):
    """Holt eine History (from/to in Unix-Sekunden). Ohne frm/to: letzte `days` Tage."""
    if to is None:
        to = int(time.time())
    if frm is None:
        frm = to - days * 86400
    params = {"symbols": symbol, "interval": interval,
              "from": frm, "to": to, "convert_to_usd": "true"}
    return get_json(endpoint, params, api_key, **kw)


# --------------------------------------------------- Parser (echtes Format)
# Antwortformat (per Test-Lauf 2026-07-24 bestaetigt): Liste je Symbol mit
#   {"symbol": "...", "history": [ {t, o, h, l, c}, ... ]}
# t = Open-Time in Unix-SEKUNDEN. open-interest-history & funding-rate-history sind
# OHLC (Close = Wert je Kerze). liquidation-history: {t, l, s} mit l=Long-Liq (USD),
# s=Short-Liq (USD). convert_to_usd=true -> OI/Liq in USD.


def _history_points(data, symbol: str = SYMBOL) -> list:
    """Zieht das history-Array fuer das Symbol aus der Coinalyze-Antwort."""
    for item in data or []:
        if isinstance(item, dict) and item.get("symbol") == symbol \
                and isinstance(item.get("history"), list):
            return item["history"]
    for item in data or []:                              # Fallback: erstes history-Array
        if isinstance(item, dict) and isinstance(item.get("history"), list):
            return item["history"]
    return []


def oi_by_ts(api_key: str, **kw) -> dict:
    """{Open-Time_ms: OI_Close_USD} aus open-interest-history (OHLC -> Close)."""
    pts = _history_points(fetch_history("open-interest-history", api_key, **kw))
    return {int(p["t"]) * 1000: float(p["c"]) for p in pts if "t" in p and "c" in p}


def funding_by_ts(api_key: str, **kw) -> dict:
    """{Open-Time_ms: Funding_Close} aus funding-rate-history (Skalierung siehe Wiring)."""
    pts = _history_points(fetch_history("funding-rate-history", api_key, **kw))
    return {int(p["t"]) * 1000: float(p["c"]) for p in pts if "t" in p and "c" in p}


def liquidations_by_ts(api_key: str, **kw) -> dict:
    """{Open-Time_ms: (Long-Liq_USD, Short-Liq_USD)} aus liquidation-history (l, s)."""
    pts = _history_points(fetch_history("liquidation-history", api_key, **kw))
    return {int(p["t"]) * 1000: (float(p.get("l", 0.0)), float(p.get("s", 0.0)))
            for p in pts if "t" in p}


def fut_delta_by_ts(api_key: str, **kw) -> dict:
    """{Open-Time_ms: Taker-Delta je Kerze} aus ohlcv-history des Futures-Marktes (E16).

    Antwortformat per Probe 2026-07-28 bestaetigt: {t, o, h, l, c, v, bv, tx, btx} mit
    v = Gesamtvolumen der Kerze, bv = davon Taker-KAEUFE. Daraus folgt
        Delta = Kaeufe - Verkaeufe = bv - (v - bv) = 2*bv - v
    — dieselbe Formel, die main.py fuer die Binance-Spotkerzen verwendet.

    DAS SCHLIESST DIE GROESSTE DATENLUECKE DES PROJEKTS: Futures-CVD gab es bisher nicht
    (fapi.binance.com sperrt US-Runner mit HTTP 451), deshalb war in classify_pattern der
    Zweig `if has_fut:` seit dem ersten Tag toter Code und Muster 2 (Derivate-Pump) lief
    nur ueber Ersatzmerkmale. Coinalyze liefert es mit Historie.

    NICHT aggregiert (Korrektur 19.09.2026): wie bei OI, Funding und Liquidationen ist
    das Symbol BTCUSDT_PERP.A der Binance-Markt, nicht ein Boersen-Durchschnitt.

    EINHEIT: Die Werte sind Kontrakt-/Basiswert-Mengen (BTC), nicht USD — anders als das
    Spot-Delta. Fuer classify_pattern ist das unerheblich: dort geht das kumulierte Delta
    nur ueber `_slope()` ein, also als RELATIVE Veraenderung, und der Vergleich
    `spot <= fut / 3` stellt zwei solche relativen Werte gegenueber. Die Einheit kuerzt
    sich heraus. Wer die Reihe je absolut auswerten will, muss sie erst mit dem Preis
    multiplizieren.
    """
    pts = _history_points(fetch_history("ohlcv-history", api_key, **kw))
    out = {}
    for p in pts:
        if "t" in p and "v" in p and "bv" in p:
            out[int(p["t"]) * 1000] = 2.0 * float(p["bv"]) - float(p["v"])
    return out


def long_short_by_ts(api_key: str, **kw) -> dict:
    """{Open-Time_ms: Long-Anteil in Prozent} aus long-short-ratio-history (E16).

    Format per Probe: {t, r, l, s} mit r = Verhaeltnis long/short, l/s = Anteile in
    Prozent (Beispiel 2026-07-28: r=1.867, l=65.12, s=34.88). Wir speichern den
    Long-Anteil `l`, weil er ohne Division auskommt und direkt lesbar ist:
    ueber 50 = mehrheitlich long positioniert. Furkan nutzt diese Groesse zur
    Einschaetzung der Positionierung ("Longueberhang").
    """
    pts = _history_points(fetch_history("long-short-ratio-history", api_key, **kw))
    return {int(p["t"]) * 1000: float(p["l"]) for p in pts if "t" in p and "l" in p}


# ------------------------------------------- E37: Spot-Maerkte (Kaisers Frage 19.09.2026)
# AUSLOESER: Furkan aggregiert sein Spot-CVD auf Velo ueber Binance, Coinbase, Bybit und
# OKX und betont es ausdruecklich ("Wir schauen nicht nur auf eine Boerse"). Die
# Velo-API scheidet aus (199 $/Monat), und ausser Binance liefert keine Boerse das
# Taker-Kaufvolumen fertig in den Kerzen — Coinalyze ist der einzige gangbare Weg.
#
# BEFUND DER ERSTEN PROBE (Lauf 19.09.2026, 17:50 UTC) — und er ist groesser als die
# Frage war: In /exchanges steht "A" fuer BINANCE. Es gibt in der ganzen Liste KEINEN
# Code fuer ein Aggregat (28 Boersen, 16 davon mit Maerkten, kein unbekannter Code).
# Damit ist der Kommentar an SYMBOL seit E9.1 falsch: BTCUSDT_PERP.A ist Binance,
# nicht "aggregiert ueber Boersen". Open Interest, Funding, Liquidationen, Futures-CVD
# und Long-Short-Verhaeltnis kommen also ALLE von einer einzigen Boerse — genau das,
# wovon Furkan abraet. Aggregieren muessen wir selbst.
#
# WAS DIE ERSTE PROBE SCHON BEWIESEN HAT: ohlcv-history liefert fuer Spot-Symbole
# 'v' (Gesamtvolumen) und 'bv' (Taker-Kaeufe), also alles fuer Delta = 2*bv - v,
# und reicht rund 334 Tage zurueck (2005 4h-Punkte). Das genuegt fuer einen Backtest.
#
# WAS DIESE ZWEITE PROBE KLAERT — wieder nur fragen, nicht bauen:
#   1. Welche BTC-Spotmaerkte gegen Dollar gibt es auf Furkans vier Boersen, und
#      welches Symbol heisst dort wie? (Die Schreibweise ist je Boerse anders:
#      "BTCARS.A" auf Binance, aber "sBTCEUR.6" auf Bybit.)
#   2. Nimmt ein Abruf MEHRERE Symbole auf einmal? Nur dann ist Aggregieren billig.
#   3. Reicht die Historie auf allen vier gleich weit? Der kuerzeste bestimmt das
#      Backtest-Fenster.
BOERSEN_CODES = {"A": "Binance", "C": "Coinbase", "6": "Bybit", "3": "OKX"}
DOLLAR_QUOTES = ("USDT", "USD", "USDC", "FDUSD", "USDE")   # Vorrang in dieser Reihenfolge
SPOT_REICHWEITE_TAGE = 365        # so weit zurueck fragen, um die ECHTE Grenze zu sehen


def _ablehnungsgrund(e, codes) -> str | None:
    """Warum dieser Markt nicht in Frage kommt — oder None, wenn er passt.

    Streng nach den Feldern, die der Lauf vom 19.09.2026 gezeigt hat:
    {symbol, exchange, symbol_on_exchange, base_asset, quote_asset, has_buy_sell_data}.
    Der Grund wird MITGESCHRIEBEN statt nur ein Ja/Nein, damit sich nachlesen laesst,
    warum eine Boerse leer ausgeht (E37.1, Frage nach OKX). Ein stiller Filter, der
    ganze Boersen verschluckt, ohne zu sagen warum, ist genau die Sorte Fehler, die
    dieses Vorhaben vermeiden soll.
    """
    if not isinstance(e, dict):
        return "kein Eintrag"
    if e.get("base_asset") != "BTC":
        # Wichtigster Fall: WLDBTC hat BTC im Namen, aber als GEGENwaehrung.
        return f"Basiswert ist {e.get('base_asset')!r}, nicht BTC"
    if e.get("exchange") not in codes:
        return f"Boerse {e.get('exchange')!r} gehoert nicht zu den gesuchten"
    if e.get("quote_asset") not in DOLLAR_QUOTES:
        return f"Gegenwaehrung {e.get('quote_asset')!r} ist kein Dollar"
    if not e.get("has_buy_sell_data"):
        return "keine Kauf-/Verkaufsdaten"
    return None


def _ist_btc_dollar_markt(e, codes) -> bool:
    """BTC gegen Dollar, auf einer der gesuchten Boersen, mit Kauf-/Verkaufsdaten."""
    return _ablehnungsgrund(e, codes) is None


def _kandidaten_je_boerse(maerkte: list, codes=BOERSEN_CODES) -> dict:
    """ALLE passenden Symbole je Boerse — die Auswahl trifft danach das Volumen.

    E37.1, und der Grund steht in Zahlen fest: Die Vorgaengerfassung nahm global USDT
    vor USD. Auf Coinbase ist der USDT-Markt aber winzig — 2,2 BTC in vier Stunden
    gegen 1257 BTC auf Binance, also 0,17 % der Summe. Das "Aggregat" waere faktisch
    Binance plus Bybit gewesen. Welcher Markt der groesste ist, entscheidet deshalb
    nicht mehr eine geratene Rangfolge, sondern das gemessene Volumen.
    """
    out: dict = {}
    for e in maerkte:
        if _ablehnungsgrund(e, codes) is None:
            out.setdefault(e["exchange"], []).append(e)
    return out


def _abgelehnte_je_boerse(maerkte: list, codes=BOERSEN_CODES) -> dict:
    """Je Boerse die BTC-Maerkte, die NICHT durchkamen, mit Grund (Diagnose)."""
    out: dict = {}
    for e in maerkte:
        if not isinstance(e, dict) or e.get("exchange") not in codes:
            continue
        grund = _ablehnungsgrund(e, codes)
        if grund is None:
            continue
        # Nur was ueberhaupt mit BTC zu tun hat — sonst waere die Liste endlos.
        if e.get("base_asset") != "BTC" and "BTC" not in str(e.get("symbol", "")).upper():
            continue
        out.setdefault(e["exchange"], []).append(
            {"symbol": e.get("symbol"), "quote": e.get("quote_asset"),
             "base": e.get("base_asset"), "grund": grund})
    return out


def _groesster_je_boerse(kandidaten: dict, reihen: dict, codes=BOERSEN_CODES) -> dict:
    """Je Boerse das Symbol mit dem groessten belegten USD-Umsatz im Fenster.

    `reihen` kommt aus _pruefe_symbole(); massgeblich ist dort 'summe_v'. Symbole
    ohne brauchbare Reihe (keine Antwort, kein 'v'/'bv') scheiden aus — ein Markt,
    den wir nicht lesen koennen, darf nicht gewinnen, nur weil er zuerst dasteht.
    """
    out: dict = {}
    for code, liste in kandidaten.items():
        bester = None
        for e in liste:
            sym = e.get("symbol")
            r = reihen.get(sym) or {}
            if not r.get("hat_v_und_bv"):
                continue
            summe = r.get("summe_v_usd")
            if summe is None or not math.isfinite(summe) or summe <= 0:
                continue
            if bester is None or summe > bester["summe_v_usd"]:
                bester = {"symbol": sym, "quote": e.get("quote_asset"),
                          "an_der_boerse": e.get("symbol_on_exchange"),
                          "boerse": codes.get(code, code),
                          "summe_v": r.get("summe_v"),
                          "summe_v_usd": summe,
                          "volumen_einheit": r.get("volumen_einheit"),
                          "umrechnung": r.get("umrechnung"),
                          "punkte": r.get("punkte"),
                          "reichweite_tage": r.get("reichweite_tage")}
        if bester is not None:
            out[code] = bester
    return out


def _einheit_einschaetzen(v_letzte: float | None, kurs: float | None) -> str:
    """Ist 'v' in BTC oder in Dollar? Aus der Groessenordnung abgelesen.

    Eine 4h-Kerze auf einer grossen Boerse bewegt Hunderte bis Tausende BTC, in Dollar
    waeren das zweistellige Millionen. Wer beide Einheiten mischt und summiert, bekommt
    eine Zahl, die voellig normal aussieht und Unsinn ist — deshalb steht die
    Einschaetzung samt Rechnung in der Probe, statt vorausgesetzt zu werden.
    """
    if not v_letzte or not kurs:
        return "nicht einschaetzbar (keine Werte)"
    in_usd = v_letzte * kurs
    if v_letzte < 100_000:
        return (f"BTC (v={v_letzte:,.1f} BTC entspraeche {in_usd:,.0f} $ Umsatz — "
                "plausibel fuer 4 Stunden)")
    return (f"vermutlich schon Dollar (v={v_letzte:,.0f}; als BTC waeren das "
            f"{in_usd:,.0f} $ und damit unrealistisch)")


SYMBOLE_JE_ABRUF = 6     # Blockgroesse; die Grenze von Coinalyze ist nicht dokumentiert
PAUSE_JE_BLOCK = 1.6     # Sekunden zwischen Bloecken (Rate-Limit 40 Abrufe/Min)
MAX_VERSUCHE = 4         # Wiederholungen bei HTTP 429
MAX_WARTE = 30.0         # Sekunden, laenger wird nicht gewartet


def _mit_wiederholung(hole, was: str):
    """Fuehrt `hole()` aus und wiederholt bei HTTP 429, wie die Antwort es verlangt.

    BEFUND 20.09.2026, 10:52 UTC: Der Derivate-Vergleich fiel mit
    `429 Too Many Requests. See the "Retry-After" header.` aus. Die Doku nennt
    40 Abrufe je Minute, und so viele waren es nicht — aber offenbar zaehlt Coinalyze
    je SYMBOL, nicht je Anfrage: Bis zu diesem Punkt hatte der Lauf rund 39 Symbole
    abgefragt, und die naechsten vier kippten ihn.

    Die Antwort sagt selbst, was zu tun ist. Bis 20.09. wurde dieser Hinweis ignoriert
    und der ganze Vergleich stattdessen fallengelassen.
    """
    for versuch in range(MAX_VERSUCHE):
        try:
            return hole()
        except urllib.error.HTTPError as e:
            if e.code != 429 or versuch == MAX_VERSUCHE - 1:
                raise
            kopf = e.headers.get("Retry-After") if e.headers else None
            try:
                warte = float(kopf)
            except (TypeError, ValueError):
                warte = PAUSE_JE_BLOCK * (2 ** versuch)     # sonst: verdoppeln
            warte = min(max(warte, PAUSE_JE_BLOCK), MAX_WARTE)
            print(f"  Coinalyze 429 bei {was} — warte {warte:.1f}s "
                  f"(Versuch {versuch + 2}/{MAX_VERSUCHE})")
            time.sleep(warte)
    raise RuntimeError("unerreichbar")


def _reihe_auswerten(eintrag: dict, markt: dict | None = None,
                     fx_je_quote: dict | None = None) -> dict:
    """Symbolreihe und nur belegbar nach USD umgerechneten Umsatz beschreiben."""
    punkte = eintrag.get("history") or []
    felder = sorted({k for p in punkte if isinstance(p, dict) for k in p})
    zeiten = [int(p["t"]) for p in punkte if isinstance(p, dict) and "t" in p]
    # summe_v ist die Zahl, an der ab E37.1 die Auswahl haengt: welcher Markt einer
    # Boerse der groesste ist, wird gemessen und nicht mehr geraten.
    summe_v = sum(float(p["v"]) for p in punkte
                  if isinstance(p, dict) and isinstance(p.get("v"), (int, float)))
    markt = markt or {}
    denom = markt.get(DENOM_FELD)
    quote = markt.get("quote_asset")
    basis = markt.get("base_asset")
    einheit = ("BTC" if denom in ("BASE_ASSET", "BTC") and basis == "BTC"
               else "USD" if denom in ("QUOTE_ASSET", "USD") and quote == "USD"
               else quote if denom == "QUOTE_ASSET" else None)
    usd_werte = []
    grund = None
    if not einheit:
        grund = "Instrument-Denominierung nicht belegt"
    for p in punkte:
        if grund:
            break
        if not isinstance(p, dict) or not all(k in p for k in ("t", "v", "bv")):
            grund = "Volumenpunkt ohne t/v/bv"
            break
        v = p["v"]
        if not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            grund = "ungueltiges Volumen"
            break
        faktor = 1.0
        if einheit == "BTC":
            kurs = p.get("c")
            if not isinstance(kurs, (int, float)) or not math.isfinite(kurs) or kurs <= 0:
                grund = "BTC/Quote-Schlusskurs fehlt"
                break
            faktor = kurs
        if quote != "USD":
            kursreihe = (fx_je_quote or {}).get(quote, {})
            fx = kursreihe.get(int(p["t"]))
            if not isinstance(fx, (int, float)) or not math.isfinite(fx) or fx <= 0:
                grund = f"{quote}/USD-Umrechnung fehlt"
                break
            faktor *= fx
        usd_werte.append(v * faktor)
    if not punkte:
        grund = "keine Volumenpunkte"
    letzter = punkte[-1] if punkte else None
    return {
        "punkte": len(punkte),
        "felder": felder,
        "hat_v_und_bv": ("v" in felder and "bv" in felder),
        "summe_v": round(summe_v, 2),
        "summe_v_usd": round(sum(usd_werte), 2) if not grund else None,
        "volumen_einheit": einheit,
        "umrechnung": ("v * BTC/Quote-Schlusskurs * Quote/USD je Punkt" if einheit == "BTC"
                        else "v * Quote/USD je Punkt" if einheit else None),
        "nicht_auswertbar": grund,
        "reichweite_tage": round((max(zeiten) - min(zeiten)) / 86400.0, 1)
                           if len(zeiten) > 1 else 0.0,
        "von": time.strftime("%Y-%m-%d %H:%M", time.gmtime(min(zeiten))) if zeiten else "",
        "einheit": _einheit_einschaetzen(
            (letzter or {}).get("v"), (letzter or {}).get("c")),
        "letzter_punkt": letzter,
    }


AUSWAHL_TAGE = 30        # Fenster fuer die Rangfolge-Messung (siehe _hole_reihen_roh)


def _hole_reihen_roh(api_key: str, symbole: list, endpoint: str = "ohlcv-history",
                     tage: int = SPOT_REICHWEITE_TAGE, pause: float | None = None,
                     **kw) -> tuple:
    """Holt alle Symbole in Bloecken; gibt (rohe Eintraege, Blockprotokoll) zurueck.

    Coinalyze nimmt den Parameter `symbols` (Mehrzahl); der Lauf vom 19.09.2026 hat
    belegt, dass je Symbol eine eigene Reihe zurueckkommt. Wie viele Symbole ein Abruf
    vertraegt, steht nicht in der Doku — deshalb Bloecke zu SYMBOLE_JE_ABRUF mit Pause
    dazwischen (Rate-Limit 40/Min). Faellt ein Block aus, wird er als Fehler vermerkt
    und die uebrigen laufen weiter; ein stiller Totalausfall waere schlimmer als eine
    Luecke, die man sieht.

    Probe (_pruefe_symbole) und Produktion (spot_delta_aggregiert) teilen sich diese
    Mechanik bewusst: waeren es zwei Wege, koennte die Probe etwas bestaetigen, was
    der Produktionsweg anders macht.
    """
    # Ein injizierter `opener` heisst: kein Netz, also auch kein Rate-Limit — dann
    # nicht warten. Das ersetzt den frueheren Behelf "nicht warten, wenn kw leer ist",
    # der im echten Lauf genau falsch herum wirkte.
    _pause = (0.0 if "opener" in kw else PAUSE_JE_BLOCK) if pause is None else pause
    eintraege, bloecke = [], []
    for i in range(0, len(symbole), SYMBOLE_JE_ABRUF):
        teil = symbole[i:i + SYMBOLE_JE_ABRUF]
        # FEHLER BIS 20.09.2026: Die Pause hing daran, ob `kw` leer ist. Gedacht war
        # "im Test nicht warten" — im ECHTEN Lauf sind aber frm/to gesetzt, also war
        # kw NICHT leer und es wurde NIE gewartet. Genau dort, wo das Rate-Limit
        # (40/Min) greift, lief es ohne Pause. Jetzt explizit statt als Nebenwirkung.
        if i and _pause:
            time.sleep(_pause)
        try:
            roh = _mit_wiederholung(
                lambda: fetch_history(endpoint, api_key, symbol=",".join(teil),
                                      days=tage, **kw),
                f"{endpoint} ({len(teil)} Symbole)")
        except urllib.error.HTTPError as e:
            bloecke.append({"symbole": teil, "http_error": e.code,
                            "body": e.read().decode(errors="replace")[:200]})
            # Auch NACH einem Fehler einzeln nachfragen: Bis 20.09.2026 sprang der
            # Rueckfall nur an, wenn die Antwort unvollstaendig war — bei einem
            # HTTP-Fehler fiel der ganze Block ersatzlos aus. Ein einzelnes Symbol
            # kostet weniger Kontingent und kommt oft durch, wo vier scheitern.
            if len(teil) > 1:
                for sym in teil:
                    if _pause:
                        time.sleep(_pause)
                    nach, prot = _hole_reihen_roh(api_key, [sym], endpoint=endpoint,
                                                  tage=tage, pause=pause, **kw)
                    eintraege.extend(nach)
                    bloecke.append({"einzeln": sym, "zurueck": len(nach),
                                    "weil": f"Block scheiterte mit HTTP {e.code}",
                                    "protokoll": prot})
            continue
        except Exception as e:  # noqa: BLE001
            bloecke.append({"symbole": teil, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            continue
        if not isinstance(roh, list):
            bloecke.append({"symbole": teil, "fehler": "Antwort ist keine Liste",
                            "antwort_roh": _sample(roh)})
            continue
        neu = [e for e in roh if isinstance(e, dict)]
        eintraege.extend(neu)
        bloecke.append({"symbole": teil, "zurueck": len(neu)})
        if len(teil) > 1:
            # RUECKFALL (20.09.2026): Dass EIN Abruf mehrere Reihen liefert, ist nur
            # fuer ohlcv-history belegt. Beim Open Interest kam nichts zurueck, und der
            # ganze Vergleich fiel aus. Statt das als gegeben hinzunehmen: fehlende
            # Symbole einzeln nachfragen.
            #
            # Hier stand zuerst zusaetzlich `len(neu) < len(teil)`. Das war ueberfluessig
            # — `fehlt` ist ohnehin leer, wenn alles kam — und im Randfall schaedlich:
            # liefert die Antwort eine Reihe doppelt oder eine fremde mit, stimmt die
            # ANZAHL, aber ein Symbol fehlt trotzdem, und der Rueckfall bliebe aus.
            # Massgeblich ist, welche Symbole da sind, nicht wie viele Reihen kamen.
            fehlt = [s for s in teil
                     if not any(e.get("symbol") == s for e in eintraege)]
            for sym in fehlt:
                if _pause:
                    time.sleep(_pause)
                nach, prot = _hole_reihen_roh(api_key, [sym], endpoint=endpoint,
                                              tage=tage, pause=pause, **kw)
                eintraege.extend(nach)
                bloecke.append({"einzeln": sym, "zurueck": len(nach),
                                "weil": "Mehrfachabruf lieferte diese Reihe nicht",
                                "protokoll": prot})
    return eintraege, bloecke


def _pruefe_symbole(api_key: str, symbole: list, tage: int = SPOT_REICHWEITE_TAGE,
                    maerkte: dict | None = None, fx_je_quote: dict | None = None,
                    **kw) -> dict:
    """Beschreibt jede Symbolreihe (Probe-Sicht): Felder, Reichweite, Volumen, Einheit."""
    if not symbole:
        return {"fehler": "keine Symbole zu pruefen"}
    eintraege, bloecke = _hole_reihen_roh(api_key, symbole, tage=tage, **kw)
    je_symbol = {e.get("symbol", "?"): _reihe_auswerten(
        e, (maerkte or {}).get(e.get("symbol")), fx_je_quote) for e in eintraege}
    return {"angefragt": len(symbole), "zurueck": len(je_symbol),
            "mehrfachabruf_geht": any(b.get("zurueck", 0) > 1 for b in bloecke),
            "bloecke": bloecke, "je_symbol": je_symbol}


# ------------------------------------------------ E37.2: aggregiertes Spot-CVD
# Die Auswahl der Maerkte steht seit E37.1 fest (nach gemessenem Volumen, Lauf vom
# 19.09.2026): Binance BTCUSD.A, Bybit sBTCUSDT.6, Coinbase BTCUSD.C. OKX hat bei
# Coinalyze keinen Spot.
#
# ZWEI WAHLMOEGLICHKEITEN, weil das Dollar-Volumen einer Boerse auf mehrere Maerkte
# faellt (Binance: USDT 61 %, FDUSD 21 %, USDC 18 %). Welche besser ist, wird in E37.5
# gemessen und hier nicht behauptet.
SPOT_WAHL_GROESSTER = "groesster"     # je Boerse nur der groesste Dollar-Markt
SPOT_WAHL_ALLE = "alle_dollar"        # je Boerse alle Dollar-Maerkte zusammen


def spot_auswahl(api_key: str, **kw) -> dict:
    """BEIDE Auswahlen in EINEM Durchgang: {"groesster": {...}, "alle_dollar": {...}}.

    Je Wert {Boersencode: [Symbole]}. Bewusst zusammen, nicht zweimal einzeln: die
    Marktliste hat 5953 Eintraege und die Volumenmessung kostet mehrere Abrufe — das
    zweimal zu holen waere Verschwendung und koennte zwischen den beiden Laeufen sogar
    unterschiedlich ausfallen.
    """
    fx_je_quote = kw.pop("fx_je_quote", None)
    maerkte = get_json("spot-markets", {}, api_key, **kw)
    kandidaten = _kandidaten_je_boerse(maerkte if isinstance(maerkte, list) else [])
    alle = [e["symbol"] for liste in kandidaten.values() for e in liste if e.get("symbol")]
    if not alle:
        return {SPOT_WAHL_GROESSTER: {}, SPOT_WAHL_ALLE: {}}
    # Fuer die RANGFOLGE genuegen 30 Tage: welcher Markt einer Boerse der groesste ist,
    # aendert sich nicht dadurch, dass man ein Jahr statt einen Monat misst — aber ein
    # Jahr mal zehn Symbole ist zehnmal so viel Last auf einer API mit 40 Abrufen/Minute.
    reihen = _pruefe_symbole(api_key, alle, tage=AUSWAHL_TAGE,
                             maerkte={e["symbol"]: e for liste in kandidaten.values()
                                      for e in liste}, fx_je_quote=fx_je_quote,
                             **kw).get("je_symbol", {})
    spot_reihen = {s: r for s, r in reihen.items() if r.get("volumen_einheit") == "BTC"}
    return {
        SPOT_WAHL_GROESSTER: {code: [d["symbol"]] for code, d
                              in _groesster_je_boerse(kandidaten, spot_reihen).items()},
        SPOT_WAHL_ALLE: {code: [e["symbol"] for e in liste
                                if (reihen.get(e.get("symbol")) or {}).get("summe_v_usd") is not None
                                and (reihen.get(e.get("symbol")) or {}).get("volumen_einheit") == "BTC"]
                         for code, liste in kandidaten.items()},
    }


def spot_symbole(api_key: str, wahl: str = SPOT_WAHL_GROESSTER, **kw) -> dict:
    """Eine der beiden Auswahlen (Bequemlichkeit; holt beide und gibt eine zurueck)."""
    return spot_auswahl(api_key, **kw).get(wahl, {})


def _summiere_vollstaendig(je_symbol: dict, symbole: list, bloecke: list,
                           einheit: str) -> tuple:
    """Summiert je Zeitpunkt — aber NUR, wo alle gefragten Reihen einen Wert haben.

    DIE Regel der ganzen Aggregation, absichtlich an EINER Stelle: Spot-CVD,
    Futures-CVD, Open Interest und Liquidationen benutzen sie alle. Waere sie
    viermal geschrieben, koennte sie dreimal richtig und einmal falsch sein.

    Warum ueberhaupt: Fehlt an einem Zeitpunkt eine Boerse, entstuende sonst eine
    Teilsumme, die wie eine volle aussieht. Beim Open Interest waere das ein
    scheinbarer Einbruch um ein Drittel — also genau das Signal, auf das Muster 4
    (Kapitulation) wartet. Ein Datenloch wuerde zum Kaufsignal.
    """
    fehlende = [s for s in symbole if s not in je_symbol]
    vorhanden = [s for s in symbole if s in je_symbol]
    alle_ts = set().union(*(set(je_symbol[s]) for s in vorhanden)) if vorhanden else set()
    if not vorhanden:
        return {}, {"fehler": "keine einzige Reihe erhalten", "bloecke": bloecke,
                    "ohne_antwort": fehlende, "punkte_gesamt": 0,
                    "punkte_vollstaendig": 0, "punkte_ausgelassen": 0,
                    "einheit": einheit}

    vollstaendig = (set.intersection(*(set(je_symbol[s]) for s in vorhanden))
                     if not fehlende else set())
    summe = {ts: sum(je_symbol[s][ts] for s in symbole) for ts in sorted(vollstaendig)
             if all(_valid_measurement(je_symbol[s][ts]) for s in symbole)}
    bericht = {
        "symbole": vorhanden,
        "ohne_antwort": fehlende,
        "punkte_gesamt": len(alle_ts),
        "punkte_vollstaendig": len(summe),
        "punkte_ausgelassen": len(alle_ts) - len(summe),
        "je_symbol_punkte": {s: len(je_symbol[s]) for s in vorhanden},
        "bloecke": bloecke,
        "einheit": einheit,
    }
    if fehlende:
        bericht["fehler"] = "angeforderter Boersenkorb unvollstaendig"
    return summe, bericht


def _valid_measurement(value):
    return type(value) in (int, float) and math.isfinite(value)


def spot_delta_aggregiert(api_key: str, symbole: list,
                         einheiten: dict | None = None, **kw) -> tuple:
    """{Open-Time_ms: Summe der Taker-Deltas} ueber mehrere Boersen, plus Bericht.

    Delta je Markt = 2*bv - v (Kaeufe minus Verkaeufe), dieselbe Formel wie beim
    Futures-CVD. Summiert wird ueber die Boersen.

    DIE WICHTIGE REGEL: Ein Zeitpunkt zaehlt nur, wenn ihn ALLE gefragten Maerkte
    haben. Fehlt einer, entstuende sonst eine Teilsumme, die wie eine volle aussieht —
    an einem Tag, an dem Coinbase eine Luecke hat, waere der Spot-Flow scheinbar um ein
    Drittel eingebrochen, ohne dass irgendetwas am Markt passiert waere. Solche
    Zeitpunkte werden ausgelassen und GEZAEHLT; der Bericht sagt, wie viele es waren.

    Einheit: BTC (Basiswert), wie beim Futures-CVD. Das heutige Spot-CVD aus
    Binance-Vision rechnet dagegen in USD (Quote-Volumen). Beide gehen nur ueber
    _slope() als relative Aenderung in classify_pattern ein — die Einheit kuerzt sich
    heraus. Gemischt werden duerfen die beiden Reihen trotzdem nie.
    """
    if not symbole:
        return {}, {"fehler": "keine Symbole"}
    if not einheiten or any(einheiten.get(s) != "BTC" for s in symbole):
        return {}, {"fehler": "Spot-CVD-Einheiten nicht fuer jeden Markt als BTC belegt",
                    "symbole": symbole, "einheiten": einheiten or {}}
    eintraege, bloecke = _hole_reihen_roh(api_key, symbole, **kw)
    je_symbol: dict = {}
    for e in eintraege:
        sym, deltas = e.get("symbol", "?"), {}
        for p in e.get("history") or []:
            if isinstance(p, dict) and "t" in p and "v" in p and "bv" in p:
                deltas[int(p["t"]) * 1000] = 2.0 * float(p["bv"]) - float(p["v"])
        je_symbol[sym] = deltas

    return _summiere_vollstaendig(
        je_symbol, symbole, bloecke,
        "BTC (Basiswert) — NICHT mit dem USD-Spot-CVD aus Binance-Vision mischen")


# -------------------------- E37.3: Open Interest, Liquidationen, Futures-CVD aggregiert
# Furkan im Seminar zur Futures-Seite: "bei den Futures schauen wir uns die Perps von
# Binance, Bybit, OKX, [Bitget], Hyperliquid an" — dazu jeweils die USD-Perps. Von
# diesen fuehrt Coinalyze vier (Bitget fehlt in der Boersenliste).
PERP_BOERSEN = {"A": "Binance", "6": "Bybit", "3": "OKX", "H": "Hyperliquid"}
PERP_QUOTES = ("USDT", "USD", "USDC")     # Stablecoin- UND USD-Perps, wie bei ihm

# EINHEITEN — der gefaehrlichste Punkt dieser Etappe, per Probe 19.09.2026 geklaert:
#   open-interest-history und liquidation-history laufen mit convert_to_usd=true und
#   kommen in USD zurueck. USD ist ueber Boersen hinweg dieselbe Einheit -> summierbar.
#   ohlcv-history ignoriert convert_to_usd: 'v'/'bv' kommen in der Denominierung des
#   Marktes (Feld oi_lq_vol_denominated_in, bei BTCUSDT_PERP.A "BASE_ASSET" = BTC).
#   Ein inverser Kontrakt rechnet dagegen in USD/Kontrakten. Wer beides summiert,
#   bekommt eine voellig normal aussehende Zahl, die Unsinn ist. Deshalb wird das
#   Futures-CVD NUR ueber Maerkte derselben Denominierung gebildet; alle anderen
#   werden gemeldet statt stillschweigend weggelassen.
DENOM_FELD = "oi_lq_vol_denominated_in"


def _perp_ablehnungsgrund(e, codes) -> str | None:
    """Warum dieser Perp-Markt nicht in Frage kommt — oder None, wenn er passt."""
    if not isinstance(e, dict):
        return "kein Eintrag"
    if e.get("base_asset") != "BTC":
        return f"Basiswert ist {e.get('base_asset')!r}, nicht BTC"
    if e.get("exchange") not in codes:
        return f"Boerse {e.get('exchange')!r} gehoert nicht zu den gesuchten"
    if not e.get("is_perpetual"):
        return "kein Perpetual (Termin-Kontrakt laeuft aus)"
    if e.get("quote_asset") not in PERP_QUOTES:
        return f"Gegenwaehrung {e.get('quote_asset')!r} ist kein Dollar"
    if not e.get("has_ohlcv_data"):
        return "keine OHLCV-Daten (kein Futures-CVD moeglich)"
    return None


def _perp_kandidaten_je_boerse(maerkte: list, codes=PERP_BOERSEN) -> dict:
    """Alle passenden Perp-Maerkte je Boerse."""
    out: dict = {}
    for e in maerkte:
        if _perp_ablehnungsgrund(e, codes) is None:
            out.setdefault(e["exchange"], []).append(e)
    return out


def perp_auswahl(api_key: str, **kw) -> dict:
    """Je Boerse der groesste BTC-Perp-Markt, nach gemessenem Volumen — wie bei E37.1.

    Gibt zusaetzlich die Denominierung je gewaehltem Markt zurueck, weil davon
    abhaengt, welche Maerkte beim Futures-CVD ueberhaupt zusammengerechnet werden
    duerfen.
    """
    fx_je_quote = kw.pop("fx_je_quote", None)
    maerkte = get_json("future-markets", {}, api_key, **kw)
    kandidaten = _perp_kandidaten_je_boerse(maerkte if isinstance(maerkte, list) else [])
    alle = [e["symbol"] for liste in kandidaten.values() for e in liste if e.get("symbol")]
    if not alle:
        return {"gewaehlt": {}, "kandidaten": {}, "abgelehnt": {}}
    reihen = _pruefe_symbole(api_key, alle, tage=AUSWAHL_TAGE,
                             maerkte={e["symbol"]: e for liste in kandidaten.values()
                                      for e in liste}, fx_je_quote=fx_je_quote,
                             **kw).get("je_symbol", {})
    gewaehlt = _groesster_je_boerse(kandidaten, reihen, codes=PERP_BOERSEN)
    denom = {e["symbol"]: e.get(DENOM_FELD)
             for liste in kandidaten.values() for e in liste if e.get("symbol")}
    for code, d in gewaehlt.items():
        d["denominierung"] = denom.get(d["symbol"])
    abgelehnt: dict = {}
    for e in maerkte if isinstance(maerkte, list) else []:
        if not isinstance(e, dict) or e.get("exchange") not in PERP_BOERSEN:
            continue
        if e.get("base_asset") != "BTC":
            continue
        grund = _perp_ablehnungsgrund(e, PERP_BOERSEN)
        if grund:
            abgelehnt.setdefault(e["exchange"], []).append(
                {"symbol": e.get("symbol"), "grund": grund})
    # Die Einheiten-Trennung gehoert hierher, nicht in den Backtest: wer die Maerkte
    # waehlt, muss auch sagen, welche davon zusammengerechnet werden duerfen.
    cvd_symbole, cvd_ausgeschlossen = _nach_denominierung(gewaehlt)
    return {"gewaehlt": gewaehlt, "kandidaten": kandidaten, "abgelehnt": abgelehnt,
            "alle_symbole": [d["symbol"] for d in gewaehlt.values()],
            "cvd_symbole": cvd_symbole, "cvd_ausgeschlossen": cvd_ausgeschlossen}


def _nach_denominierung(gewaehlt: dict) -> tuple:
    """Teilt die gewaehlten Maerkte in die groesste Denominierungs-Gruppe und den Rest.

    Zurueck: (Symbole derselben Denominierung, Liste der ausgeschlossenen mit Grund).
    Gewinnt die Gruppe mit dem meisten Volumen — nicht die mit den meisten Maerkten:
    zwei Zwergboersen duerfen Binance nicht ueberstimmen.
    """
    gruppen: dict = {}
    raus = []
    for d in gewaehlt.values():
        denom = d.get("denominierung")
        usd = d.get("summe_v_usd")
        if denom not in ("BASE_ASSET", "QUOTE_ASSET", "BTC", "USD") \
                or not isinstance(usd, (int, float)) or not math.isfinite(usd) or usd <= 0:
            raus.append({"symbol": d.get("symbol"), "boerse": d.get("boerse"),
                         "denominierung": denom, "grund": "Einheit oder USD-Umrechnung nicht belegt"})
            continue
        gruppen.setdefault(denom, []).append(d)
    if not gruppen:
        return [], raus
    beste = max(gruppen, key=lambda k: sum(d["summe_v_usd"] for d in gruppen[k]))
    drin = [d["symbol"] for d in gruppen[beste]]
    raus += [{"symbol": d["symbol"], "boerse": d.get("boerse"),
             "denominierung": d.get("denominierung"),
             "grund": f"rechnet in {d.get('denominierung')!r}, die Mehrheit in {beste!r} "
                      "— summieren wuerde zwei Einheiten mischen"}
            for k, liste in gruppen.items() if k != beste for d in liste]
    return drin, raus


def fut_delta_aggregiert(api_key: str, symbole: list,
                        einheiten: dict | None = None, **kw) -> tuple:
    """Futures-CVD ueber mehrere Perp-Maerkte. NUR gleiche Denominierung (siehe oben)."""
    if not symbole:
        return {}, {"fehler": "keine Symbole"}
    denom = {einheiten.get(s) for s in symbole} if einheiten else set()
    if len(denom) != 1 or next(iter(denom), None) not in ("BASE_ASSET", "QUOTE_ASSET", "BTC", "USD"):
        return {}, {"fehler": "Futures-CVD-Denominierung fehlt oder ist gemischt",
                    "symbole": symbole, "einheiten": einheiten or {}}
    eintraege, bloecke = _hole_reihen_roh(api_key, symbole, **kw)
    je_symbol: dict = {}
    for e in eintraege:
        deltas = {}
        for p in e.get("history") or []:
            if isinstance(p, dict) and "t" in p and "v" in p and "bv" in p:
                deltas[int(p["t"]) * 1000] = 2.0 * float(p["bv"]) - float(p["v"])
        je_symbol[e.get("symbol", "?")] = deltas
    return _summiere_vollstaendig(
        je_symbol, symbole, bloecke,
        "Denominierung des Marktes (meist BTC) — NICHT mit USD-Reihen mischen")


def _werte_je_symbol(eintraege: list, feld: str) -> dict:
    """{Symbol: {ts_ms: Wert}} aus rohen History-Eintraegen, ein Feld herausgezogen."""
    return {
        e.get("symbol", "?"): {int(p["t"]) * 1000: float(p[feld])
                               for p in e.get("history") or []
                               if isinstance(p, dict) and "t" in p and feld in p}
        for e in eintraege}


def oi_je_symbol(api_key: str, symbole: list, **kw) -> tuple:
    """Open Interest je Boerse EINZELN — Grundlage fuer die Gewichtung in E37.4."""
    if not symbole:
        return {}, []
    eintraege, bloecke = _hole_reihen_roh(api_key, symbole,
                                          endpoint="open-interest-history", **kw)
    return _werte_je_symbol(eintraege, "c"), bloecke


def oi_aggregiert(api_key: str, symbole: list, **kw) -> tuple:
    """Open Interest ueber mehrere Perp-Maerkte, in USD (convert_to_usd=true)."""
    if not symbole:
        return {}, {"fehler": "keine Symbole"}
    je_symbol, bloecke = oi_je_symbol(api_key, symbole, **kw)
    return _summiere_vollstaendig(je_symbol, symbole, bloecke, "USD (convert_to_usd)")


# ------------------------------- E37.4: gewichtete Mittel (Funding, Long-Short)
# Funding und Long-Short werden NICHT summiert. Furkan stellt bei Velo ausdruecklich
# "Open Interest gewichtet, 8 Stunden, Durchschnitt" ein — und das ist auch sachlich
# richtig: Eine Funding-Rate ist ein Preis, keine Menge. Zwei Boersen mit 0,01 % und
# 0,03 % haben zusammen nicht 0,04 %.
#
# DIE FALLE: Ein EINFACHER Durchschnitt gibt einer Zwergboerse dasselbe Gewicht wie
# Binance. Bei vier Boersen, von denen eine 90 % des Open Interest haelt, verschiebt
# das den Wert um ein Vielfaches — und die Zahl sieht dabei voellig normal aus.


def gewichtetes_mittel(werte: dict, gewichte: dict, symbole: list, bloecke: list,
                       einheit: str) -> tuple:
    """Nach `gewichte` gewichtetes Mittel von `werte`, je Zeitpunkt.

    Dieselbe Vollstaendigkeitsregel wie bei den Summen, nur strenger: Ein Zeitpunkt
    zaehlt nur, wenn ALLE gefragten Symbole dort BEIDES haben — einen Wert UND ein
    Gewicht. Ein Markt ohne Open Interest wird also nicht etwa mit Gewicht 0
    mitgeschleift (das waere rechnerisch dasselbe wie Weglassen, aber niemand saehe
    es), sondern der ganze Zeitpunkt faellt weg und wird gezaehlt.

    Die Gewichte werden je Zeitpunkt normiert — die Summe der Gewichte kuerzt sich
    heraus. Ohne Normierung kaeme statt eines Mittels eine mit dem Open Interest
    skalierte Zahl heraus, die je nach Marktphase um Groessenordnungen schwankt.
    """
    fehlende = [s for s in symbole if s not in werte or s not in gewichte]
    vorhanden = [s for s in symbole if s in werte and s in gewichte]
    if not vorhanden:
        return {}, {"fehler": "keine einzige vollstaendige Reihe erhalten",
                    "bloecke": bloecke, "ohne_antwort": fehlende}

    alle_ts = set().union(*(set(werte[s]) for s in vorhanden))
    vollstaendig = (set.intersection(
        *(set(werte[s]) & set(gewichte[s]) for s in vorhanden))
        if not fehlende else set())
    out = {}
    ohne_gewicht = 0
    for ts in sorted(vollstaendig):
        if not all(_valid_measurement(werte[s][ts]) and
                   _valid_measurement(gewichte[s][ts]) and gewichte[s][ts] >= 0
                   for s in vorhanden):
            ohne_gewicht += 1
            continue
        summe_g = sum(gewichte[s][ts] for s in vorhanden)
        if summe_g <= 0:           # alle Gewichte null -> kein sinnvolles Mittel
            ohne_gewicht += 1
            continue
        out[ts] = sum(werte[s][ts] * gewichte[s][ts] for s in vorhanden) / summe_g
    bericht = {
        "symbole": vorhanden,
        "ohne_antwort": fehlende,
        "punkte_gesamt": len(alle_ts),
        "punkte_vollstaendig": len(out),
        "punkte_ausgelassen": len(alle_ts) - len(out),
        "punkte_ohne_gewicht": ohne_gewicht,
        "je_symbol_punkte": {s: len(werte[s]) for s in vorhanden},
        "bloecke": bloecke,
        "einheit": einheit,
        "gewichtung": "nach Open Interest je Zeitpunkt, normiert",
    }
    if fehlende:
        bericht["fehler"] = "angeforderter Boersenkorb unvollstaendig"
    return out, bericht


def funding_aggregiert(api_key: str, symbole: list, gewichte: dict, **kw) -> tuple:
    """Funding-Rate ueber mehrere Perp-Maerkte, nach Open Interest gewichtet.

    `gewichte` kommt aus oi_je_symbol() — bewusst als Parameter statt intern geholt:
    Der Backtest braucht das Open Interest ohnehin, und ein zweiter Abruf derselben
    Daten hat am 20.09.2026 das Rate-Limit gesprengt.
    """
    if not symbole:
        return {}, {"fehler": "keine Symbole"}
    eintraege, bloecke = _hole_reihen_roh(api_key, symbole,
                                          endpoint="funding-rate-history", **kw)
    return gewichtetes_mittel(_werte_je_symbol(eintraege, "c"), gewichte, symbole,
                              bloecke, "Funding-Rate je Intervall (Skala pruefen!)")


def long_short_aggregiert(api_key: str, symbole: list, gewichte: dict, **kw) -> tuple:
    """Long-Anteil in Prozent, nach Open Interest gewichtet.

    Ein Prozentsatz ist wie das Funding ein Verhaeltnis, keine Menge — summieren
    ergaebe Werte ueber 100 %.
    """
    if not symbole:
        return {}, {"fehler": "keine Symbole"}
    eintraege, bloecke = _hole_reihen_roh(api_key, symbole,
                                          endpoint="long-short-ratio-history", **kw)
    return gewichtetes_mittel(_werte_je_symbol(eintraege, "l"), gewichte, symbole,
                              bloecke, "Prozent Long-Anteil")


def skalen_vergleich(a: dict, b: dict, name_a: str, name_b: str) -> dict:
    """Vergleicht zwei Reihen in der Groessenordnung — VOR dem Austausch der Quelle.

    Grund (E37.4): Das Funding kommt heute von Kraken (`relativeFundingRate * 8`),
    die aggregierte Reihe von Coinalyze. Ob beide dieselbe Skala haben, steht
    NIRGENDS. In classify_pattern ist das Vorzeichen skalenunabhaengig — die Schwelle
    `funding_hot = 0.0001` aber nicht. Wer die Quelle tauscht, ohne die Skala zu
    pruefen, verschiebt stillschweigend eine Musterbedingung.
    """
    gemeinsam = sorted(set(a) & set(b))
    if not gemeinsam:
        return {"fehler": "keine gemeinsamen Zeitpunkte",
                f"punkte_{name_a}": len(a), f"punkte_{name_b}": len(b)}

    def _median(werte):
        s = sorted(werte)
        return s[len(s) // 2] if s else 0.0

    med_a = _median([abs(a[ts]) for ts in gemeinsam])
    med_b = _median([abs(b[ts]) for ts in gemeinsam])
    gleiches_vz = sum(1 for ts in gemeinsam
                      if (a[ts] > 0) == (b[ts] > 0)) / len(gemeinsam)
    return {
        "gemeinsame_punkte": len(gemeinsam),
        f"median_betrag_{name_a}": med_a,
        f"median_betrag_{name_b}": med_b,
        "faktor": (med_a / med_b) if med_b else None,
        "gleiches_vorzeichen_anteil": round(gleiches_vz, 4),
    }


def liq_aggregiert(api_key: str, symbole: list, **kw) -> tuple:
    """Liquidationen ueber mehrere Perp-Maerkte: {ts: (Long_USD, Short_USD)}.

    Long und Short werden getrennt summiert, aber ueber DIESELBE
    Vollstaendigkeitspruefung — sonst koennte ein Zeitpunkt bei Long vollstaendig und
    bei Short unvollstaendig sein und die beiden Reihen liefen auseinander.
    """
    if not symbole:
        return {}, {"fehler": "keine Symbole"}
    eintraege, bloecke = _hole_reihen_roh(api_key, symbole,
                                          endpoint="liquidation-history", **kw)
    lang, kurz = {}, {}
    for e in eintraege:
        sym = e.get("symbol", "?")
        pkte = [p for p in e.get("history") or [] if isinstance(p, dict)
                and all(k in p for k in ("t", "l", "s"))]
        lang[sym] = {int(p["t"]) * 1000: float(p["l"]) for p in pkte}
        kurz[sym] = {int(p["t"]) * 1000: float(p["s"]) for p in pkte}
    s_lang, bericht = _summiere_vollstaendig(lang, symbole, bloecke, "USD (convert_to_usd)")
    s_kurz, _ = _summiere_vollstaendig(kurz, symbole, bloecke, "USD (convert_to_usd)")
    return {ts: (v, s_kurz[ts]) for ts, v in s_lang.items()}, bericht


def spot_probe(api_key: str, **kw) -> dict:
    """Klaert, ob sich ein ueber Boersen aggregiertes Spot-CVD bauen laesst.

    Es wird nichts gebaut und nichts entschieden — nur gefragt und berichtet.
    """
    fx_je_quote = kw.pop("fx_je_quote", None)
    out: dict = {"_frage": ("Gibt es BTC-Dollar-Spotmaerkte auf Binance, Coinbase, Bybit "
                            "und OKX mit Kauf-/Verkaufsdaten, laesst sich alles in EINEM "
                            "Abruf holen, und wie weit reicht die Historie?")}
    try:
        maerkte_roh = get_json("spot-markets", {}, api_key, **kw)
    except urllib.error.HTTPError as e:
        out["spot_markets"] = {"http_error": e.code,
                               "body": e.read().decode(errors="replace")[:300]}
        out["_ergebnis"] = ("Der Endpunkt /spot-markets hat NICHT geantwortet (siehe "
                            "http_error). Damit faellt der Coinalyze-Weg aus.")
        return out
    except Exception as e:  # noqa: BLE001
        out["spot_markets"] = {"error": f"{type(e).__name__}: {str(e)[:250]}"}
        out["_ergebnis"] = "Abruf gescheitert (kein HTTP-Fehler) — siehe 'error'."
        return out

    maerkte = maerkte_roh if isinstance(maerkte_roh, list) else []
    kandidaten = _kandidaten_je_boerse(maerkte)
    abgelehnt = _abgelehnte_je_boerse(maerkte)
    alle = [e for liste in kandidaten.values() for e in liste]
    out["spot_markets"] = {
        "anzahl_gesamt": len(maerkte),
        "kandidaten_je_boerse": {BOERSEN_CODES.get(c, c): [e.get("symbol") for e in liste]
                                 for c, liste in kandidaten.items()},
        "abgelehnte_btc_maerkte": {BOERSEN_CODES.get(c, c): liste
                                   for c, liste in abgelehnt.items()},
    }

    # ALLE Kandidaten holen, nicht nur einen je Boerse — erst daraus entscheidet das
    # gemessene Volumen, welcher Markt der groesste ist (E37.1).
    symbole = [e["symbol"] for e in alle if e.get("symbol")]
    out["mehrfachabruf"] = _pruefe_symbole(
        api_key, symbole, maerkte={e["symbol"]: e for e in alle},
        fx_je_quote=fx_je_quote, **kw)
    reihen = out["mehrfachabruf"].get("je_symbol", {})
    gewaehlt = _groesster_je_boerse(
        kandidaten, {s: r for s, r in reihen.items() if r.get("volumen_einheit") == "BTC"})
    out["gewaehlt_je_boerse"] = gewaehlt

    # Wie deutlich gewinnt der groesste Markt? Das macht sichtbar, ob die alte Regel
    # (USDT vor USD) daneben lag — und um welchen Faktor.
    vergleich = {}
    for code, liste in kandidaten.items():
        zeilen = sorted(
            ({"symbol": e.get("symbol"), "quote": e.get("quote_asset"),
              "summe_v": (reihen.get(e.get("symbol")) or {}).get("summe_v", 0.0),
              "summe_v_usd": (reihen.get(e.get("symbol")) or {}).get("summe_v_usd")}
             for e in liste),
            key=lambda z: z["summe_v_usd"] or 0.0, reverse=True)
        gesamt = sum(z["summe_v_usd"] or 0.0 for z in zeilen) or 1.0
        for z in zeilen:
            z["anteil_prozent"] = (round(100.0 * z["summe_v_usd"] / gesamt, 2)
                                     if z["summe_v_usd"] is not None else None)
        vergleich[BOERSEN_CODES.get(code, code)] = zeilen
    out["volumenvergleich_je_boerse"] = vergleich

    fehlend = [name for code, name in BOERSEN_CODES.items() if code not in gewaehlt]
    weiten = [d["reichweite_tage"] for s, d in reihen.items()
              if s in {g["symbol"] for g in gewaehlt.values()}
              and isinstance(d.get("reichweite_tage"), (int, float)) and d["reichweite_tage"]]

    if not kandidaten:
        ergebnis = ("NEIN: auf keiner der vier Boersen wurde ein BTC-Dollar-Spotmarkt "
                    "mit Kauf-/Verkaufsdaten gefunden.")
    elif not gewaehlt:
        ergebnis = ("NEIN: Kandidaten gefunden, aber keiner liefert 'v' UND 'bv'. Ohne "
                    "beide Zahlen gibt es kein Kauf-/Verkaufs-Delta.")
    else:
        namen = ", ".join(f"{g['boerse']}: {g['symbol']}" for g in gewaehlt.values())
        ergebnis = (f"JA fuer {len(gewaehlt)} von {len(BOERSEN_CODES)} Boersen — "
                    f"nach Volumen gewaehlt: {namen}. Aggregiert wird von UNS, nicht "
                    "von Coinalyze — dort gibt es kein Aggregat-Symbol.")
        ergebnis += (" Ein Abruf liefert mehrere Reihen auf einmal."
                     if out["mehrfachabruf"].get("mehrfachabruf_geht")
                     else " ACHTUNG: kein Abruf gab mehr als eine Reihe zurueck — je "
                          "Symbol muss einzeln gefragt werden.")
    if fehlend:
        ergebnis += (f" Ohne passenden Markt: {', '.join(fehlend)} — Gruende unter "
                     "'abgelehnte_btc_maerkte'.")
    if weiten:
        ergebnis += (f" Historie der gewaehlten Maerkte: {min(weiten):.0f} bis "
                     f"{max(weiten):.0f} Tage — der KUERZESTE bestimmt das "
                     "Backtest-Fenster.")
    out["_ergebnis"] = ergebnis
    return out



SAMPLE_MAX_EINTRAEGE = 40    # Listenlaenge in der Probe-Datei (siehe unten)


def _sample(data):
    """Behaelt nur die letzten 3 Punkte je Symbol (kleine Probe fuers Log/JSON).

    Deckelt ausserdem die Laenge der Liste selbst. Grund (19.09.2026): die
    Metadaten-Endpunkte geben Tausende Eintraege zurueck — future-markets 5436,
    spot-markets 5953 — und die Probe-Datei wuchs dadurch auf 2 MB, die bei JEDEM
    Lauf ins Repo committet wurden. Wie viele weggelassen wurden, steht als letzter
    Eintrag drin, damit niemand eine gekuerzte Liste fuer die ganze haelt.
    """
    try:
        if isinstance(data, list):
            slim = []
            for item in data[:SAMPLE_MAX_EINTRAEGE]:
                it = dict(item) if isinstance(item, dict) else item
                if isinstance(it, dict) and isinstance(it.get("history"), list):
                    it = dict(it)
                    it["history"] = it["history"][-3:]
                slim.append(it)
            if len(data) > SAMPLE_MAX_EINTRAEGE:
                slim.append({"_gekuerzt": f"{len(data) - SAMPLE_MAX_EINTRAEGE} weitere "
                                          f"Eintraege weggelassen (von {len(data)})"})
            return slim
        return data
    except Exception:  # noqa: BLE001 — Probe soll nie hart scheitern
        return data


def probe():
    """Holt Mini-Stichproben und schreibt das rohe Format nach coinalyze_probe.json."""
    api_key = os.environ.get("COINALYZE_API_KEY", "")
    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "symbol": SYMBOL,
        "interval": INTERVAL,
    }
    if not api_key:
        out["error"] = "COINALYZE_API_KEY fehlt (Secret nicht gesetzt?)"
    else:
        def _hole(ep, ohne_zeitraum=False):
            if ohne_zeitraum:                      # Metadaten-Endpunkte kennen kein from/to
                return _sample(get_json(ep, {}, api_key))
            return _sample(fetch_history(ep, api_key))

        def _versuch(ep, ohne_zeitraum=False):
            try:
                return _hole(ep, ohne_zeitraum), None
            except urllib.error.HTTPError as e:
                return {"http_error": e.code,
                        "body": e.read().decode(errors="replace")[:400]}, e.code
            except Exception as e:  # noqa: BLE001
                return {"error": f"{type(e).__name__}: {str(e)[:300]}"}, "exc"

        for name, ep in ENDPOINTS.items():
            out[name], _ = _versuch(ep)

        # --- E15: Kandidaten-Endpunkte abklopfen (fuer Futures-CVD + Long-Short) -------
        gefunden, fehlt = [], []
        kand = {}
        for name, ep in KANDIDATEN.items():
            time.sleep(1.6)                        # Rate-Limit 40/Min respektieren
            daten, fehler = _versuch(ep, ohne_zeitraum=name in ("future_markets", "exchanges"))
            kand[name] = {"pfad": ep, "antwort": daten}
            (fehlt if fehler else gefunden).append(f"{name} ({ep})")
        out["kandidaten"] = kand

        # --- E37: Spot-Maerkte (Kaisers Frage nach Furkans Aggregation) --------------
        time.sleep(1.6)
        try:
            out["spot"] = spot_probe(api_key)
        except Exception as e:  # noqa: BLE001 — Probe soll nie hart scheitern
            out["spot"] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}

        out["_ergebnis"] = {
            "nutzbar": gefunden or ["— keiner"],
            "nicht_vorhanden": fehlt or ["— keiner"],
            "_lesehilfe": ("'nutzbar' = Endpunkt existiert und hat geantwortet; jetzt im "
                           "Feld 'kandidaten' nachsehen, WELCHE Felder drin sind. Fuer "
                           "Futures-CVD brauchen wir Taker-Kauf- und -Verkaufsvolumen "
                           "getrennt (oft 'bv'/'sv' oder 'buy_volume'/'sell_volume'). "
                           "Nur wenn beides da ist, laesst sich ein echtes Futures-CVD "
                           "bilden."),
        }
        print("\nNUTZBAR:", ", ".join(gefunden) or "keiner")
        print("NICHT VORHANDEN:", ", ".join(fehlt) or "keiner")
        print("\nSPOT (E37):", out.get("spot", {}).get("_ergebnis", "— nicht gelaufen"))
    path = ROOT / "site" / "data" / "coinalyze_probe.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print("Probe geschrieben:", path)
    print(json.dumps(out, indent=1, ensure_ascii=False)[:2500])


if __name__ == "__main__":
    probe()
