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
    print("Hors des horaires de trading actifs (8h-18h). Le bot est en veille.")
    exit(0)

actifs_forex = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "NZDUSD=X",
    "USDCHF=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X", "EURAUD=X", "GBPAUD=X",
    "AUDJPY=X", "EURCAD=X", "AUDCAD=X", "NZDJPY=X", "CHFJPY=X"
]

# --- 1. FILTRE DU CALENDRIER ÉCONOMIQUE EN DIRECT (LE JUGE SUPRÊME) ---
alerte_eco = False
message_eco = ""
try:
    url_calendrier = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    req = urllib.request.Request(url_calendrier, headers={"User-Agent": "Mozilla/5.0"})
    reponse = urllib.request.urlopen(req)
    evenements = json.loads(reponse.read().decode("utf-8"))
    date_jour = maintenant.strftime("%Y-%m-%d")
    for ev in evenements:
        if ev.get("impact") == "High" and date_jour in ev.get("date", ""):
            heure_ev = datetime.strptime(ev.get("date")[:19], "%Y-%m-%dT%H:%M:%S")
            diff = (heure_ev - maintenant).total_seconds() / 60
            if -15 <= diff <= 45:
                alerte_eco = True
                message_eco = f"Annonce majeure : '{ev.get('title')}' ({ev.get('country')})"
                break
except Exception:
    pass

opportunites = []

# --- 2. IA DE TRADING : VISION TOP-DOWN (M15 + H1 + Macro D1) ---
for symbole in actifs_forex:
    try:
        ticker = yf.Ticker(symbole)
        # Extraction des 3 unités de temps comme un vrai pro
        data_m15 = ticker.history(period="2d", interval="15m")
        data_h1 = ticker.history(period="7d", interval="1h")
        data_d1 = ticker.history(period="30d", interval="1d") # Le Radar Macro (Sentiment)

        if data_m15.empty or data_h1.empty or data_d1.empty or len(data_m15) < 15:
            continue

        prix_actuel = data_m15["Close"].iloc[-1]
        mm20_h1 = data_h1["Close"].rolling(20).mean().iloc[-1]
        mm20_d1 = data_d1["Close"].rolling(20).mean().iloc[-1] # Tendance de fond mensuelle
        
        ecart = ((prix_actuel - mm20_h1) / mm20_h1) * 100
        
        # Sentiment Macro D1
        sentiment_macro_haussier = prix_actuel > mm20_d1

        # Condition de cassure fraîche M15
        plus_haut_recent = data_m15["High"].iloc[-11:-1].max()
        plus_bas_recent = data_m15["Low"].iloc[-11:-1].min()

        cassure_hausse = prix_actuel > plus_haut_recent and ecart > 0.04
        cassure_baisse = prix_actuel < plus_bas_recent and ecart < -0.04

        if not cassure_hausse and not cassure_baisse:
            continue

        # Analyse Structurelle
        if cassure_hausse:
            stop_loss = data_m15["Low"].iloc[-10:].min() - 0.0005
            risque = prix_actuel - stop_loss
            type_ordre = "ACHAT (LONG) 🟢"
            tendance = "HAUSSE 🟢"
            alignement_macro = sentiment_macro_haussier
        else:
            stop_loss = data_m15["High"].iloc[-10:].max() + 0.0005
            risque = stop_loss - prix_actuel
            type_ordre = "VENTE (SHORT) 🔴"
            tendance = "BAISSE 🔴"
            alignement_macro = not sentiment_macro_haussier

        amplitude_pips = abs(risque) * 10000

        # FILTRE DE VIABILITÉ : Trop de risque = on rejette
        if amplitude_pips > 50 or amplitude_pips < 8:
            continue

        # GESTION HUMAINE DU RISQUE (Conscience du sentiment Macro D1)
        force_absolue = abs(ecart)
        
        if alignement_macro:
            # Le vent macro nous pousse dans le dos, on peut viser loin !
            if force_absolue > 0.10:
                ratio_rr = 2.2
                profil_conviction = "Maximale (Alignement Macro D1 + H1 🚀)"
            elif force_absolue > 0.07:
                ratio_rr = 1.8
                profil_conviction = "Solide (Tendance H1 + Macro saine ⚡)"
            else:
                ratio_rr = 1.5
                profil_conviction = "Standard (Sortie de range avec appui Macro 🎯)"
        else:
            # On trade contre le sentiment D1 (Contre-tendance).
            # Le bot n'est pas parano, il prend le trade, mais réduit sa cible !
            ratio_rr = 1.2
            profil_conviction = "Prudente / Guérilla (À contre-courant du Daily D1 ⚠️)"

        # Calcul du TP adapté
        if cassure_hausse:
            take_profit = prix_actuel + (risque * ratio_rr)
        else:
            take_profit = prix_actuel - (risque * ratio_rr)

        if amplitude_pips < 20:
            duree_estimee = "15 à 30 minutes (Scalping net)"
        elif amplitude_pips < 35:
            duree_estimee = "30 à 60 minutes (Développement)"
        else:
            duree_estimee = "45 à 90 minutes (Onde large)"

        opportunites.append({
            "symbole": symbole.replace("=X", ""),
            "prix": prix_actuel,
            "tendance": tendance,
            "force": force_absolue,
            "sl": stop_loss,
            "tp": take_profit,
            "type": type_ordre,
            "duree": duree_estimee,
            "pips_risque": amplitude_pips,
            "rr": ratio_rr,
            "conviction": profil_conviction
        })
    except:
        continue

# --- 3. CONSTRUCTION DU MESSAGE DISCORD ---
if alerte_eco:
    message = f"""⛔ **FILTRE ÉCONOMIQUE STRICT : INTERDICTION DE TRADER**
🚨 `{message_eco}`
*Annonce majeure en cours, le bot bloque l'envoi du signal pour te protéger.*"""
elif opportunites:
    meilleur = max(opportunites, key=lambda x: x["force"])
    message = f"""🧠 **SIGNAL INSTITUTIONNEL (Analyse Multi-Timeframe)** 🧠

💱 **Actif sélectionné** : **{meilleur['symbole']}**
📊 **Type d'ordre** : {meilleur['type']}
💶 **Prix d'entrée estimé** : {meilleur['prix']:.5f}
🛑 **Stop Loss (Structurel pur)** : `{meilleur['sl']:.5f}` ({meilleur['pips_risque']:.1f} pips)
🎯 **Take Profit Intelligent** : `{meilleur['tp']:.5f}` (RR calculé : **1:{meilleur['rr']}**)
🔥 **Conscience Macro** : {meilleur['conviction']}
⏱️ **Durée estimée** : {meilleur['duree']}
⏳ **Timing** : Clôture M15 dans **{minutes_restantes} min**.
"""
else:
    message = None

if message:
    donnees = {"content": message}
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    requete = urllib.request.Request(webhook_url, data=json.dumps(donnees).encode("utf-8"), headers=headers)
    try:
        urllib.request.urlopen(requete)
        print("Signal Pro sur-mesure envoyé sur Discord !")
    except Exception as e:
        print("Erreur d'envoi Discord :", e)
else:
    print("Marché en attente, structures invalides ou hors critères : silence radio.")
