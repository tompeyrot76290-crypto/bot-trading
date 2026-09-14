from datetime import datetime
import json
import os
import urllib.request
import yfinance as yf

webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

maintenant = datetime.now()
heure_actuelle = maintenant.hour
minute_actuelle = maintenant.minute
minutes_restantes = 15 - (minute_actuelle % 15)

# --- 0. FILTRE DES HORAIRES DE TRADING (08:00 - 18:00) ---
if heure_actuelle < 8 or heure_actuelle >= 18:
    print(
        "Hors des horaires de trading actifs (8h-18h). Le bot est en veille."
    )
    exit(0)

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
    "GBPAUD=X",
    "AUDJPY=X",
    "EURCAD=X",
    "AUDCAD=X",
    "NZDJPY=X",
    "CHFJPY=X",
]

# --- 1. FILTRE DU CALENDRIER ÉCONOMIQUE EN DIRECT ---
alerte_eco = False
message_eco = ""
try:
    url_calendrier = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    req = urllib.request.Request(
        url_calendrier, headers={"User-Agent": "Mozilla/5.0"}
    )
    reponse = urllib.request.urlopen(req)
    evenements = json.loads(reponse.read().decode("utf-8"))
    date_jour = maintenant.strftime("%Y-%m-%d")
    for ev in evenements:
        if ev.get("impact") == "High" and date_jour in ev.get("date", ""):
            heure_ev = datetime.strptime(
                ev.get("date")[:19], "%Y-%m-%dT%H:%M:%S"
            )
            diff = (heure_ev - maintenant).total_seconds() / 60
            if -15 <= diff <= 45:
                alerte_eco = True
                message_eco = f"Annonce majeure : '{ev.get('title')}' ({ev.get('country')})"
                break
except Exception:
    pass

opportunites = []

