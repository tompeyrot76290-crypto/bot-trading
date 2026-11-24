#!/usr/bin/env python3
"""
EUR/USD scalping alert bot (1 minute) -> alertes Discord (webhook), conçu pour GitHub Actions.
Le bot N'EXÉCUTE AUCUN ORDRE : il t'envoie des plans de trade que tu exécutes toi-même.

Usage :
    python bot.py --test-discord      # envoie un message test
    python bot.py --backtest          # backtest technique sur ~7 jours
    python bot.py --minutes 305       # mode live pendant 305 minutes

Variables d'environnement : DISCORD_WEBHOOK_URL, CAPITAL, RISK_PCT, RR, MIN_SCORE
AVERTISSEMENT : aucun gain garanti. Backtest + compte démo avant tout argent réel.
"""
import os, re, time, argparse, email.utils
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import requests

# ============ PARAMETRES ============
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
CAPITAL = float(os.getenv("CAPITAL", "1000"))
RISK_PCT = float(os.getenv("RISK_PCT", "1.0"))
RR = float(os.getenv("RR", "2.0"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "70"))
PIP = 0.0001
PIP_VALUE_PER_LOT = 10.0          # USD / pip / lot standard (compte en USD)
MIN_STOP_PIPS, MAX_STOP_PIPS = 5.0, 12.0
MAX_HOLD_MIN = int(os.getenv("MAX_HOLD_MIN", "45"))   # durée max d'un trade (mets 15 pour du scalp très court)
COOLDOWN_MIN = 15
MAX_SIGNALS_PER_SESSION = 3
SPREAD_PIPS = 0.8
TRADE_HOURS_UTC = (7, 17)
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FEEDS = [
    "https://news.google.com/rss/search?q=EUR%2FUSD+OR+ECB+OR+Fed+OR+dollar+euro+when%3A1d&hl=en-US&gl=US&ceid=US%3Aen",
    "https://www.fxstreet.com/rss/news",
    "https://www.ecb.europa.eu/rss/press.html",
    "https://www.federalreserve.gov/feeds/press_all.xml",
]
TZ = ZoneInfo("Europe/Paris")                       # gère automatiquement heure d'été / d'hiver
INTRADAY_START_HOUR = int(os.getenv("INTRADAY_START_HOUR", "8"))   # heure de Paris
READ_EVERY_MIN = int(os.getenv("READ_EVERY_MIN", "120"))
JOURS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
GREEN, RED, GREY, ORANGE, BLUE = 0x2ECC71, 0xE74C3C, 0x95A5A6, 0xF39C12, 0x3498DB


# ============ HEURE FRANCAISE + TRADUCTION ============
def hm(ts):
    return pd.Timestamp(ts).tz_convert(TZ).strftime("%H:%M")


def jhm(ts):
    t = pd.Timestamp(ts).tz_convert(TZ)
    return f"{JOURS[t.weekday()]} {t.strftime('%H:%M')}"


_TR = {}


def fr(text):
    """Traduit en français (service gratuit non officiel ; si indisponible, garde l'anglais)."""
    if text in _TR:
        return _TR[text]
    out = text
    try:
        r = requests.get("https://translate.googleapis.com/translate_a/single",
                         params={"client": "gtx", "sl": "en", "tl": "fr", "dt": "t", "q": text}, timeout=8)
        if r.status_code == 200:
            out = "".join(seg[0] for seg in r.json()[0] if seg and seg[0]) or text
    except Exception as e:
        print("Traduction indisponible:", e)
    _TR[text] = out
    return out


def ev_name(e):
    return f"{e['country']} {fr(e['name'])}"


# ============ DISCORD ============
def discord(title, desc="", color=BLUE, fields=None):
    print(f"\n[{title}]\n{desc}\n")
    if not WEBHOOK:
        return
    embed = {"title": title[:256], "description": desc[:4000], "color": color,
             "timestamp": pd.Timestamp.now(tz="UTC").isoformat()}
    if fields:
        fields = [f for f in fields if f]
        total = len(title) + len(desc) + sum(len(n) + len(v or "") for n, v, _ in fields)
        if total > 5600:
            lim = max(300, (5200 - len(desc)) // max(1, len(fields)) - 40)
            fields = [(n, cut(v or "-", lim), i) for n, v, i in fields]
        embed["fields"] = [{"name": n[:256], "value": (v or "-")[:1024], "inline": i} for n, v, i in fields]
    try:
        r = requests.post(WEBHOOK, json={"username": "EUR/USD Bot", "embeds": [embed]}, timeout=10)
        if r.status_code >= 300:
            print("Erreur Discord:", r.status_code, r.text[:200])
    except Exception as e:
        print("Erreur Discord:", e)


# ============ DONNEES ============
def fetch_df(ticker, interval, period):
    import yfinance as yf
    for _ in range(3):
        df = yf.download(ticker, interval=interval, period=period, progress=False, auto_adjust=False)
        if df is not None and len(df):
            break
        time.sleep(3)
    else:
        raise RuntimeError(f"Pas de données pour {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    df.index = df.index.tz_localize("UTC") if df.index.tz is None else df.index.tz_convert("UTC")
    return df


def get_candles(period="5d"):
    return fetch_df("EURUSD=X", "1m", period)[["Open", "High", "Low", "Close"]]


def get_events():
    """Annonces EUR/USD de la semaine (impact High/Medium), heures en UTC."""
    try:
        data = requests.get(CALENDAR_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"}).json()
    except Exception as e:
        print("Calendrier indisponible:", e)
        return None
    ev = []
    for e in data:
        if e.get("country") not in ("EUR", "USD") or e.get("impact") not in ("High", "Medium"):
            continue
        ev.append({"time": pd.to_datetime(e["date"], utc=True), "title": f"{e['country']} {e['title']}",
                   "country": e["country"], "name": e["title"],
                   "impact": e["impact"], "forecast": e.get("forecast", ""), "previous": e.get("previous", "")})
    return sorted(ev, key=lambda x: x["time"])


# ============ CONSCIENCE DU MARCHE ============
def get_macro():
    """Dollar index, rendements US 10 ans, S&P 500 : ce que fait le reste du marché en ce moment."""
    score, reasons, snap = 0.0, [], {}
    try:
        dxy = fetch_df("DX-Y.NYB", "5m", "2d")["Close"]
        chg = (dxy.iloc[-1] / dxy.iloc[-13] - 1) * 100  # ~1h
        snap["DXY"] = f"{dxy.iloc[-1]:.2f} ({chg:+.2f}% /1h)"
        if abs(chg) > 0.10:
            s = -1 if chg > 0 else 1
            score += s * (2 if abs(chg) > 0.25 else 1)
            reasons.append(f"Dollar index {'en hausse' if chg > 0 else 'en baisse'} ({chg:+.2f}%/1h) => {'pression sur' if chg > 0 else 'soutien pour'} l'EUR/USD")
    except Exception as e:
        print("DXY indisponible:", e)
    try:
        y = fetch_df("^TNX", "5m", "2d")["Close"]
        d = y.iloc[-1] - y.iloc[-13]
        snap["US10Y"] = f"{y.iloc[-1]:.3f}% ({d:+.3f} /1h)"
        if abs(d) > 0.03:
            score += -1 if d > 0 else 1
            reasons.append(f"Rendement US 10 ans {'monte' if d > 0 else 'baisse'} ({d:+.3f} pt) => dollar {'soutenu' if d > 0 else 'pénalisé'}")
    except Exception as e:
        print("US10Y indisponible:", e)
    try:
        spx = fetch_df("^GSPC", "5m", "2d")["Close"]
        chg = (spx.iloc[-1] / spx.iloc[-13] - 1) * 100
        snap["S&P500"] = f"{spx.iloc[-1]:.0f} ({chg:+.2f}% /1h)"
        if abs(chg) > 0.4:
            score += 0.5 if chg > 0 else -0.5
            reasons.append(f"Actions US {'en hausse' if chg > 0 else 'en baisse'} ({chg:+.2f}%) : {'appétit' if chg > 0 else 'aversion'} pour le risque")
    except Exception as e:
        print("S&P indisponible:", e)
    return score, reasons, snap


_EXCL = re.compile(r"\b(australian|canadian|new zealand|aussie|kiwi|loonie|yen|sterling|pound|franc|aud|cad|nzd|jpy|gbp|chf)\b")
_BULL = [
    r"eur/usd[^.]*\b(rises|rise|gains|gain|climbs|rallies|jumps|advances|higher|extends gains)\b",
    r"\beuro\b[^.]*\b(rises|gains|climbs|rallies|jumps|firms|strengthens|advances)\b",
    r"\b(dollar|usd|greenback|dxy)\b[^.]*\b(falls|slides|drops|weakens|retreats|declines|slumps|softens|sinks|slips)\b",
    r"\bfed\b[^.]*\b(rate cut|cuts|dovish|easing)\b",
    r"\becb\b[^.]*\b(hike|hikes|hawkish|raise|raises|tighten)\b",
    r"\b(us|u\.s\.)\b[^.]*\b(payrolls|jobs|cpi|inflation|retail sales|gdp|pmi)\b[^.]*\b(miss|misses|weaker|cools|below|slows|disappoint)\b",
    r"\b(eurozone|euro zone|german|germany)\b[^.]*\b(beat|beats|stronger|above|rebound|improves)\b",
]
_BEAR = [
    r"eur/usd[^.]*\b(falls|fall|drops|slides|slips|sinks|declines|lower|extends losses)\b",
    r"\beuro\b[^.]*\b(falls|slides|drops|weakens|slumps|sinks|slips|declines)\b",
    r"\b(dollar|usd|greenback|dxy)\b[^.]*\b(rises|gains|climbs|rallies|jumps|strengthens|firms|advances)\b",
    r"\bfed\b[^.]*\b(hike|hikes|hawkish|higher for longer|tighten)\b",
    r"\becb\b[^.]*\b(rate cut|cuts|dovish|easing)\b",
    r"\b(us|u\.s\.)\b[^.]*\b(payrolls|jobs|cpi|inflation|retail sales|gdp|pmi)\b[^.]*\b(beat|beats|stronger|hotter|above|accelerates|surges)\b",
    r"\b(eurozone|euro zone|german|germany)\b[^.]*\b(miss|misses|weaker|contracts|below|slump|recession)\b",
]


def score_headline(title):
    t = title.lower()
    if _EXCL.search(t) and "eur" not in t:
        return 0
    b = any(re.search(p, t) for p in _BULL)
    r = any(re.search(p, t) for p in _BEAR)
    return int(b) - int(r)


def get_news(max_age_h=8):
    """Titres récents (BCE, Fed, FXStreet, presse) + score directionnel EUR/USD."""
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=max_age_h)
    items, seen = [], set()
    for url in FEEDS:
        try:
            root = ET.fromstring(requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}).content)
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                pub = it.findtext("pubDate")
                if not title or not pub or title in seen:
                    continue
                ts = pd.Timestamp(email.utils.parsedate_to_datetime(pub))
                ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
                if ts >= cutoff:
                    seen.add(title)
                    items.append((ts, title, score_headline(title)))
        except Exception as e:
            print("Flux indisponible:", url[:40], e)
    items.sort(key=lambda x: x[0], reverse=True)
    total = sum(s for _, _, s in items)
    return float(np.clip(total * 0.5, -3, 3)), items


