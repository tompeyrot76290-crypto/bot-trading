import json
import os
import urllib.request
from datetime import datetime

import yfinance as yf

# Webhook Discord configuré dans les secrets GitHub
webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

# --- GESTION STRICTE DU FUSEAU HORAIRE PARIS ---
try:
    from zoneinfo import ZoneInfo

    maintenant = datetime.now(ZoneInfo("Europe/Paris"))
except ImportError:
    import pytz

    maintenant = datetime.now(pytz.timezone("Europe/Paris"))

heure_actuelle = maintenant.hour
minute_actuelle = maintenant.minute
minutes_restantes = 15 - (minute_actuelle % 15)

# --- 0. FILTRE DES HORAIRES DE TRADING (08:00 - 18:00 Heure Française) ---
if heure_actuelle < 8 or heure_actuelle >= 18:
    print(
        f"Hors des horaires de trading actifs ({heure_actuelle}h{minute_actuelle:02d} Paris). Le bot est en veille."
    )
    exit(0)

# Liste complète des 17 paires Forex scannées
actifs_forex = [
    "EURUSD=X",
    "GBPUSD=X",
    "USDJPY=X",
    "AUDUSD=X",
    "USDCAD=X",
    "NZDUSD=X",
    "USDCHF=X",
    "EURGBP=X",
    "EURJPY=X",
    "GBPJPY=X",
    "EURAUD=X",
    "EURCAD=X",
    "GBPCHF=X",
    "AUDJPY=X",
    "CADJPY=X",
    "EURNZD=X",
    "GBPAUD=X",
]


def calculer_atr(data, periode=14):
    high = data["High"]
    low = data["Low"]
    close = data["Close"].shift(1)
    tr1 = high - low
    tr2 = (high - close).abs()
    tr3 = (low - close).abs()
    import pandas as pd

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=periode).mean().iloc[-1]


def envoyer_discord(msg):
    if not webhook_url:
        print("Webhook Discord non configuré.")
        return
    payload = json.dumps({"content": msg}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        urllib.request.urlopen(req)
        print("Signal envoyé sur Discord avec succès.")
    except Exception as e:
        print(f"Erreur envoi Discord: {e}")


# --- ALGORITHME DE TRADING ---
for ticker in actifs_forex:
    nom_paire = ticker.replace("=X", "")
    try:
        # Téléchargement des données M15 et D1
        df_m15 = yf.download(ticker, period="5d", interval="15m", progress=False)
        df_d1 = yf.download(ticker, period="60d", interval="1d", progress=False)

        if len(df_m15) < 30 or len(df_d1) < 20:
            continue

        close_m15 = (
            df_m15["Close"].squeeze()
            if hasattr(df_m15["Close"], "squeeze")
            else df_m15["Close"]
        )
        high_m15 = (
            df_m15["High"].squeeze()
            if hasattr(df_m15["High"], "squeeze")
            else df_m15["High"]
        )
        low_m15 = (
            df_m15["Low"].squeeze()
            if hasattr(df_m15["Low"], "squeeze")
            else df_m15["Low"]
        )

        close_d1 = (
            df_d1["Close"].squeeze()
            if hasattr(df_d1["Close"], "squeeze")
            else df_d1["Close"]
        )
        sma20_d1 = close_d1.rolling(20).mean().iloc[-1]
        tendance_d1_haussiere = close_d1.iloc[-1] > sma20_d1

        prix_actuel = float(close_m15.iloc[-1])
        sma20_m15 = float(close_m15.rolling(20).mean().iloc[-1])
        atr = float(calculer_atr(df_m15))

        pip_size = 0.01 if "JPY" in nom_paire else 0.0001

        # 1. Filtre Anti-Chasing (Sur-extension)
        distance_mm20_pips = abs(prix_actuel - sma20_m15) / pip_size
        if distance_mm20_pips > 25:
            continue

        # 2. Cassure de structure (10 dernières bougies M15)
        plus_haut_10 = float(high_m15.iloc[-11:-1].max())
        plus_bas_10 = float(low_m15.iloc[-11:-1].min())

        signal = None
        if prix_actuel > plus_haut_10:
            signal = "ACHAT (LONG)"
        elif prix_actuel < plus_bas_10:
            signal = "VENTE (SHORT)"

        if signal:
            sl_pips = round((atr * 1.5) / pip_size, 1)
            sl_pips = max(8.0, min(sl_pips, 25.0))

            is_long = signal == "ACHAT (LONG)"

            if (is_long and tendance_d1_haussiere) or (
                not is_long and not tendance_d1_haussiere
            ):
                rr = 1.5
                contexte = "Tendance saine (Aligné D1 / H1)"
            else:
                rr = 1.2
                contexte = "Guérilla / Profit rapide (Contre-tendance D1 ⚠️)"

            tp_pips = round(sl_pips * rr, 1)

            if is_long:
                sl_price = round(prix_actuel - (sl_pips * pip_size), 5)
                tp_price = round(prix_actuel + (tp_pips * pip_size), 5)
                emoji_ordre = "🟢"
            else:
                sl_price = round(prix_actuel + (sl_pips * pip_size), 5)
                tp_price = round(prix_actuel - (tp_pips * pip_size), 5)
                emoji_ordre = "🔴"

            nb_dec = 3 if "JPY" in nom_paire else 5

            message = (
                f"🧠 **SIGNAL M15 RECALIBRÉ (Anti-Chasing)** 🧠\n\n"
                f"💱 **Actif :** {nom_paire}\n"
                f"📊 **Ordre :** {signal} {emoji_ordre}\n"
                f"💶 **Entrée :** {prix_actuel:.{nb_dec}f}\n"
                f"🛑 **Stop Loss :** {sl_price:.{nb_dec}f} ({sl_pips} pips)\n"
                f"🎯 **Take Profit :** {tp_price:.{nb_dec}f} ({tp_pips} pips — RR 1:{rr})\n"
                f"🔥 **Context Macro :** {contexte}\n"
                f"⏱️ **Durée estimée réaliste :** 44 à 74 min (Intraday court)\n"
                f"⏳ **Timing :** Clôture M15 dans {minutes_restantes} min."
            )

            envoyer_discord(message)
            break

    except Exception as e:
        print(f"Erreur sur {nom_paire}: {e}")