# --- 2. IA DE TRADING AJUSTÉE (Calibrage ATR + Anti-Chasing) ---
for symbole in actifs_forex:
    try:
        ticker = yf.Ticker(symbole)
        data_m15 = ticker.history(period="3d", interval="15m")
        data_h1 = ticker.history(period="7d", interval="1h")
        data_d1 = ticker.history(period="30d", interval="1d")

        if (
            data_m15.empty
            or data_h1.empty
            or data_d1.empty
            or len(data_m15) < 20
        ):
            continue

        prix_actuel = data_m15["Close"].iloc[-1]
        mm20_m15 = data_m15["Close"].rolling(20).mean().iloc[-1]
        mm20_h1 = data_h1["Close"].rolling(20).mean().iloc[-1]
        mm20_d1 = data_d1["Close"].rolling(20).mean().iloc[-1]

        # Calcul de l'ATR M15 (Volatilité moyenne sur 14 bougies)
        high_low = data_m15["High"] - data_m15["Low"]
        atr_m15 = high_low.rolling(14).mean().iloc[-1]

        if atr_m15 == 0:
            continue

        # FILTRE ANTI-CHASING : Refus si le prix est trop étiré par rapport à la moyenne M15
        distance_mm20_m15 = abs(prix_actuel - mm20_m15)
        if distance_mm20_m15 > (atr_m15 * 1.8):
            continue  # Le mouvement a déjà eu lieu, trop tard pour entrer !

        ecart_h1 = ((prix_actuel - mm20_h1) / mm20_h1) * 100
        sentiment_macro_haussier = prix_actuel > mm20_d1

        plus_haut_recent = data_m15["High"].iloc[-11:-1].max()
        plus_bas_recent = data_m15["Low"].iloc[-11:-1].min()

        cassure_hausse = prix_actuel > plus_haut_recent and ecart_h1 > 0.03
        cassure_baisse = prix_actuel < plus_bas_recent and ecart_h1 < -0.03

        if not cassure_hausse and not cassure_baisse:
            continue

        # Stop Loss ancré sur l'ATR (adapté au M15 réel, entre 8 et 18 pips selon les paires)
        distance_sl = max(atr_m15 * 1.5, 0.0008)

        if cassure_hausse:
            stop_loss = prix_actuel - distance_sl
            type_ordre = "ACHAT (LONG) 🟢"
            tendance = "HAUSSE 🟢"
            alignement_macro = sentiment_macro_haussier
        else:
            stop_loss = prix_actuel + distance_sl
            type_ordre = "VENTE (SHORT) 🔴"
            tendance = "BAISSE 🔴"
            alignement_macro = not sentiment_macro_haussier

        risque = abs(prix_actuel - stop_loss)
        pips_risque = risque * 10000

        # Refus des SL disproportionnés (> 22 pips interdit en M15 Forex)
        if pips_risque > 22 or pips_risque < 6:
            continue

        # Ratio ajusté selon la macro
        ratio_rr = 1.5 if alignement_macro else 1.2
        profil_conviction = (
            "Solide (Macro D1 & H1 alignés ⚡)"
            if alignement_macro
            else "Guérilla / Profit rapide (Contre-tendance D1 ⚠️)"
        )

        if cassure_hausse:
            take_profit = prix_actuel + (risque * ratio_rr)
        else:
            take_profit = prix_actuel - (risque * ratio_rr)

        pips_tp = abs(take_profit - prix_actuel) * 10000

        # ESTIMATION DE DURÉE RÉELLE (Basée sur le TP et la vitesse moyenne M15)
        # Nombre de bougies M15 théoriques pour parcourir la distance du TP
        vitesse_pips_par_bougie = (atr_m15 * 10000) * 0.6
        bougies_estimees = (
            pips_tp / vitesse_pips_par_bougie if vitesse_pips_par_bougie > 0 else 4
        )
        minutes_estimees = int(bougies_estimees * 15)

        if minutes_estimees <= 35:
            duree_str = f"{minutes_estimees} à {minutes_estimees + 15} min (Scalping M15)"
        elif minutes_estimees <= 90:
            duree_str = f"{minutes_estimees} à {minutes_estimees + 30} min (Intraday court)"
        else:
            duree_str = "1h30 à 3h (Intraday étendu)"

        opportunites.append(
            {
                "symbole": symbole.replace("=X", ""),
                "prix": prix_actuel,
                "tendance": tendance,
                "force": abs(ecart_h1),
                "sl": stop_loss,
                "tp": take_profit,
                "type": type_ordre,
                "duree": duree_str,
                "pips_risque": pips_risque,
                "pips_tp": pips_tp,
                "rr": ratio_rr,
                "conviction": profil_conviction,
            }
        )
    except Exception:
        continue

# --- 3. ENVOI DISCORD ---
if alerte_eco:
    message = f"""⛔ **FILTRE ÉCONOMIQUE STRICT**
🚨 `{message_eco}`
*Signal bloqué par sécurité.*"""
elif opportunites:
    meilleur = max(opportunites, key=lambda x: x["force"])
    message = f"""🧠 **SIGNAL M15 RECALIBRÉ (Anti-Chasing)** 🧠

💱 **Actif** : **{meilleur['symbole']}**
📊 **Ordre** : {meilleur['type']}
💶 **Entrée** : {meilleur['prix']:.5f}
🛑 **Stop Loss** : `{meilleur['sl']:.5f}` ({meilleur['pips_risque']:.1f} pips)
🎯 **Take Profit** : `{meilleur['tp']:.5f}` ({meilleur['pips_tp']:.1f} pips — RR 1:{meilleur['rr']})
🔥 **Context Macro** : {meilleur['conviction']}
⏱️ **Durée estimée réaliste** : {meilleur['duree']}
⏳ **Timing** : Clôture M15 dans **{minutes_restantes} min**.
"""
else:
    message = None

if message:
    donnees = {"content": message}
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    requete = urllib.request.Request(
        webhook_url, data=json.dumps(donnees).encode("utf-8"), headers=headers
    )
    try:
        urllib.request.urlopen(requete)
        print("Signal envoyé avec succès !")
    except Exception as e:
        print("Erreur Discord :", e)
else:
    print("Aucune opportunité réaliste détectée.")
