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

# --- 1. FILTRE DU CALENDRIER ÉCONOMIQUE EN DIRECT (LE JUGE SUPRÊME) ---
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
            # Fenêtre de sécurité stricte : -15m / +45m autour de l'annonce majeure
            if -15 <= diff <= 45:
                alerte_eco = True
                message_eco = f"Annonce majeure : '{ev.get('title')}' ({ev.get('country')})"
                break
except Exception:
    pass

opportunites = []

# --- 2. IA DE TRADING DISCRÉTIONNAIRE (Structure intègre + RR Dynamique) ---
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

        # Condition de cassure fraîche M15 validée par la tendance lourde H1
        plus_haut_recent = data_m15["High"].iloc[-11:-1].max()
        plus_bas_recent = data_m15["Low"].iloc[-11:-1].min()

        cassure_hausse = prix_actuel > plus_haut_recent and ecart > 0.04
        cassure_baisse = prix_actuel < plus_bas_recent and ecart < -0.04

        if not cassure_hausse and not cassure_baisse:
            continue

        # Analyse de la structure technique pure (Stop loss sur les vrais pivots)
        if cassure_hausse:
            stop_loss = data_m15["Low"].iloc[-10:].min() - 0.0005
            risque = prix_actuel - stop_loss
            type_ordre = "ACHAT (LONG) 🟢"
            tendance = "HAUSSE 🟢"
        else:
            stop_loss = data_m15["High"].iloc[-10:].max() + 0.0005
            risque = stop_loss - prix_actuel
            type_ordre = "VENTE (SHORT) 🔴"
            tendance = "BAISSE 🔴"

        amplitude_pips = abs(risque) * 10000

        # FILTRE DE VIABILITÉ : Si le marché est trop instable (> 50 pips), le pro rejette le trade.
        if amplitude_pips > 50 or amplitude_pips < 8:
            continue

        # CONVICTION & RR DYNAMIQUE : S'adapte à la force réelle du marché
        force_absolue = abs(ecart)
        if force_absolue > 0.10:
            ratio_rr = 2.2
            profil_conviction = "Élevée (Tendance lourde validée 🚀)"
        elif force_absolue > 0.07:
            ratio_rr = 1.8
            profil_conviction = "Solide (Impulsion propre ⚡)"
        else:
            ratio_rr = 1.5
            profil_conviction = "Standard (Sortie de range 🎯)"

        if cassure_hausse:
            take_profit = prix_actuel + (risque * ratio_rr)
        else:
            take_profit = prix_actuel - (risque * ratio_rr)

        if amplitude_pips < 20:
            duree_estimee = "15 à 30 minutes (Scalping net)"
        elif amplitude_pips < 35:
            duree_estimee = "30 à 60 minutes (Développement de tendance)"
        else:
            duree_estimee = "45 à 90 minutes (Onde d'impulsion large)"

        opportunites.append(
            {
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
                "conviction": profil_conviction,
            }
        )
    except:
        continue

# --- 3. CONSTRUCTION DU MESSAGE DISCORD (MACRO PRIORITAIRE) ---
if alerte_eco:
    message = f"""⛔ **FILTRE ÉCONOMIQUE STRICT : INTERDICTION DE TRADER**
🚨 `{message_eco}`
*Annonce majeure en cours, le bot bloque l'envoi du signal pour te protéger.*"""
elif opportunites:
    meilleur = max(opportunites, key=lambda x: x["force"])
    message = f"""🧠 **SIGNAL DISCRÉTIONNAIRE PRO (IA SUR-MESURE)** 🧠

💱 **Actif sélectionné** : **{meilleur['symbole']}**
📊 **Type d'ordre** : {meilleur['type']}
💶 **Prix d'entrée estimé** : {meilleur['prix']:.5f}
🛑 **Stop Loss (Structurel pur)** : `{meilleur['sl']:.5f}` ({meilleur['pips_risque']:.1f} pips)
🎯 **Take Profit Dynamique** : `{meilleur['tp']:.5f}` (RR calculé : **1:{meilleur['rr']}**)
🔥 **Niveau de conviction** : {meilleur['conviction']}
⏱️ **Durée estimée** : {meilleur['duree']}
⏳ **Timing** : Clôture M15 dans **{minutes_restantes} min**. Annonces et structures validées !
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
        print("Signal Pro sur-mesure envoyé sur Discord !")
    except Exception as e:
        print("Erreur d'envoi Discord :", e)
else:
    print(
        "Marché en attente, structures invalides ou hors critères : silence radio."
    )
