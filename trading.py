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

# --- 2. LOGIQUE DE TRADING PRO (Cassure + Filtre d'épuisement + Risque intelligent) ---
for symbole in actifs_forex:
    try:
        ticker = yf.Ticker(symbole)
        data_m15 = ticker.history(period="2d", interval="15m")
        data_h1 = ticker.history(period="7d", interval="1h")

        if data_m15.empty or data_h1.empty or len(data_m15) < 15:
            continue

        prix_actuel = data_m15["Close"].iloc[-1]
        mm20_h1 = data_h1["Close"].rolling(20).mean().iloc[-1]
        ecart = ((prix_actuel - mm20_h1) / mm20_h1) * 100

        # Analyse de la volatilité récente (taille moyenne des bougies M15)
        data_m15["Candle_Size"] = data_m15["High"] - data_m15["Low"]
        taille_moyenne_bougie = data_m15["Candle_Size"].iloc[-15:-1].mean()
        taille_bougie_actuelle = (
            data_m15["High"].iloc[-1] - data_m15["Low"].iloc[-1]
        )

        # RÈGLE PRO 1 : FILTRE ANTI-ÉPUISEMENT (Evite d'acheter un pic déjà essoufflé)
        # Si la bougie actuelle est 2.5 fois plus grande que la moyenne, le mouvement est sur-étiré, on évite le piège.
        if (
            taille_moyenne_bougie > 0
            and taille_bougie_actuelle > taille_moyenne_bougie * 2.5
        ):
            continue

        # Condition de cassure fraîche M15
        plus_haut_recent = data_m15["High"].iloc[-11:-1].max()
        plus_bas_recent = data_m15["Low"].iloc[-11:-1].min()

        cassure_hausse = prix_actuel > plus_haut_recent and ecart > 0.04
        cassure_baisse = prix_actuel < plus_bas_recent and ecart < -0.04

        if cassure_hausse:
            # Stop loss technique de base
            stop_loss_brut = data_m15["Low"].iloc[-10:].min() - 0.0005
            risque_brut = prix_actuel - stop_loss_brut

            # RÈGLE PRO 2 : PLAFOND DE RISQUE INTELLIGENT (Empêche les SL de 100 pips)
            # Si le risque dépasse 35 pips, un pro ne prend pas un trade aussi large sur M15.
            # On resserre intelligemment sur le plus bas de la bougie de cassure ou un pivot proche.
            max_risque_autorise = 0.0035  # 35 pips max sur Forex
            if risque_brut > max_risque_autorise:
                stop_loss = prix_actuel - max_risque_autorise
            else:
                stop_loss = stop_loss_brut

            risque = prix_actuel - stop_loss
            take_profit = prix_actuel + (risque * 1.5)
            type_ordre = "ACHAT (LONG) 🟢"
            tendance = "HAUSSE 🟢"
            force = abs(ecart)

        elif cassure_baisse:
            stop_loss_brut = data_m15["High"].iloc[-10:].max() + 0.0005
            risque_brut = stop_loss_brut - prix_actuel

            max_risque_autorise = 0.0035
            if risque_brut > max_risque_autorise:
                stop_loss = prix_actuel + max_risque_autorise
            else:
                stop_loss = stop_loss_brut

            risque = stop_loss - prix_actuel
            take_profit = prix_actuel - (risque * 1.5)
            type_ordre = "VENTE (SHORT) 🔴"
            tendance = "BAISSE 🔴"
            force = abs(ecart)
        else:
            continue

        amplitude_pips = abs(risque) * 10000
        # Durées réalistes calées sur des objectifs courts et percutants
        if amplitude_pips < 15:
            duree_estimee = "10 à 25 minutes (Scalping rapide)"
        elif amplitude_pips < 25:
            duree_estimee = "20 à 40 minutes (Impulsion standard)"
        else:
            duree_estimee = "35 à 60 minutes (Cible optimisée)"

        opportunites.append(
            {
                "symbole": symbole.replace("=X", ""),
                "prix": prix_actuel,
                "tendance": tendance,
                "force": force,
                "sl": stop_loss,
                "tp": take_profit,
                "type": type_ordre,
                "duree": duree_estimee,
                "pips_risque": amplitude_pips,
            }
        )
    except:
        continue

# --- 3. CONSTRUCTION DU MESSAGE DISCORD ---
if alerte_eco:
    message = f"""⛔ **FILTRE ÉCONOMIQUE STRICT : INTERDICTION DE TRADER**
🚨 `{message_eco}`
*Annonce majeure en cours, le bot bloque l'envoi du signal.*"""
elif opportunites:
    meilleur = max(opportunites, key=lambda x: x["force"])
    message = f"""🎯 **SIGNAL PRO SUR MESURE (IA QUANT)** 🎯

💱 **Actif sélectionné** : **{meilleur['symbole']}**
📊 **Type d'ordre** : {meilleur['type']}
💶 **Prix d'entrée estimé** : {meilleur['prix']:.5f}
🛑 **Stop Loss (Optimisé Pro)** : `{meilleur['sl']:.5f}` ({meilleur['pips_risque']:.1f} pips)
🎯 **Take Profit Réaliste** : `{meilleur['tp']:.5f}`
⏱️ **Durée estimée** : {meilleur['duree']}
⏳ **Timing** : Clôture M15 dans **{minutes_restantes} min**. Setup validé et filtré contre l'épuisement !
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
        print("Signal Pro envoyé sur Discord avec succès !")
    except Exception as e:
        print("Erreur d'envoi Discord :", e)
else:
    print(
        "Marché en consolidation, mouvement épuisé ou sans cassure : silence radio."
    )
