import json
import os
import urllib.request
from datetime import datetime
import pandas as pd
import yfinance as yf

# Webhook Discord configuré dans les secrets GitHub
webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

# --- GESTION DU FUSEAU HORAIRE PARIS ---
try:
    from zoneinfo import ZoneInfo
    maintenant = datetime.now(ZoneInfo("Europe/Paris"))
except ImportError:
    import pytz
    maintenant = datetime.now(pytz.timezone("Europe/Paris"))

heure_actuelle = maintenant.hour
minute_actuelle = maintenant.minute
minutes_restantes = 60 - minute_actuelle

# --- 1. HORAIRES DE SESSION (08:00 - 17:00 Heure Française) ---
# En H1, le dernier trade de la journée européenne doit être pris max à 17h (clôture à 18h)
if heure_actuelle < 8 or heure_actuelle >= 17:
    print(f"Hors session européenne ou fin de journée ({heure_actuelle}h{minute_actuelle:02d}). Veille passive.")
    exit(0)

# --- 2. KILLZONES DE LIQUIDITÉ (Filtre du creux de mi-journée) ---
# On bloque strictement les fausses cassures de la pause de midi (12h et 13h)
if heure_actuelle == 12 or heure_actuelle == 13:
    print(f"Pause institutionnelle H1 ({heure_actuelle}h). Rejet automatique du bruit de midi.")
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
        print("Signal H1 A+ transmis à Discord.")
    except Exception as e:
        print(f"Erreur envoi Discord : {e}")

# --- ALGORITHME H1 : LE SNIPER INSTITUTIONNEL ---
for ticker in actifs_forex:
    nom_paire = ticker.replace("=X", "")
    try:
        # On télécharge les données en 1 Heure (1h)
        df_h1 = yf.download(ticker, period="10d", interval="1h", progress=False)
        df_d1 = yf.download(ticker, period="60d", interval="1d", progress=False)

        if len(df_h1) < 30 or len(df_d1) < 20:
            continue

        close_h1 = df_h1["Close"].squeeze() if hasattr(df_h1["Close"], "squeeze") else df_h1["Close"]
        high_h1 = df_h1["High"].squeeze() if hasattr(df_h1["High"], "squeeze") else df_h1["High"]
        low_h1 = df_h1["Low"].squeeze() if hasattr(df_h1["Low"], "squeeze") else df_h1["Low"]

        close_d1 = df_d1["Close"].squeeze() if hasattr(df_d1["Close"], "squeeze") else df_d1["Close"]
        
        # Tendance Macro (Daily)
        sma20_d1 = float(close_d1.rolling(20).mean().iloc[-1])
        tendance_d1_haussiere = float(close_d1.iloc[-1]) > sma20_d1

        prix_actuel = float(close_h1.iloc[-1])
        sma20_h1 = float(close_h1.rolling(20).mean().iloc[-1])
        atr_h1 = float(calculer_atr(df_h1))
        pip_size = 0.01 if "JPY" in nom_paire else 0.0001

        # --- FILTRE 1 : ANTI-CHASING H1 ---
        distance_mm20_brute = abs(prix_actuel - sma20_h1)
        # En H1, on autorise un peu plus d'élasticité (2.0) car les mouvements sont plus profonds
        limite_sur_extension = atr_h1 * 2.0 
        
        if distance_mm20_brute > limite_sur_extension:
            print(f"[{nom_paire}] Rejet H1 : Sur-extension majeure (Mouvement épuisé).")
            continue

        # --- FILTRE 2 : BRUIT DE MARCHÉ H1 ---
        sl_pips_brut = (atr_h1 * 1.5) / pip_size
        if sl_pips_brut < 10.0: # En H1, si l'ATR demande un Stop Loss < 10 pips, c'est un marché mort.
            print(f"[{nom_paire}] Rejet H1 : Volatilité globale insuffisante.")
            continue

        # --- FILTRE 3 : CASSURE STRUCTURELLE MAJEURE (12 dernières heures) ---
        plus_haut_12 = float(high_h1.iloc[-13:-1].max())
        plus_bas_12 = float(low_h1.iloc[-13:-1].min())

        signal = None
        if prix_actuel > plus_haut_12:
            signal = "ACHAT (LONG)"
        elif prix_actuel < plus_bas_12:
            signal = "VENTE (SHORT)"

        if signal:
            is_long = signal == "ACHAT (LONG)"

            # --- FILTRE 4 : ALIGNEMENT STRICT D1 + H1 (ZÉRO ERREUR) ---
            est_aligne = (is_long and tendance_d1_haussiere) or (not is_long and not tendance_d1_haussiere)
            
            if not est_aligne:
                print(f"[{nom_paire}] Rejet H1 : Cassure à contre-courant de la tendance Daily. Ignoré.")
                continue 

            # LE SETUP EST PARFAIT (A+ MACRO & MICRO)
            sl_pips = round(sl_pips_brut, 1)
            # Stop Loss H1 réaliste (entre 10 et 45 pips selon la volatilité)
            sl_pips = max(10.0, min(sl_pips, 45.0))
            
            rr = 1.5
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
            nom_killzone = "London Session" if heure_actuelle < 12 else "NY Session"

            message = (
                f"🏛️ **SETUP INSTITUTIONNEL H1 (Sniper)** 🏛️\n\n"
                f"💱 **Actif :** {nom_paire}\n"
                f"📊 **Ordre :** {signal} {emoji_ordre}\n"
                f"💶 **Entrée :** {prix_actuel:.{nb_dec}f}\n"
                f"🛑 **Stop Loss :** {sl_price:.{nb_dec}f} ({sl_pips} pips)\n"
                f"🎯 **Take Profit :** {tp_price:.{nb_dec}f} ({tp_pips} pips — RR 1:{rr})\n"
                f"🏢 **Session :** {nom_killzone}\n"
                f"🔥 **Structure :** Cassure 12H alignée avec Flux Daily 🏆\n"
                f"⏱️ **Durée estimée :** 3h à 8h (Swing Intraday)\n"
                f"⏳ **Timing :** Clôture bougie H1 dans {minutes_restantes} min."
            )

            envoyer_discord(message)
            break

    except Exception as e:
        print(f"Erreur sur {nom_paire} : {e}")
