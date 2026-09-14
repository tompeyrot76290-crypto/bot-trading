import json
import os
import urllib.request
from datetime import datetime
import pandas as pd
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
temps_en_minutes = heure_actuelle * 60 + minute_actuelle

# --- 1. HORAIRES DE SESSION (08:00 - 18:00 Heure Française) ---
if heure_actuelle < 8 or heure_actuelle >= 18:
    print(f"Hors session européenne ({heure_actuelle}h{minute_actuelle:02d} Paris). Veille passive.")
    exit(0)

# --- 2. KILLZONES DE LIQUIDITÉ (Filtre du creux de mi-journée) ---
if (11 * 60 + 30) <= temps_en_minutes < (14 * 60):
    print(f"Pause institutionnelle ({heure_actuelle}h{minute_actuelle:02d}). Rejet automatique.")
    exit(0)

actifs_forex = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X",
    "NZDUSD=X", "USDCHF=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X",
    "EURAUD=X", "EURCAD=X", "GBPCHF=X", "AUDJPY=X", "CADJPY=X",
    "EURNZD=X", "GBPAUD=X",
]

def calculer_atr(data, periode=14):
    high = data["High"]
    low = data["Low"]
    close = data["Close"].shift(1)
    tr1 = high - low
    tr2 = (high - close).abs()
    tr3 = (low - close).abs()
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
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    try:
        urllib.request.urlopen(req)
        print("Signal A+ transmis à Discord.")
    except Exception as e:
        print(f"Erreur envoi Discord : {e}")

# --- ALGORITHME D'EXÉCUTION : 100% SETUP A+ ---
for ticker in actifs_forex:
    nom_paire = ticker.replace("=X", "")
    try:
        df_m15 = yf.download(ticker, period="5d", interval="15m", progress=False)
        df_d1 = yf.download(ticker, period="60d", interval="1d", progress=False)

        if len(df_m15) < 30 or len(df_d1) < 20:
            continue

        close_m15 = df_m15["Close"].squeeze() if hasattr(df_m15["Close"], "squeeze") else df_m15["Close"]
        high_m15 = df_m15["High"].squeeze() if hasattr(df_m15["High"], "squeeze") else df_m15["High"]
        low_m15 = df_m15["Low"].squeeze() if hasattr(df_m15["Low"], "squeeze") else df_m15["Low"]

        close_d1 = df_d1["Close"].squeeze() if hasattr(df_d1["Close"], "squeeze") else df_d1["Close"]
        sma20_d1 = float(close_d1.rolling(20).mean().iloc[-1])
        tendance_d1_haussiere = float(close_d1.iloc[-1]) > sma20_d1

        prix_actuel = float(close_m15.iloc[-1])
        sma20_m15 = float(close_m15.rolling(20).mean().iloc[-1])
        atr = float(calculer_atr(df_m15))
        pip_size = 0.01 if "JPY" in nom_paire else 0.0001

        # --- FILTRE 1 : ANTI-CHASING ATR ---
        distance_mm20_brute = abs(prix_actuel - sma20_m15)
        limite_sur_extension = atr * 1.8
        
        if distance_mm20_brute > limite_sur_extension:
            print(f"[{nom_paire}] Rejet : Sur-extension (Anti-Chasing actif).")
            continue

        # --- FILTRE 2 : BRUIT DE MARCHÉ ---
        sl_pips_brut = (atr * 1.5) / pip_size
        if sl_pips_brut < 6.0:
            print(f"[{nom_paire}] Rejet : Volatilité insuffisante (Bruit).")
            continue

        # --- FILTRE 3 : CASSURE M15 ---
        plus_haut_10 = float(high_m15.iloc[-11:-1].max())
        plus_bas_10 = float(low_m15.iloc[-11:-1].min())

        signal = None
        if prix_actuel > plus_haut_10:
            signal = "ACHAT (LONG)"
        elif prix_actuel < plus_bas_10:
            signal = "VENTE (SHORT)"

        if signal:
            is_long = signal == "ACHAT (LONG)"

            # --- FILTRE 4 (NOUVEAU) : ALIGNEMENT STRICT (ZÉRO CONTRE-TENDANCE) ---
            est_aligne = (is_long and tendance_d1_haussiere) or (not is_long and not tendance_d1_haussiere)
            
            if not est_aligne:
                print(f"[{nom_paire}] Rejet : Signal à contre-courant du Daily ignoré.")
                continue # Le bot abandonne et passe à la paire suivante

            # SI ON ARRIVE ICI, LE SETUP EST PARFAIT (A+)
            sl_pips = round(sl_pips_brut, 1)
            sl_pips = max(6.0, min(sl_pips, 30.0))
            
            rr = 1.5 # Seul le RR optimal est conservé
            tp_pips = round(sl_pips * rr, 1)
            contexte = "Flux Institutionnel Aligné (D1 + M15) 🏆"

            if is_long:
                sl_price = round(prix_actuel - (sl_pips * pip_size), 5)
                tp_price = round(prix_actuel + (tp_pips * pip_size), 5)
                emoji_ordre = "🟢"
            else:
                sl_price = round(prix_actuel + (sl_pips * pip_size), 5)
                tp_price = round(prix_actuel - (tp_pips * pip_size), 5)
                emoji_ordre = "🔴"

            nb_dec = 3 if "JPY" in nom_paire else 5
            nom_killzone = "London Open" if temps_en_minutes < (11 * 60 + 30) else "NY Overlap"

            message = (
                f"🏆 **SETUP A+ : FLUX ALIGNÉ (Zero Bruit)** 🏆\n\n"
                f"💱 **Actif :** {nom_paire}\n"
                f"📊 **Ordre :** {signal} {emoji_ordre}\n"
                f"💶 **Entrée :** {prix_actuel:.{nb_dec}f}\n"
                f"🛑 **Stop Loss :** {sl_price:.{nb_dec}f} ({sl_pips} pips)\n"
                f"🎯 **Take Profit :** {tp_price:.{nb_dec}f} ({tp_pips} pips — RR 1:{rr})\n"
                f"🏛️ **Killzone :** {nom_killzone}\n"
                f"🔥 **Contexte :** {contexte}\n"
                f"⏱️ **Durée estimée :** 45 à 90 min\n"
                f"⏳ **Timing :** Clôture bougie dans {minutes_restantes} min."
            )

            envoyer_discord(message)
            break

    except Exception as e:
        print(f"Erreur sur {nom_paire} : {e}")
