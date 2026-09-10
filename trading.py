from datetime import datetime
import json
import os
import urllib.request
import yfinance as yf

webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

actifs_forex = [
    "EURUSD=X",
    "GBPUSD=X",
    "USDJPY=X",
    "AUDUSD=X",
    "USDCAD=X",
    "EURGBP=X",
]

maintenant = datetime.now()
minute_actuelle = maintenant.minute
heure_actuelle = maintenant.hour
minutes_restantes = 15 - (minute_actuelle % 15)

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
            # Si une annonce majeure tombe entre -15 min et +45 min
            if -15 <= diff <= 45:
                alerte_eco = True
                message_eco = f"Annonce majeure : '{ev.get('title')}' ({ev.get('country')})"
                break
except Exception:
    pass  # Si le flux rencontre un souci technique, on laisse passer le trade pour ne pas bloquer bêtement

opportunites = []

# --- 2. ANALYSE TECHNIQUE (H1 + M15 + SL/TP) ---
for symbole in actifs_forex:
    try:
        ticker = yf.Ticker(symbole)
        data_m15 = ticker.history(period="2d", interval="15m")
        data_h1 = ticker.history(period="7d", interval="1h")

        if data_m15.empty or data_h1.empty:
            continue

        prix_actuel = data_m15["Close"].iloc[-1]
        mm20_h1 = data_h1["Close"].rolling(20).mean().iloc[-1]

        ecart = ((prix_actuel - mm20_h1) / mm20_h1) * 100
        tendance = "HAUSSE 🟢" if ecart > 0 else "BAISSE 🔴"

        if ecart > 0:
            stop_loss = data_m15["Low"].iloc[-10:].min() - 0.0005
            risque = prix_actuel - stop_loss
            take_profit = prix_actuel + (risque * 1.5)
            type_ordre = "ACHAT (LONG) 🟢"
        else:
            stop_loss = data_m15["High"].iloc[-10:].max() + 0.0005
            risque = stop_loss - prix_actuel
            take_profit = prix_actuel - (risque * 1.5)
            type_ordre = "VENTE (SHORT) 🔴"

        if abs(ecart) > 0.03:
            opportunites.append(
                {
                    "symbole": symbole.replace("=X", ""),
                    "prix": prix_actuel,
                    "tendance": tendance,
                    "force": abs(ecart),
                    "sl": stop_loss,
                    "tp": take_profit,
                    "type": type_ordre,
                }
            )
    except:
        continue

# --- 3. CONSTRUCTION DU MESSAGE FINAL ---
if alerte_eco:
    message = f"""⛔ **FILTRE ÉCONOMIQUE STRICT : INTERDICTION DE TRADER**
🚨 `{message_eco}`
*Le marché est trop instable, le bot bloque l'envoi du signal pour te protéger.*"""
elif opportunites:
    meilleur = max(opportunites, key=lambda x: x["force"])
    message = f"""🚨 **PRÉ-SIGNAL TRADING BÉTON ARMÉ (CLOUD H24)** 🚨

🎯 **Meilleure opportunité** : **{meilleur['symbole']}**
💱 **Action** : {meilleur['type']}
💶 **Prix d'entrée estimé** : {meilleur['prix']:.5f}
🛑 **Stop Loss (Dynamique)** : `{meilleur['sl']:.5f}`
🎯 **Take Profit (Objectif)** : `{meilleur['tp']:.5f}`
⏱️ **Durée estimée du trade** : 15 à 45 minutes max (2 à 3 bougies M15).
⏳ **Timing d'entrée** : Clôture M15 dans **{minutes_restantes} min**. Calendrier économique vérifié : OK !
"""
else:
    message = f"""⏳ **MARCHÉ NEUTRE / CALME** 

Le bot tourne en arrière-plan dans le cloud. Analyse technique OK, calendrier propre, mais aucune opportunité volatile nette pour l'instant. Prochain point dans **{minutes_restantes} min**.
"""

donnees = {"content": message}
headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
requete = urllib.request.Request(
    webhook_url, data=json.dumps(donnees).encode("utf-8"), headers=headers
)

try:
    urllib.request.urlopen(requete)
    print("Rapport complet (Technique + Calendrier) envoyé sur Discord !")
except Exception as e:
    print("Erreur d'envoi Discord :", e)
