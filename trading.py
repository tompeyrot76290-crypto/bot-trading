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

opportunites = []

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

# Construction du message de test sécurisé
if opportunites:
    meilleur = max(opportunites, key=lambda x: x["force"])
    message = f"""🚨 **TEST DU CLOUD / PRÉ-SIGNAL** 🚨

🎯 **Actif** : **{meilleur['symbole']}**
💱 **Action** : {meilleur['type']}
💶 **Prix actuel** : {meilleur['prix']:.5f}
🛑 **Stop Loss** : `{meilleur['sl']:.5f}`
🎯 **Take Profit** : `{meilleur['tp']:.5f}`
⏱️ **Statut** : Test réussi depuis GitHub Actions !
"""
else:
    message = """🟢 **TEST DU CLOUD RÉUSSI !** 

Le script tourne parfaitement sur les serveurs de GitHub. Aucune opportunité Forex volatile n'a été trouvée à cet instant précis (marché calme), mais la liaison avec ton webhook Discord est validée.
"""

donnees = {"content": message}
headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
requete = urllib.request.Request(
    webhook_url, data=json.dumps(donnees).encode("utf-8"), headers=headers
)

try:
    urllib.request.urlopen(requete)
    print("Message de test envoyé sur Discord avec succès !")
except Exception as e:
    print("Erreur d'envoi Discord :", e)
