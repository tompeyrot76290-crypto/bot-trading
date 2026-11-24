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
MAX_HOLD_MIN = 45
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
        return {"trend": int(np.sign(e20 - e50)), "ema20": e20, "ema50": e50,
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
    """Lit la journée EN TEMPS RÉEL à partir des vraies données 1 minute (pas de texte au hasard)."""
    now_p = now.tz_convert(TZ)
    start_p = now_p.replace(hour=INTRADAY_START_HOUR, minute=0, second=0, microsecond=0)
    if now_p < start_p:
        start_p -= pd.Timedelta(days=1)
    start = start_p.tz_convert("UTC")
    d = df[df.index >= start]
    if len(d) < 30:
        return None
    last = float(d.Close.iloc[-1])
    move = (last - float(d.Open.iloc[0])) / PIP
    hi, lo = float(d.High.max()), float(d.Low.min())
    pos = (last - lo) / (hi - lo) if hi > lo else 0.5
    c5 = d.Close.resample("5min").last().dropna()
    step = c5.diff().abs().sum()
    er = float(abs(c5.iloc[-1] - c5.iloc[0]) / step) if len(c5) > 2 and step > 0 else 0.0
    slope = float(np.polyfit(np.arange(len(c5)), c5.values, 1)[0] / PIP * 12) if len(c5) > 2 else 0.0
    maxdd = float((c5.cummax() - c5).max() / PIP)
    maxrally = float((c5 - c5.cummin()).max() / PIP)
    if move >= 8 and er >= 0.10:
        dirn, label = 1, "HAUSSIÈRE"
    elif move <= -8 and er >= 0.10:
        dirn, label = -1, "BAISSIÈRE"
    else:
        dirn, label = 0, "EN RANGE (pas de direction nette)"
    quality = "propre" if er >= 0.25 else "heurtée"
    pullback = maxdd if dirn >= 0 else maxrally

    lines = [f"Depuis {INTRADAY_START_HOUR}h (Paris) : **{move:+.1f} pips** → tendance intraday **{label}**" + (f" ({quality})" if dirn else ""),
             f"Plus haut {hi:.5f} / plus bas {lo:.5f} ({(hi-lo)/PIP:.0f} pips) | prix à {pos*100:.0f}% de la fourchette",
             f"Pente {slope:+.1f} pips/h | plus gros repli {pullback:.1f} pips"]
    if dirn and abs(move) >= 15:
        lines.append(f"Après coup : ≈ {abs(move)/8:.0f}x un stop de 8 pips")

    fond = "indisponible"
    daily_notes = []
    if daily:
        t = daily["trend"]
        fond = "BAISSIER" if t < 0 else "HAUSSIER" if t > 0 else "neutre"
        gap = (last - daily["ema20"]) / PIP
        rng20 = daily["hi20"] - daily["lo20"]
        lines.append(f"Fond (journalier) : **{fond}** | EMA20 jour {daily['ema20']:.5f} ({gap:+.0f} pips) | 5 derniers jours {daily['chg5']:+.0f} pips")
        if rng20 > 0 and t != 0:
            ret = (last - daily["lo20"]) / rng20 if t < 0 else (daily["hi20"] - last) / rng20
            daily_notes.append(f"le prix a repris {ret*100:.0f}% de {'la baisse' if t < 0 else 'la hausse'} des 20 derniers jours")
        if dirn and dirn != t and t != 0:
            lines.append(f"⚠️ **Contre-tendance** : mouvement intraday {'haussier' if dirn > 0 else 'baissier'} dans un fond {fond.lower()}")

    # ----- le pourquoi : uniquement des faits mesurés -----
    why = []
    dd = df[df.index >= now.normalize()]
    if daily and len(dd):
        if dd.Low.min() < daily["pdl"] < last:
            why.append(f"Les stops sous le plus bas de la veille ({daily['pdl']:.5f}) ont été balayés puis le prix a repris : schéma de prise de liquidité observé")
        if dd.High.max() > daily["pdh"] > last:
            why.append(f"Les stops au-dessus du plus haut de la veille ({daily['pdh']:.5f}) ont été balayés puis le prix est retombé : schéma de prise de liquidité observé")
    asia = df[(df.index >= now.normalize()) & (df.index < now.normalize() + pd.Timedelta(hours=7))]
    if len(asia) > 20 and now.hour >= 7:
        ah, al = float(asia.High.max()), float(asia.Low.min())
        if last > ah:
            why.append(f"Sortie par le haut du range asiatique ({al:.5f}–{ah:.5f}, {(ah-al)/PIP:.0f} pips) à l'ouverture européenne")
        elif last < al:
            why.append(f"Sortie par le bas du range asiatique ({al:.5f}–{ah:.5f}, {(ah-al)/PIP:.0f} pips) à l'ouverture européenne")

    if drivers and dirn:
        g, a, j = drivers.get("GBP/USD"), drivers.get("AUD/USD"), drivers.get("USD/JPY")
        if None not in (g, a, j):
            votes = (g * dirn > 0.03) + (a * dirn > 0.03) + (j * dirn < -0.03)
            detail = f"GBP/USD {g:+.2f}%, AUD/USD {a:+.2f}%, USD/JPY {j:+.2f}%"
            if votes >= 2:
                why.append(f"Le dollar {'faiblit' if dirn > 0 else 'se renforce'} face à plusieurs devises, pas seulement l'euro ({detail}) : mouvement porté par le dollar")
            elif votes == 0:
                why.append(f"L'euro bouge presque seul ({detail}) : {'force' if dirn > 0 else 'faiblesse'} propre à l'euro plutôt qu'un mouvement général du dollar")
            else:
                why.append(f"Signal dollar mitigé ({detail})")
        y = drivers.get("US10Y")
        if y is not None and abs(y) > 0.02:
            why.append(f"Rendement US 10 ans {y:+.3f} pt depuis {INTRADAY_START_HOUR}h : {'soutient' if (y < 0) == (dirn > 0) else 'contredit'} le mouvement de l'EUR/USD")
    nb = sum(1 for ts, _, s in items if ts >= start and s > 0)
    nr = sum(1 for ts, _, s in items if ts >= start and s < 0)
    if nb or nr:
        why.append(f"Titres de presse depuis {INTRADAY_START_HOUR}h orientés EUR/USD : {nb} favorable(s) à l'euro, {nr} favorable(s) au dollar")
    passed = [ev_name(e) for e in events if start <= e["time"] <= now]
    if passed:
        why.append("Annonces déjà publiées depuis le début de la journée : " + " ; ".join(passed[:4]) + " (valeurs réelles non fournies par ma source)")
    if dirn and daily and dirn != daily["trend"] and daily["trend"] != 0:
        t = daily["trend"]
        notes = "; ".join(daily_notes) if daily_notes else "fond inchangé"
        crossed = (last > daily["ema20"]) if t < 0 else (last < daily["ema20"])
        if crossed:
            tail = f"Le prix a déjà repassé l'EMA20 journalière ({daily['ema20']:.5f}) : début de retournement possible, à confirmer (la tendance de fond ne bascule qu'avec l'EMA20 sous/sur l'EMA50)"
        else:
            tail = f"Tant que le prix reste {'sous' if t < 0 else 'au-dessus de'} l'EMA20 journalière ({daily['ema20']:.5f}), le fond ne change pas"
        why.append(f"Lecture : {'rebond' if t < 0 else 'repli'} dans un fond {fond.lower()} ({notes}). {tail}")
    if not why:
        why.append("Aucun facteur mesurable ne ressort clairement pour l'instant")
    return {"summary": "\n".join(lines), "why": "\n".join("• " + w for w in why), "dirn": dirn}


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
    ia = None
    if df is not None:
        try:
            start_p = now.tz_convert(TZ).replace(hour=INTRADAY_START_HOUR, minute=0, second=0, microsecond=0)
            if now.tz_convert(TZ) < start_p:
                start_p -= pd.Timedelta(days=1)
            ia = intraday_analysis(df, daily, list(events), items, now, get_drivers(start_p.tz_convert("UTC")))
        except Exception as e:
            print("Analyse intraday impossible:", e)
    return {"bias": bias, "macro_score": m_score, "news_score": n_score, "macro_reasons": m_reasons,
            "snap": snap, "headlines": top, "n_headlines": len(items), "daily": daily, "items": items,
            "intraday": ia, "forum": get_forum_chatter()}


def bias_label(b):
    if b >= 2: return "🟢 HAUSSIER EUR/USD"
    if b <= -2: return "🔴 BAISSIER EUR/USD"
    return "⚪ NEUTRE / pas de biais clair"


def market_read(ctx, events, now):
    nxt = [e for e in events if e["time"] > now][:4]
    agenda = "\n".join(f"{'🔴' if e['impact']=='High' else '🟠'} {jhm(e['time'])} — {ev_name(e)}" for e in nxt) or "Rien de notable à venir"
    snap = "\n".join(f"**{k}** : {v}" for k, v in ctx["snap"].items()) or "indisponible"
    heads = "\n".join(f"{'🟢' if s>0 else '🔴' if s<0 else '•'} {fr(re.sub(r' - [^-]+$', '', t))[:150]}" for t, s in ctx["headlines"]) or "Aucun titre récent exploitable"
    d = ctx["daily"]
    daily = "indisponible" if not d else f"tendance {'haussière' if d['trend']>0 else 'baissière' if d['trend']<0 else 'neutre'}\nveille : haut {d['pdh']:.5f} / bas {d['pdl']:.5f}"
    why = "\n".join(f"• {r}" for r in ctx["macro_reasons"]) or "Pas de mouvement macro marquant"
    fields = []
    ia = ctx.get("intraday")
    if ia:
        fields += [(f"📈 Lecture de la journée (temps réel, 1 min)", ia["summary"], False),
                   ("🔎 Pourquoi ça bouge (faits mesurés)", ia["why"], False)]
    fields += [("Marchés liés", snap, True), ("Contexte journalier", daily, True),
               ("Ce qui se dit (presse)", heads, False)]
    if ctx.get("forum"):
        fields.append(("💬 Ambiance traders (Reddit r/Forex)", "\n".join("• " + fr(t)[:140] for t in ctx["forum"]), False))
    fields.append(("Prochaines annonces (heure de Paris)", agenda, False))
    discord(f"🧠 Lecture du marché — {bias_label(ctx['bias'])}",
            f"Score macro {ctx['macro_score']:+.1f} | Score actualités {ctx['news_score']:+.1f} ({ctx['n_headlines']} titres analysés)\n\n{why}\n\n"
            f"_Ce sont des corrélations mesurées, pas une certitude sur les causes._",
            GREEN if ctx["bias"] >= 2 else RED if ctx["bias"] <= -2 else GREY, fields)


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
    sent_briefs, sent_days = set(), set()
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
                market_read(ctx, events, now)
                read_ts = time.time()
            today_fr = now.tz_convert(TZ).date()
            if today_fr not in sent_days:
                sent_days.add(today_fr)
                today = [e for e in events if e["time"].tz_convert(TZ).date() == today_fr]
                if today:
                    discord("📅 Agenda du jour (heure de Paris)", "\n".join(
                        f"{'🔴' if e['impact']=='High' else '🟠'} {hm(e['time'])} — {ev_name(e)}" for e in today), BLUE)
            for e in events:
                m = (e["time"] - now).total_seconds() / 60
                key = (e["time"], e["title"])
                if e["impact"] == "High" and 0 < m <= 30 and key not in sent_briefs:
                    sent_briefs.add(key)
                    bias = f"\nBiais actuel du marché : {bias_label(ctx['bias'])}" if ctx else ""
                    discord(f"⚠️ {ev_name(e)} dans {int(m)} min",
                            f"Heure : {hm(e['time'])} (Paris)\nPrévu : {e['forecast'] or '?'} | Précédent : {e['previous'] or '?'}{bias}\n\n"
                            f"**Plan :** aucun trade jusqu'à +3 min après la publication. Ensuite je cherche un pullback dans le sens de l'impulsion.",
                            ORANGE)
            s = decide(df, len(df) - 1, events, ctx)
            if "side" in s:
                if (last_signal is None or (now - last_signal).total_seconds() > COOLDOWN_MIN * 60) and count < MAX_SIGNALS_PER_SESSION:
                    send_signal(s)
                    last_signal, count = now, count + 1
            else:
                r = s["reason"]
                if r != last_idle and time.time() - last_idle_ts > 1800 and "hors session" not in r:
                    discord("⏸ Pas de trade", r, GREY)
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
