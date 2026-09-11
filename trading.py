from datetime import datetime
import json
import os
import urllib.request
import yfinance as yf

webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

maintenant = datetime.now()
heure_actuelle = maintenant.heure
minute_actuelle = maintenant.minute
minutes_restantes = 15 - (minute_actuelle % 15)

# --- 0. FILTRE DES HORAIRES DE TRADING (08:00 - 18:00 UNIQUEMENT) ---
# Si on est la nuit ou le weekend, le bot s'arrête net sans spammer
if heure_actuelle < 8 or heure_actuelle >= 18:
    print(
        "Hors des horaires de trading actifs (8h-18h). Le bot est en veille."
    )
    exit()

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

# --- 2. ANALYSE TECHNIQUE ET SUR MESURE (H1 + M15 + SL/TP) ---
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

        amplitude_pips = abs(risque) * 10000
        if amplitude_pips < 15:
            duree_estimee = "10 à 25 minutes (Mouvement rapide)"
        elif amplitude_pips < 35:
            duree_estimee = "20 à 45 minutes (Volatilité standard)"
        else:
            duree_estimee = "45 à 90 minutes (Large amplitude / Tendance lourde)"

        # Seuil de force un poil plus strict pour éviter qu'il ne s'emballe sur des micro-variations
        if abs(ecart) > 0.05:
            opportunites.append(
                {
                    "symbole": symbole.replace("=X", ""),
                    "prix": prix_actuel,
                    "tendance": tendance,
                    "force": abs(ecart),
                    "sl": stop_loss,
                    "tp": take_profit,
                    "type": type_ordre,
                    "duree": duree_estimee,
                }
            )
    except:
        continue

# --- 3. CONSTRUCTION DU MESSAGE SUR MESURE ---
if alerte_eco:
    message = f"""⛔ **FILTRE ÉCONOMIQUE STRICT : INTERDICTION DE TRADER**
🚨 `{message_eco}`
*Annonce majeure en cours, le bot coupe les signaux pour te protéger.*"""
elif opportunites:
    meilleur = max(opportunites, key=lambda x: x["force"])
    message = f"""🎯 **PRÉ-SIGNAL SUR MESURE (PRO CLOUD)** 🎯

💱 **Actif sélectionné** : **{meilleur['symbole']}**
📊 **Type d'ordre** : {meilleur['type']}
💶 **Prix d'entrée estimé** : {meilleur['prix']:.5f}
🛑 **Stop Loss (Dynamique)** : `{meilleur['sl']:.5f}`
🎯 **Take Profit (Objectif)** : `{meilleur['tp']:.5f}`
⏱️ **Durée estimée sur mesure** : {meilleur['duree']}
⏳ **Timing d'entrée** : Clôture M15 dans **{minutes_restantes} min**. Analyse technique et calendrier validés !
"""
else:
    # Optionnel : si rien de propre, on n'envoie RIEN sur Discord pour éviter le spam inutile.
    # Mais si tu veux un rapport de veille neutre, tu peux laisser ce bloc.
    message = None

if message:
    donnees = {"content": message}
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    requete = urllib.request.Request(
        webhook_url, data=json.dumps(donnees).encode("utf-8"), headers=headers
    )
    try:
        urllib.request.urlopen(requete)
        print("Rapport envoyé sur Discord avec succès !")
    except Exception as e:
        print("Erreur d'envoi Discord :", e)
else:
    print(
        "Marché calme ou hors critères stricts : aucun message polluant envoyé."
    )