def get_daily():
    """Tendance journalière, EMA20/50, plus haut/bas de la veille, fourchette 20 jours."""
    try:
        d = fetch_df("EURUSD=X", "1d", "1y")
        c = d.Close
        e20 = float(c.ewm(span=20, adjust=False).mean().iloc[-1])
        e50 = float(c.ewm(span=50, adjust=False).mean().iloc[-1])
        today = pd.Timestamp.now(tz="UTC").date()
        prev = d.iloc[-2] if d.index[-1].date() == today else d.iloc[-1]
        last20 = d.iloc[-20:]
        done = d[d.index.date < today]
        adr = float((done.High - done.Low).iloc[-10:].mean() / PIP) if len(done) >= 3 else None
        return {"adr10": adr, "trend": int(np.sign(e20 - e50)), "ema20": e20, "ema50": e50,
                "pdh": float(prev.High), "pdl": float(prev.Low),
                "hi20": float(last20.High.max()), "lo20": float(last20.Low.min()),
                "chg5": float((c.iloc[-1] - c.iloc[-6]) / PIP) if len(c) > 6 else 0.0}
    except Exception as e:
        print("Données journalières indisponibles:", e)
        return None


# ============ ANALYSE INTRADAY : "pourquoi ça bouge ?" ============
def pct_since(ticker, start, absolute=False):
    s = fetch_df(ticker, "5m", "2d")["Close"]
    ref = s.asof(start)
    if pd.isna(ref):
        ref = s.iloc[0]
    last = s.iloc[-1]
    return float(last - ref) if absolute else float((last / ref - 1) * 100)


def get_drivers(start):
    """Variation depuis `start` des autres devises vs dollar, du DXY et du rendement US 10 ans."""
    out = {}
    for k, t in (("GBP/USD", "GBPUSD=X"), ("AUD/USD", "AUDUSD=X"), ("USD/JPY", "USDJPY=X"), ("DXY", "DX-Y.NYB")):
        try:
            out[k] = pct_since(t, start)
        except Exception as e:
            print("Indisponible:", k, e)
    try:
        out["US10Y"] = pct_since("^TNX", start, absolute=True)
    except Exception as e:
        print("Indisponible: US10Y", e)
    return out


def intraday_analysis(df, daily, events, items, now, drivers=None):
    """Récap court et cohérent de la journée, bâti uniquement sur des faits mesurés."""
    now_p = now.tz_convert(TZ)
    start_p = now_p.replace(hour=INTRADAY_START_HOUR, minute=0, second=0, microsecond=0)
    if now_p < start_p:
        start_p -= pd.Timedelta(days=1)
    start = start_p.tz_convert("UTC")
    d = df[df.index >= start]
    if len(d) < 30:
        return None
    H = INTRADAY_START_HOUR
    last = float(d.Close.iloc[-1])
    move = (last - float(d.Open.iloc[0])) / PIP
    hi, lo = float(d.High.max()), float(d.Low.min())
    c5 = d.Close.resample("5min").last().dropna()
    step = c5.diff().abs().sum()
    er = float(abs(c5.iloc[-1] - c5.iloc[0]) / step) if len(c5) > 2 and step > 0 else 0.0
    if move >= 8 and er >= 0.10:
        dirn = 1
    elif move <= -8 and er >= 0.10:
        dirn = -1
    else:
        dirn = 0
    quality = "propre" if er >= 0.25 else "heurtée"
    t = daily["trend"] if daily else 0
    fond = "baissier" if t < 0 else "haussier" if t > 0 else "neutre"

    if dirn:
        head = f"{'📈' if dirn > 0 else '📉'} Journée **{'haussière' if dirn > 0 else 'baissière'}** : {move:+.0f} pips depuis {H}h, tendance {quality}"
        if t and dirn != t:
            head += f", à contre-courant du fond {fond}"
        elif t:
            head += f", dans le sens du fond {fond}"
    else:
        head = f"↔️ Journée **sans direction nette** : {move:+.0f} pips depuis {H}h, fourchette {(hi - lo) / PIP:.0f} pips" + (f" (fond {fond})" if t else "")

    causes = []
    dd = df[df.index >= now.normalize()]
    if daily and len(dd) and dirn:
        if dirn > 0 and dd.Low.min() < daily["pdl"] < last:
            causes.append("balayage des stops sous le plus bas de la veille puis reprise")
        if dirn < 0 and dd.High.max() > daily["pdh"] > last:
            causes.append("balayage des stops au-dessus du plus haut de la veille puis retombée")
    asia = df[(df.index >= now.normalize()) & (df.index < now.normalize() + pd.Timedelta(hours=7))]
    if len(asia) > 20 and now.hour >= 7 and dirn:
        ah, al = float(asia.High.max()), float(asia.Low.min())
        if dirn > 0 and last > ah:
            causes.append("sortie par le haut du range asiatique")
        elif dirn < 0 and last < al:
            causes.append("sortie par le bas du range asiatique")
    if drivers and dirn:
        dv = dollar_votes(drivers)
        if dv:
            weak, strong, detail = dv
            usd_side = weak if dirn > 0 else strong
            if usd_side >= 2:
                causes.append(f"dollar {'faible' if dirn > 0 else 'fort'} face à plusieurs devises ({detail})")
            elif usd_side == 0:
                causes.append("l'euro bouge presque seul (les autres devises ne suivent pas) : flux propres à l'euro")
        y = drivers.get("US10Y")
        if y is not None and abs(y) > 0.02 and (y < 0) == (dirn > 0):
            causes.append(f"rendements US {'en baisse' if y < 0 else 'en hausse'} ({y:+.3f} pt)")
    nb = sum(1 for ts, _, s in items if ts >= start and s > 0)
    nr = sum(1 for ts, _, s in items if ts >= start and s < 0)
    if dirn and (nb - nr) * dirn > 0:
        causes.append(f"presse orientée {'euro' if dirn > 0 else 'dollar'} ({max(nb, nr)} titres)")
    cause_line = ("Causes probables (corrélations mesurées) : " + " ; ".join(causes)) if causes else \
        "Causes : aucun facteur mesurable net, je ne force pas d'explication"

    niv, crossed = [], False
    if daily:
        gap = (last - daily["ema20"]) / PIP
        niv.append(f"EMA20 jour {daily['ema20']:.5f} ({gap:+.0f} pips)")
        niv.append(f"veille {daily['pdl']:.5f}–{daily['pdh']:.5f}")
        rng20 = daily["hi20"] - daily["lo20"]
        if rng20 > 0 and t != 0:
            ret = (last - daily["lo20"]) / rng20 if t < 0 else (daily["hi20"] - last) / rng20
            niv.append(f"{ret * 100:.0f}% de la {'baisse' if t < 0 else 'hausse'} sur 20 j déjà {'reprise' if t < 0 else 'effacée'}")
        if dirn and t and dirn != t:
            crossed = (last > daily["ema20"]) if t < 0 else (last < daily["ema20"])
    lines = [head, cause_line]
    if niv:
        lines.append("Niveaux : " + " | ".join(niv))
    if dirn and t and dirn != t:
        impl = ("EMA20 jour déjà repassée : retournement possible mais à confirmer, trades courts." if crossed
                else "mouvement de correction dans le fond : trades courts (15–20 min), pas de poursuite lointaine.")
    elif dirn:
        impl = "mouvement en phase avec le fond : les replis sont à jouer dans le sens du mouvement, horizon un peu plus long possible."
    else:
        impl = "pas d'avantage clair : patience, ou scalps de range."
    lines.append("➡️ " + impl)
    passed = [ev_name(e) for e in events if start <= e["time"] <= now and e["impact"] == "High"]
    if passed:
        lines.append("Déjà publié : " + " ; ".join(passed[:2]))
    return {"recap": "\n".join(lines), "dirn": dirn, "move": move}


def get_forum_chatter(max_items=3):
    """Titres récents du forum Reddit r/Forex parlant de l'euro (peut échouer depuis GitHub : tolérant)."""
    out = []
    try:
        r = requests.get("https://www.reddit.com/r/Forex/.rss", timeout=15, headers={"User-Agent": "Mozilla/5.0 (eurusd-bot)"})
        root = ET.fromstring(r.content)
        for el in root.iter():
            if el.tag.endswith("entry"):
                title = next((c.text for c in el if c.tag.endswith("title")), "") or ""
                if re.search(r"\b(eur|euro|eurusd|eur/usd|ecb|bce)\b", title.lower()):
                    out.append(title.strip())
    except Exception as e:
        print("Reddit indisponible:", e)
    return out[:max_items]


def build_context(daily, df=None, events=(), now=None):
    m_score, m_reasons, snap = get_macro()
    n_score, items = get_news()
    now = now or pd.Timestamp.now(tz="UTC")
    bias = m_score + n_score
    top = [(t, s) for _, t, s in items if s != 0][:3] or [(t, s) for _, t, s in items][:3]
    ia, drivers = None, {}
    if df is not None:
        try:
            start_p = now.tz_convert(TZ).replace(hour=INTRADAY_START_HOUR, minute=0, second=0, microsecond=0)
            if now.tz_convert(TZ) < start_p:
                start_p -= pd.Timedelta(days=1)
            drivers = get_drivers(start_p.tz_convert("UTC"))
            ia = intraday_analysis(df, daily, list(events), items, now, drivers)
        except Exception as e:
            print("Analyse intraday impossible:", e)
    if ia and ia.get("dirn"):
        bias += ia["dirn"] * 1.0
        m_reasons = m_reasons + [f"Tendance intraday {'haussière' if ia['dirn'] > 0 else 'baissière'} depuis {INTRADAY_START_HOUR}h"]
    return {"bias": bias, "macro_score": m_score, "news_score": n_score, "macro_reasons": m_reasons,
            "snap": snap, "headlines": top, "n_headlines": len(items), "daily": daily, "items": items,
            "intraday": ia, "forum": get_forum_chatter(), "drivers": drivers}


def bias_label(b):
    if b >= 2: return "🟢 HAUSSIER EUR/USD"
    if b <= -2: return "🔴 BAISSIER EUR/USD"
    return "⚪ NEUTRE / pas de biais clair"


def market_read(ctx, events, now, df=None):
    nxt = [e for e in events if e["time"] > now][:4]
    agenda = "\n".join(f"{'🔴' if e['impact']=='High' else '🟠'} {jhm(e['time'])} — {ev_name(e)}" for e in nxt) or "Rien de notable à venir"
    snap = "\n".join(f"**{k}** : {v}" for k, v in ctx["snap"].items()) or "indisponible"
    heads = "\n".join(f"{'🟢' if s>0 else '🔴' if s<0 else '•'} {fr(re.sub(r' - [^-]+$', '', t))[:150]}" for t, s in ctx["headlines"]) or "Aucun titre récent exploitable"
    d = ctx["daily"]
    daily = "indisponible" if not d else f"tendance {'haussière' if d['trend']>0 else 'baissière' if d['trend']<0 else 'neutre'}\nveille : haut {d['pdh']:.5f} / bas {d['pdl']:.5f}"
    why = "\n".join(f"• {r}" for r in ctx["macro_reasons"]) or "Pas de mouvement macro marquant"
    fields = []
    if df is not None:
        fields.append(advice_field(ctx, df, events, now))
    ia = ctx.get("intraday")
    if ia:
        fields.append(("🧾 Récap de la journée", cut(ia["recap"]), False))
    fields.append(outlook_field(events, now))
    fields += [("Marchés liés", snap, True), ("Contexte journalier", daily, True), ("Ce qui se dit (presse)", cut(heads), False)]
    if ctx.get("forum"):
        fields.append(("💬 Ambiance traders (Reddit r/Forex)", cut("\n".join("• " + fr(t)[:140] for t in ctx["forum"])), False))
    fields.append(("Prochaines annonces (heure de Paris)", agenda, False))
    discord(f"🧠 Lecture du marché — {bias_label(ctx['bias'])}",
            f"Score macro {ctx['macro_score']:+.1f} | Score actualités {ctx['news_score']:+.1f} ({ctx['n_headlines']} titres analysés)\n\n{why}",
            GREEN if ctx["bias"] >= 2 else RED if ctx["bias"] <= -2 else GREY, fields)




# ============ CONSEIL "CE QUE JE FERAIS MAINTENANT" ============
def window_stats(df, minutes):
    w = df.iloc[-minutes:]
    if len(w) < minutes * 0.8:
        return None
    move = (float(w.Close.iloc[-1]) - float(w.Open.iloc[0])) / PIP
    c5 = w.Close.resample("5min").last().dropna()
    step = c5.diff().abs().sum()
    er = float(abs(c5.iloc[-1] - c5.iloc[0]) / step) if len(c5) > 2 and step > 0 else 0.0
    return move, er


def _wait(reason, await_=""):
    return {"action": "wait", "reason": reason, "await": await_}


def advise(ctx, df, events, now):
    """Orientation du moment : acheter / vendre / attendre, durée, entrée, stop, objectif, prudence.
    Raisonnement : somme de faits mesurés (momentum, tendances 5m/15m, dollar, presse), le fond journalier ne décide pas
    de la direction mais limite la conviction et la durée."""
    row = df.iloc[-1]
    price, atr_pips = float(row.Close), float(row.atr) / PIP
    if not (TRADE_HOURS_UTC[0] <= now.hour < TRADE_HOURS_UTC[1]) or now.weekday() >= 5:
        return _wait("hors des heures de trading (Londres/New York)", "la prochaine session")
    state, info = news_state(now, events)
    if state == "block":
        return _wait(info, "3 minutes après la publication, puis je lis la réaction du prix avant d'agir")
    if atr_pips < 0.6:
        return _wait(f"marché trop calme (ATR {atr_pips:.1f} pips) : pas assez de mouvement pour viser {RR:g}R", "une reprise de volatilité")
    if atr_pips > 6:
        return _wait(f"volatilité excessive (ATR {atr_pips:.1f} pips) : spreads et slippage dangereux", "le retour au calme")

    facts = []  # (poids signé, texte)
    sg = lambda x: 1 if x > 0 else -1
    m30, m60, m120 = window_stats(df, 30), window_stats(df, 60), window_stats(df, 120)
    if m60 and abs(m60[0]) >= 6 and m60[1] >= 0.2:
        facts.append((1.5 * sg(m60[0]), f"depuis {hm(now - pd.Timedelta(minutes=60))} : {m60[0]:+.0f} pips, mouvement régulier (efficacité {m60[1]:.0%})"))
    if m120 and abs(m120[0]) >= 10 and m120[1] >= 0.15:
        facts.append((0.5 * sg(m120[0]), f"sur 2h : {m120[0]:+.0f} pips"))
    if m30 and abs(m30[0]) >= 4 and m60 and sg(m30[0]) != sg(m60[0]):
        facts.append((1.0 * sg(m30[0]), f"sur 30 min le prix repart en sens inverse ({m30[0]:+.0f} pips)"))
    for name, lab in (("t5", "5 min"), ("t15", "15 min")):
        if row[name] != 0:
            facts.append((0.5 * row[name], f"tendance {lab} {'haussière' if row[name] > 0 else 'baissière'}"))
    if ctx:
        dv = dollar_votes(ctx.get("drivers") or {})
        if dv:
            weak, strong, detail = dv
            if weak >= 2:
                facts.append((0.75, f"dollar faible face à plusieurs devises ({detail})"))
            elif strong >= 2:
                facts.append((-0.75, f"dollar fort face à plusieurs devises ({detail})"))
        v = float(np.clip((ctx["macro_score"] + ctx["news_score"]) / 2, -1, 1)) * 0.75
        if abs(v) >= 0.25:
            facts.append((v, f"presse et marchés liés orientés {'euro' if v > 0 else 'dollar'}"))
    if state == "post":
        mv = (price - float(df.Close.asof(info["time"]))) / PIP
        if abs(mv) >= 4:
            facts.append((1.0 * sg(mv), f"réaction à {ev_name(info)} : {mv:+.0f} pips"))

    D = sum(w for w, _ in facts)
    if abs(D) < 1.5:
        return _wait("aucun avantage net : les facteurs se partagent ou se neutralisent (score {:+.1f})".format(D),
                     "un mouvement régulier sur 30–60 min, ou une cassure franche avec momentum")
    dirn = 1 if D > 0 else -1
    sup = [t for w, t in facts if np.sign(w) == dirn]
    opp = [t for w, t in facts if np.sign(w) == -dirn]
    conv = "forte" if abs(D) >= 3.5 else "moyenne" if abs(D) >= 2.5 else "faible"
    dt_ = ctx["daily"]["trend"] if ctx and ctx.get("daily") else 0
    counter = dt_ == -dirn and dt_ != 0
    if counter:
        if conv == "faible":
            return _wait(f"avantage trop mince pour aller contre le fond journalier {'baissier' if dt_ < 0 else 'haussier'} (score {D:+.1f})",
                         "plus de confluence (momentum + dollar + presse alignés)")
        conv = "faible" if conv == "moyenne" else "moyenne"

    base = {"forte": 30, "moyenne": 20, "faible": 15}[conv]
    if counter:
        base = min(base, 20)
    elif dt_ == dirn:
        base += 10
    h = int(min(max(10, round(base / 5) * 5), MAX_HOLD_MIN))
    nxt = next((e for e in events if e["time"] > now and e["impact"] == "High"), None)
    if nxt:
        avail = int((nxt["time"] - now).total_seconds() / 60) - 5
        if avail < h:
            h = int(avail // 5 * 5)
    if h < 10:
        return _wait("annonce majeure trop proche pour un trade qui a le temps de se développer", "la publication")
    h_txt = f"{max(10, h - 5)}–{h} min" if h > 10 else "10 min"

    ext = (price - float(row.ema20)) / float(row.atr) * dirn
    rsi_ext = (dirn > 0 and row.rsi > 75) or (dirn < 0 and row.rsi < 25)
    if ext > 2.5 or rsi_ext:
        entry = float(row.ema20)
        entry_txt = f"Entrée : **ne pas poursuivre ici** (prix étiré de {max(ext, 0):.1f} ATR de la moyenne, RSI {row.rsi:.0f}). Attends un repli vers **{entry:.5f}**."
    else:
        entry = price
        entry_txt = f"Entrée : possible vers **{entry:.5f}**, après une bougie 1 min qui confirme le sens."
    swing = float(df.Low.iloc[-15:].min()) if dirn > 0 else float(df.High.iloc[-15:].max())
    stop_pips = float(np.clip(abs(entry - swing) / PIP + 0.5 * atr_pips, MIN_STOP_PIPS, MAX_STOP_PIPS))
    sl, tp = entry - dirn * stop_pips * PIP, entry + dirn * stop_pips * RR * PIP
    part = entry + dirn * stop_pips * PIP
    risk_amt = CAPITAL * RISK_PCT / 100
    lots = max(0.01, np.floor(risk_amt / (stop_pips * PIP_VALUE_PER_LOT) * 100) / 100)
    caution = ""
    if counter:
        caution = (f"Tu vas contre le fond journalier {'baissier' if dt_ < 0 else 'haussier'} : mouvement plus court et plus fragile, "
                   f"durée limitée, prends tes profits vite.")
    if nxt:
        caution += f" Annonce majeure à {hm(nxt['time'])} ({ev_name(nxt)}) : sois sorti avant."
    return {"action": "long" if dirn > 0 else "short", "conv": conv, "h": h, "h_txt": h_txt, "sup": sup, "opp": opp,
            "entry_txt": entry_txt, "sl": sl, "tp": tp, "part": part, "stop_pips": stop_pips, "lots": lots,
            "risk_amt": risk_amt, "exit_by": now + pd.Timedelta(minutes=h), "caution": caution.strip(),
            "invalid": swing, "score": D}


def advice_text(a):
    if a["action"] == "wait":
        t = f"**⚪ ATTENDRE** — {a['reason']}"
        if a.get("await"):
            t += f"\nCe que j'attends : {a['await']}"
        return t + "\nSécurité : ne force pas un trade sans avantage clair."
    buy = a["action"] == "long"
    lines = [f"**{'🟢 ACHETER' if buy else '🔴 VENDRE'}** — scalp **{a['h_txt']}** | conviction **{a['conv']}** (score {a['score']:+.1f})",
             "Pourquoi : " + " ; ".join(a["sup"][:4])]
    if a["opp"]:
        lines.append("Contre : " + " ; ".join(a["opp"][:2]))
    lines += [a["entry_txt"],
              f"Stop **{a['sl']:.5f}** ({a['stop_pips']:.1f} pips) | sécurise la moitié à {a['part']:.5f} (1R) puis stop à l'entrée | objectif **{a['tp']:.5f}** (1:{RR:g})",
              f"Taille ≈ **{a['lots']:.2f} lot** (risque ≈ {a['risk_amt']:.0f}$) | sors au plus tard à **{hm(a['exit_by'])}** si ni objectif ni stop",
              f"Annule l'idée si le prix {'repasse sous' if buy else 'repasse au-dessus de'} {a['invalid']:.5f}"]
    if a["caution"]:
        lines.append("⚠️ " + a["caution"])
    lines.append("_Orientation basée sur des données mesurées, pas une certitude._")
    return "\n".join(lines)


def advice_field(ctx, df, events, now):
    try:
        return ("🎯 Ce que je ferais maintenant", cut(advice_text(advise(ctx, df, events, now)), 1000), False)
    except Exception as e:
        print("Conseil indisponible:", e)
        return None


# ============ BRIEFING DU MATIN + APERCU D'ANNONCE (scénarios) ============
def cut(s, n=1000):
    return s if len(s) <= n else s[: n - 1] + "…"


def num(s):
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(s or ""))
    return float(m.group().replace(",", ".")) if m else None


def dollar_votes(drivers):
    g, a, j = drivers.get("GBP/USD"), drivers.get("AUD/USD"), drivers.get("USD/JPY")
    if None in (g, a, j):
        return None
    weak = int(g > 0.03) + int(a > 0.03) + int(j < -0.03)
    strong = int(g < -0.03) + int(a < -0.03) + int(j > 0.03)
    return weak, strong, f"GBP/USD {g:+.2f}%, AUD/USD {a:+.2f}%, USD/JPY {j:+.2f}%"


def morning_briefing(ctx, events, df, daily, now):
    """Scénario du jour : facteurs mesurés, niveaux, invalidation, fenêtres. Un scénario n'est PAS une prévision."""
    last = float(df.Close.iloc[-1])
    night_start = now.normalize() - pd.Timedelta(hours=2)          # ≈ ouverture asiatique (22h UTC veille)
    n = df[df.index >= night_start]
    if len(n) < 30:
        return
    night_move = (last - float(n.Open.iloc[0])) / PIP
    asia = df[(df.index >= now.normalize()) & (df.index < now.normalize() + pd.Timedelta(hours=7))]
    base = asia if len(asia) > 20 else n
    ah, al = float(base.High.max()), float(base.Low.min())
    drivers = get_drivers(night_start)

    comp, total = [], 0.0
    if abs(night_move) >= 5:
        s = float(np.sign(night_move)); total += s
        comp.append(f"{'➕' if s > 0 else '➖'} EUR/USD {night_move:+.0f} pips depuis l'ouverture asiatique")
    else:
        comp.append("▫️ EUR/USD calme depuis l'ouverture asiatique")
    dv = dollar_votes(drivers)
    if dv:
        weak, strong, detail = dv
        if weak >= 2:
            total += 1; comp.append(f"➕ Dollar faible face à plusieurs devises ({detail})")
        elif strong >= 2:
            total -= 1; comp.append(f"➖ Dollar fort face à plusieurs devises ({detail})")
        else:
            comp.append(f"▫️ Dollar sans direction nette ({detail})")
    y = drivers.get("US10Y")
    if y is not None and abs(y) > 0.02:
        s = -float(np.sign(y)); total += 0.5 * s
        comp.append(f"{'➕' if s > 0 else '➖'} Rendement US 10 ans {y:+.3f} pt ({'moins' if y < 0 else 'plus'} de soutien au dollar)")
    ns = ctx["news_score"]
    if abs(ns) >= 0.5:
        total += float(np.clip(ns * 0.5, -1, 1))
        comp.append(f"{'➕' if ns > 0 else '➖'} Presse orientée {'euro' if ns > 0 else 'dollar'} (score {ns:+.1f}, {ctx['n_headlines']} titres)")
    if daily and daily["trend"]:
        total += 0.5 * daily["trend"]
        comp.append(f"{'➕' if daily['trend'] > 0 else '➖'} Fond journalier {'haussier' if daily['trend'] > 0 else 'baissier'}")

    dirn = 1 if total >= 1.5 else -1 if total <= -1.5 else 0
    aligned = sum(1 for c in comp if c.startswith("➕" if dirn > 0 else "➖")) if dirn else 0
    label = "🟢 HAUSSIER" if dirn > 0 else "🔴 BAISSIER" if dirn < 0 else "⚪ SANS BIAS CLAIR"
    color = GREEN if dirn > 0 else RED if dirn < 0 else GREY

    lv = []
    if daily:
        lv += [("plus haut de la veille", daily["pdh"]), ("plus bas de la veille", daily["pdl"]),
               ("EMA20 journalière", daily["ema20"]), ("EMA50 journalière", daily["ema50"])]
    lv += [("haut asiatique", ah), ("bas asiatique", al)]
    for k in range(int(np.floor((last - 0.01) / 0.005)), int(np.ceil((last + 0.01) / 0.005)) + 1):
        lv.append((f"niveau rond {k * 0.005:.4f}", round(k * 0.005, 5)))
    def _dedupe(arr):
        out = []
        for x in arr:
            if not out or abs(x[1] - out[-1][1]) > 1.5 * PIP:
                out.append(x)
        return out[:3]
    above = _dedupe(sorted([x for x in lv if x[1] > last + 5 * PIP], key=lambda x: x[1]))
    below = _dedupe(sorted([x for x in lv if x[1] < last - 5 * PIP], key=lambda x: -x[1]))
    fmt = lambda arr: " → ".join(f"{p:.5f} ({nm}, {abs(p - last) / PIP:.0f} pips)" for nm, p in arr) or "aucun niveau proche"

    if dirn > 0:
        main = (f"Rebond/hausse favorisé ({aligned} facteur(s) alignés). Achats sur repli plutôt que poursuite.\n"
                f"Cibles : {fmt(above)}\nInvalidation : retour sous {below[0][1]:.5f} ({below[0][0]})" if below else f"Cibles : {fmt(above)}")
        alt = f"Si cassure sous {below[0][1]:.5f} : retour au fond, ventes des rebonds vers {fmt(below[1:])}" if below else "—"
    elif dirn < 0:
        main = (f"Baisse favorisée ({aligned} facteur(s) alignés). Ventes sur rebond.\n"
                f"Cibles : {fmt(below)}\nInvalidation : retour au-dessus de {above[0][1]:.5f} ({above[0][0]})" if above else f"Cibles : {fmt(below)}")
        alt = f"Si cassure au-dessus de {above[0][1]:.5f} : rebond plus large vers {fmt(above[1:])}" if above else "—"
    else:
        main = (f"Pas de biais net : les facteurs se neutralisent. Option prudente : ne pas trader, ou scalps de range entre "
                f"{below[0][1]:.5f} et {above[0][1]:.5f}." if above and below else "Pas de biais net.")
        alt = "Une cassure franche d'un côté avec volume/momentum donnera la direction du jour."
    if daily and dirn and dirn != daily["trend"] and daily["trend"]:
        main += "\n⚠️ Contre le fond journalier : mouvement potentiellement plus court et plus fragile, scalps courts et stops serrés."

    dd = df[df.index >= now.normalize()]
    used = (float(dd.High.max()) - float(dd.Low.min())) / PIP
    amp = f"Amplitude moyenne 10 jours : {daily['adr10']:.0f} pips | déjà parcourus aujourd'hui : {used:.0f} pips" if daily and daily.get("adr10") else f"Déjà parcourus aujourd'hui : {used:.0f} pips"

    now_p = now.tz_convert(TZ)
    today_fr = now_p.date()
    ev_today = [e for e in events if e["time"].tz_convert(TZ).date() == today_fr and e["time"] > now]
    ag = [f"{'🔴' if e['impact']=='High' else '🟠'} {hm(e['time'])} {ev_name(e)} — prévu {e['forecast'] or '?'} / préc. {e['previous'] or '?'}" for e in ev_today]
    nxt = next((e for e in ev_today if e["impact"] == "High"), None)
    if nxt:
        end_win = nxt["time"] - pd.Timedelta(minutes=MAX_HOLD_MIN + 5)
        win = (f"Fenêtre de scalp sans annonce : maintenant → {hm(end_win)}" if end_win > now
               else "Plus de fenêtre de scalp avant l'annonce : attendre la réaction")
        ag.append(f"\n⏳ Le scénario est valable jusqu'à {hm(nxt['time'])} ({ev_name(nxt)}), puis réévalué. {win}.")
    else:
        ag.append("\nAucune annonce majeure à venir aujourd'hui : journée pilotée par les flux et la liquidité des sessions.")
    liq0, liq1 = pd.Timestamp(f"{now.date()} 12:00", tz="UTC"), pd.Timestamp(f"{now.date()} 16:00", tz="UTC")
    ag.append(f"Liquidité maximale (Londres + New York) : {hm(liq0)}–{hm(liq1)}")

    heads = "\n".join(f"{'🟢' if s>0 else '🔴' if s<0 else '•'} {fr(re.sub(r' - [^-]+$', '', t))[:130]}" for t, s in ctx["headlines"]) or "Aucun titre exploitable"
    discord(f"🌅 Briefing de la journée — scénario {label}",
            "Un scénario n'est pas une prévision : il repose sur des faits mesurés, il a un niveau d'invalidation, "
            "et il est réévalué après chaque annonce majeure.", color,
            [advice_field(ctx, df, events, now), outlook_field(events, now),
             ("🧭 Scénario principal", cut(main), False), ("🔀 Scénario alternatif", cut(alt), False),
             ("📊 Facteurs mesurés", cut("\n".join(comp)), False), ("📏 Amplitude", amp, False),
             ("🗓 Agenda & fenêtres (heure de Paris)", cut("\n".join(ag)), False), ("📰 Ce que dit la presse", cut(heads), False)])


def event_kind(e):
    n = e["name"].lower()
    if any(k in n for k in ("speaks", "speech", "press conference", "fomc", "minutes", "rate decision", "rate statement",
                            "monetary policy", "testifies", "president", "governor", "chair")):
        return "cb", True
    return "data", not any(k in n for k in ("unemployment", "jobless", "claims"))


def get_event_chatter(e, max_items=4):
    """Titres de presse récents sur les attentes autour de CETTE annonce."""
    name = re.sub(r"\b(m/m|y/y|q/q|flash|prelim|final|revised)\b", "", e["name"], flags=re.I).strip()
    zone = "US" if e["country"] == "USD" else "eurozone"
    out, seen = [], set()
    try:
        r = requests.get("https://news.google.com/rss/search", timeout=15, headers={"User-Agent": "Mozilla/5.0"},
                         params={"q": f"{zone} {name} expectations OR preview OR forecast when:2d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        for it in ET.fromstring(r.content).iter("item"):
            t = (it.findtext("title") or "").strip()
            if t and t not in seen:
                seen.add(t)
                m = re.search(r" - ([^-]+)$", t)
                out.append((re.sub(r" - [^-]+$", "", t), m.group(1) if m else ""))
    except Exception as ex:
        print("Bruits d'annonce indisponibles:", ex)
    return out[:max_items]


def event_preview(e, ctx, df, now):
    m = int((e["time"] - now).total_seconds() / 60)
    fc, pv = e["forecast"], e["previous"]
    f_, p_ = num(fc), num(pv)
    expect = "Consensus indisponible"
    if f_ is not None and p_ is not None:
        expect = f"Le consensus attend {'une hausse' if f_ > p_ else 'une baisse' if f_ < p_ else 'une stabilité'} ({pv} → {fc})"
    kind, higher_good = event_kind(e)
    cur = e["country"]
    if kind == "data":
        better, worse = ("AU-DESSUS", "EN DESSOUS") if higher_good else ("EN DESSOUS", "AU-DESSUS")
        if cur == "USD":
            eb, ew = "dollar soutenu → EUR/USD plutôt ⬇️", "dollar pénalisé → EUR/USD plutôt ⬆️"
        else:
            eb, ew = "euro soutenu → EUR/USD plutôt ⬆️", "euro pénalisé → EUR/USD plutôt ⬇️"
        scen = (f"Chiffre {better} du consensus ({fc or '?'}) : {eb}\nChiffre {worse} du consensus : {ew}\n"
                f"Proche du consensus : réaction souvent faible ou ambiguë → ne pas trader le 1er mouvement.")
    else:
        if cur == "USD":
            scen = "Ton plus ferme (hawkish) : dollar soutenu → EUR/USD plutôt ⬇️\nTon plus accommodant (dovish) : dollar pénalisé → EUR/USD plutôt ⬆️"
        else:
            scen = "Ton plus ferme (hawkish) : euro soutenu → EUR/USD plutôt ⬆️\nTon plus accommodant (dovish) : euro pénalisé → EUR/USD plutôt ⬇️"
    last = float(df.Close.iloc[-1])
    drift = (last - float(df.Close.asof(now - pd.Timedelta(hours=1)))) / PIP
    pos = f"Prix sur 1h : {drift:+.1f} pips ({'positionnement déjà en cours' if abs(drift) > 8 else 'marché plutôt calme avant l’annonce'})"
    chat = get_event_chatter(e)
    chat_txt = "\n".join(f"• {fr(t)[:140]}" + (f" ({s})" if s else "") for t, s in chat) or "Aucun titre pertinent trouvé"
    discord(f"⚠️ {ev_name(e)} dans {m} min ({hm(e['time'])} Paris)",
            f"{expect}\nPrévu : {fc or '?'} | Précédent : {pv or '?'}\nContexte actuel : {bias_label(ctx['bias']) if ctx else 'n/a'}",
            ORANGE,
            [advice_field(ctx, df, [e], now), ("🎲 Scénarios selon le chiffre", scen, False), ("📈 Positionnement", pos, False),
             ("🗞 Attentes dans la presse (pas d'infos privées)", cut(chat_txt), False),
             ("🧭 Plan du bot", f"Aucun trade jusqu'à +3 min après la publication. Ensuite j'observe la réaction pendant 30 min et je ne propose un trade que dans le sens de l'impulsion, sur pullback.", False)])



# ============ AVIS SUR LES ANNONCES À VENIR (seulement si clair) ============
def event_outlook(e):
    """Avis sur une annonce à venir, UNIQUEMENT si le signal est clair (sinon None = je ne dis rien)."""
    if e["impact"] != "High":
        return None
    kind, higher_good = event_kind(e)
    if kind != "data":
        return None
    f_, p_ = num(e["forecast"]), num(e["previous"])
    if f_ is None or p_ is None or f_ == p_:
        return None
    diff = f_ - p_
    rel = abs(diff) / max(abs(p_), 1e-9)
    pct = "%" in str(e["forecast"]) or "%" in str(e["previous"])
    if not (rel >= 0.05 or (pct and abs(diff) >= 0.1)):
        return None
    improving = (diff > 0) == higher_good
    cur = e["country"]
    eff = (-1 if improving else 1) if cur == "USD" else (1 if improving else -1)
    verdict = "✅ plutôt favorable à l'EUR/USD ⬆️" if eff > 0 else "⚠️ plutôt défavorable à l'EUR/USD ⬇️"
    return (f"{jhm(e['time'])} {ev_name(e)} : attendu {e['forecast']} (préc. {e['previous']}) → "
            f"{'amélioration' if improving else 'détérioration'} attendue pour {'les États-Unis' if cur == 'USD' else 'la zone euro'} → {verdict}")


def outlook_field(events, now, horizon_h=36):
    lines = []
    for e in events:
        if now < e["time"] <= now + pd.Timedelta(hours=horizon_h):
            o = event_outlook(e)
            if o:
                lines.append(o)
    if not lines:
        return None
    lines.append("_Base : consensus vs chiffre précédent. Le prix réagit surtout à l'écart avec le consensus : indice, pas prévision._")
    return ("🔮 Annonces à venir : bon ou mauvais pour l'EUR/USD ?", cut("\n".join(lines[:5])), False)



# ============ INDICATEURS ============
def add_indicators(df):
    df = df.copy()
    c = df.Close
    df["ema20"] = c.ewm(span=20, adjust=False).mean()
    prev = c.shift(1)
    tr = pd.concat([df.High - df.Low, (df.High - prev).abs(), (df.Low - prev).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    df["hi20"] = df.High.rolling(20).max().shift(1)
    df["lo20"] = df.Low.rolling(20).min().shift(1)
    for tf, name in (("5min", "t5"), ("15min", "t15")):
        r = c.resample(tf).last().dropna()
        trend = np.sign(r.ewm(span=20, adjust=False).mean() - r.ewm(span=50, adjust=False).mean())
        df[name] = trend.shift(1).reindex(df.index, method="ffill")  # bougies terminées uniquement
    return df.dropna()


# ============ CERVEAU : annonces ============
def news_state(now, events):
    for e in events:
        m = (e["time"] - now).total_seconds() / 60
        if e["impact"] == "High" and (-3 <= m <= MAX_HOLD_MIN + 5):
            if m >= 0:
                return "block", f"annonce majeure dans {int(m)} min ({ev_name(e)}) : un trade de {MAX_HOLD_MIN} min serait pris dedans"
            return "block", f"{ev_name(e)} vient de tomber, spread et volatilité instables"
        if e["impact"] == "Medium" and (-2 <= m <= 10):
            return "block", f"annonce moyenne proche ({ev_name(e)})"
    for e in events:
        m = (e["time"] - now).total_seconds() / 60
        if e["impact"] == "High" and (-30 <= m < -3):
            return "post", e
    return "clear", None


# ============ CERVEAU : decision ============
def decide(df, i, events, ctx=None):
    row = df.iloc[i]
    now = df.index[i] + pd.Timedelta(minutes=1)
    hour = now.hour
    if not (TRADE_HOURS_UTC[0] <= hour < TRADE_HOURS_UTC[1]) or now.weekday() >= 5:
        return {"reason": "hors session (Londres/New York uniquement)"}
    if now.weekday() == 4 and hour >= 16:
        return {"reason": "vendredi fin de session, liquidité faible"}
    state, info = news_state(now, events)
    if state == "block":
        return {"reason": info}
    atr_pips = row.atr / PIP
    if atr_pips < 0.6:
        return {"reason": f"marché trop calme (ATR {atr_pips:.1f} pips), pas assez de mouvement pour viser {RR:g}R"}
    if atr_pips > 6:
        return {"reason": f"volatilité excessive (ATR {atr_pips:.1f} pips)"}

    o, h, l, c = row.Open, row.High, row.Low, row.Close
    rng = max(h - l, 1e-9)
    lw, uw = min(o, c) - l, h - max(o, c)
    cands = []
    if row.t5 > 0 and row.t15 > 0 and l <= row.ema20 < c and c > o:
        cands.append(("long", "Pullback sur EMA20 dans une tendance haussière 5m/15m", 45))
    if row.t5 < 0 and row.t15 < 0 and h >= row.ema20 > c and c < o:
        cands.append(("short", "Pullback sur EMA20 dans une tendance baissière 5m/15m", 45))
    if l < row.lo20 and c > row.lo20 and lw > 0.5 * rng and rng > 0.8 * row.atr:
        cands.append(("long", "Balayage de liquidité sous le plus bas des 20 min puis rejet (piège à vendeurs)", 40))
    if h > row.hi20 and c < row.hi20 and uw > 0.5 * rng and rng > 0.8 * row.atr:
        cands.append(("short", "Balayage de liquidité au-dessus du plus haut des 20 min puis rejet (piège à acheteurs)", 40))
    if c > row.hi20 and rng > 1.3 * row.atr and c > o and (c - l) > 0.7 * rng and row.t5 > 0:
        cands.append(("long", "Cassure impulsive du plus haut des 20 min, momentum fort", 38))
    if c < row.lo20 and rng > 1.3 * row.atr and c < o and (h - c) > 0.7 * rng and row.t5 < 0:
        cands.append(("short", "Cassure impulsive du plus bas des 20 min, momentum fort", 38))
    if not cands:
        return {"reason": "aucun setup valide (le marché ne donne rien de propre)"}

    side, why, score = max(cands, key=lambda x: x[2])
    reasons, sgn = [why], (1 if side == "long" else -1)

    for name, label, ok, ko in (("t5", "5m", 15, -10), ("t15", "15m", 15, -20)):
        if row[name] == sgn:
            score += ok; reasons.append(f"Tendance {label} alignée")
        elif row[name] == -sgn:
            score += ko; reasons.append(f"Contre la tendance {label} (pénalité)")
    if 12 <= hour < 16:
        score += 10; reasons.append("Chevauchement Londres/New York (liquidité maximale)")
    else:
        score += 5
    if (side == "long" and row.rsi > 72) or (side == "short" and row.rsi < 28):
        score -= 15; reasons.append(f"RSI extrême ({row.rsi:.0f}), risque d'épuisement")
    else:
        score += 5

    if state == "post":
        ref = df.Close.asof(info["time"])
        move = (c - ref) / PIP
        if abs(move) >= 4:
            if np.sign(move) == sgn:
                score += 10; reasons.append(f"Cohérent avec la réaction à {ev_name(info)} ({move:+.0f} pips depuis l'annonce)")
            else:
                score -= 25; reasons.append(f"Contre la réaction à {ev_name(info)} ({move:+.0f} pips)")

    if ctx:
        b = ctx["bias"]
        if abs(b) >= 2:
            if np.sign(b) == sgn:
                score += 10; reasons.append(f"Contexte macro/news aligné ({bias_label(b)}, score {b:+.1f})")
            else:
                score -= 25; reasons.append(f"Contexte macro/news CONTRE le trade ({bias_label(b)}, score {b:+.1f})")
        d = ctx.get("daily")
        if d and d["trend"] != 0:
            if d["trend"] == sgn:
                score += 5; reasons.append("Tendance journalière alignée")
            else:
                score -= 5; reasons.append("Contre la tendance journalière")

    dist = ((c - l) if side == "long" else (h - c)) + 0.5 * row.atr
    stop_pips = max(MIN_STOP_PIPS, dist / PIP)
    if stop_pips > MAX_STOP_PIPS:
        return {"reason": f"setup valide mais stop trop large ({stop_pips:.0f} pips), ratio risque/gain dégradé"}
    tp_pips = stop_pips * RR
    entry = c
    sl, tp = entry - sgn * stop_pips * PIP, entry + sgn * tp_pips * PIP

    # Obstacles : plus haut/bas de la veille et niveaux ronds entre l'entrée et le TP
    levels = [round(k * 0.005, 5) for k in range(int(np.ceil(min(entry, tp) / 0.005)), int(np.floor(max(entry, tp) / 0.005)) + 1)]
    if ctx and ctx.get("daily"):
        levels += [ctx["daily"]["pdh"], ctx["daily"]["pdl"]]
    for lvl in levels:
        if min(entry, tp) < lvl < max(entry, tp) and abs(lvl - entry) < 0.7 * abs(tp - entry):
            score -= 10; reasons.append(f"Obstacle : niveau clé {lvl:.5f} avant le TP"); break

    nxt = next((e for e in events if e["time"] > now and e["impact"] == "High"), None)
    if nxt:
        reasons.append(f"Prochaine annonce majeure : {ev_name(nxt)} à {hm(nxt['time'])} (heure de Paris)")

    if score < MIN_SCORE:
        return {"reason": f"setup '{why}' détecté mais score {score}/100 < {MIN_SCORE} (confluence insuffisante)"}

    risk_amt = CAPITAL * RISK_PCT / 100
    lots = max(0.01, np.floor(risk_amt / (stop_pips * PIP_VALUE_PER_LOT) * 100) / 100)
    return {"side": side, "score": int(min(score, 100)), "entry": entry, "sl": sl, "tp": tp,
            "stop_pips": stop_pips, "tp_pips": tp_pips, "lots": lots, "risk_amt": risk_amt,
            "reasons": reasons, "time": now}


def send_signal(s):
    exit_by = s["time"] + pd.Timedelta(minutes=MAX_HOLD_MIN)
    buy = s["side"] == "long"
    discord(f"{'🟢 ACHAT' if buy else '🔴 VENTE'} EUR/USD — confiance {s['score']}/100",
            "**Pourquoi :**\n" + "\n".join(f"• {r}" for r in s["reasons"]) +
            "\n\n❌ Annule si le prix atteint déjà le TP avant ton entrée, ou si une annonce tombe.",
            GREEN if buy else RED,
            [("📍 Entrée", f"**{s['entry']:.5f}**\nvalide 5 min", True),
             ("🛑 Stop", f"**{s['sl']:.5f}**\n{s['stop_pips']:.1f} pips", True),
             ("🎯 Take profit", f"**{s['tp']:.5f}**\n{s['tp_pips']:.1f} pips (1:{RR:g})", True),
             ("📦 Taille", f"**{s['lots']:.2f} lot**\nrisque ≈ {s['risk_amt']:.0f}$ ({RISK_PCT:g}%)", True),
             ("⏱ Sortie max", f"{hm(exit_by)} (heure de Paris)\nferme si ni TP ni SL", True)])


# ============ BACKTEST (partie technique uniquement) ============
def backtest():
    print("Téléchargement des données (7 jours, 1 minute)...")
    df = add_indicators(get_candles("7d"))
    print(f"{len(df)} bougies. Le backtest teste seulement la partie TECHNIQUE (le contexte macro/news passé n'est pas disponible).")
    trades, last, spr = [], -999, SPREAD_PIPS * PIP
    for i in range(60, len(df) - MAX_HOLD_MIN - 2):
        if i - last < COOLDOWN_MIN:
            continue
        s = decide(df, i, [])
        if "side" not in s:
            continue
        last = i
        sgn = 1 if s["side"] == "long" else -1
        entry = s["entry"] + (spr if sgn == 1 else 0)
        risk, res = abs(entry - s["sl"]), None
        for j in range(i + 1, i + 1 + MAX_HOLD_MIN):
            b = df.iloc[j]
            hi, lo = (b.High, b.Low) if sgn == 1 else (b.High + spr, b.Low + spr)
            hit_sl = lo <= s["sl"] if sgn == 1 else hi >= s["sl"]
            hit_tp = hi >= s["tp"] if sgn == 1 else lo <= s["tp"]
            if hit_sl: res = -1.0; break
            if hit_tp: res = RR; break
        if res is None:
            res = sgn * (df.iloc[i + MAX_HOLD_MIN].Close - entry) / risk
        trades.append(res)
    if not trades:
        print("Aucun trade généré."); return
    r = np.array(trades)
    eq = np.cumsum(r)
    dd = (np.maximum.accumulate(eq) - eq).max()
    print(f"\nTrades : {len(r)} | Win rate : {(r > 0).mean() * 100:.0f}% | Espérance : {r.mean():+.2f} R/trade")
    print(f"Total : {r.sum():+.1f} R | Drawdown max : {dd:.1f} R")
    print("Espérance négative = stratégie perdante sur cette période : ne la trade pas telle quelle.")


# ============ LIVE ============
def live(minutes):
    end = time.time() + minutes * 60
    events, ev_ts, ctx, ctx_ts, daily, daily_ts, read_ts = [], 0, None, 0, None, 0, 0
    sent_briefs, sent_days, sent_morning = set(), set(), set()
    last_signal, count, last_idle, last_idle_ts = None, 0, None, 0
    discord("🤖 Bot EUR/USD en ligne", f"Surveillance active pendant ~{int(minutes/60)}h. Je ne te dérange que pour un vrai setup, une annonce ou une lecture de marché. Toutes les heures affichées sont en heure de Paris.", GREY)
    while time.time() < end:
        try:
            now = pd.Timestamp.now(tz="UTC")
            if time.time() - ev_ts > 6 * 3600:
                ev = get_events()
                if ev is not None:
                    events, ev_ts = ev, time.time()
            if time.time() - daily_ts > 3600:
                daily, daily_ts = get_daily(), time.time()
            df = add_indicators(get_candles("5d")).iloc[:-1]  # retire la bougie en cours
            if time.time() - ctx_ts > 600:
                ctx, ctx_ts = build_context(daily, df, events, now), time.time()
            if ctx and time.time() - read_ts > READ_EVERY_MIN * 60:
                market_read(ctx, events, now, df)
                read_ts = time.time()
            today_fr = now.tz_convert(TZ).date()
            hp = now.tz_convert(TZ).hour
            if ctx and today_fr not in sent_morning and INTRADAY_START_HOUR <= hp < INTRADAY_START_HOUR + 2:
                sent_morning.add(today_fr)
                try:
                    morning_briefing(ctx, events, df, daily, now)
                except Exception as ex:
                    print("Briefing impossible:", ex)
            if today_fr not in sent_days:
                sent_days.add(today_fr)
                today = [e for e in events if e["time"].tz_convert(TZ).date() == today_fr]
                if today:
                    discord("📅 Agenda du jour (heure de Paris)", "\n".join(
                        f"{'🔴' if e['impact']=='High' else '🟠'} {hm(e['time'])} — {ev_name(e)}" for e in today), BLUE,
                        [advice_field(ctx, df, events, now) if ctx else None, outlook_field(events, now)])
            for e in events:
                m = (e["time"] - now).total_seconds() / 60
                key = (e["time"], e["title"])
                if e["impact"] == "High" and 0 < m <= 60 and key not in sent_briefs:
                    sent_briefs.add(key)
                    if ctx:
                        event_preview(e, ctx, df, now)
            s = decide(df, len(df) - 1, events, ctx)
            if "side" in s:
                if (last_signal is None or (now - last_signal).total_seconds() > COOLDOWN_MIN * 60) and count < MAX_SIGNALS_PER_SESSION:
                    send_signal(s)
                    last_signal, count = now, count + 1
            else:
                r = s["reason"]
                if r != last_idle and time.time() - last_idle_ts > 1800 and "hors session" not in r:
                    discord("⏸ Pas de trade", f"{r}\n\nAucun signal technique précis pour l'instant : le conseil ci-dessous est une orientation, pas un déclencheur.",
                            GREY, [advice_field(ctx, df, events, now) if ctx else None])
                    last_idle, last_idle_ts = r, time.time()
        except Exception as ex:
            print("Erreur boucle:", ex)
        time.sleep(60 - time.time() % 60 + 5)
    discord("🛑 Session terminée", "Le bot reprendra à la prochaine session planifiée.", GREY)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--test-discord", action="store_true")
    ap.add_argument("--minutes", type=int, default=305)
    a = ap.parse_args()
    if a.backtest:
        backtest()
    elif a.test_discord:
        discord("✅ Test Discord OK", "Le bot peut t'écrire sur ce salon.", GREEN)
    else:
        live(a.minutes)
