import os, requests, anthropic, schedule, time, json, threading
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()

APIFOOTBALL = os.getenv("APIFOOTBALL_KEY")
ODDS_KEY    = os.getenv("ODDS_API_KEY")
TG_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TG_CHAT     = os.getenv("TELEGRAM_CHAT_ID")
client      = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

last_update_id = 0
stop_analysis  = False
stop_live      = False
ADMIN_ID       = 8317266009

HISTORY_FILE = "predictions_history.json"

def load_history():
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except:
        return []

def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

ALLOWED_LEAGUES = [
    ("Italy", "Serie A"), ("Italy", "Serie B"), ("Italy", "Serie C"), ("Italy", "Coppa Italia"),
    ("England", "Premier League"), ("England", "Championship"), ("England", "League One"),
    ("England", "League Two"), ("England", "FA Cup"), ("England", "EFL Cup"),
    ("Spain", "La Liga"), ("Spain", "Segunda Division"), ("Spain", "Copa del Rey"),
    ("Germany", "Bundesliga"), ("Germany", "2. Bundesliga"), ("Germany", "DFB Pokal"),
    ("France", "Ligue 1"), ("France", "Ligue 2"), ("France", "Coupe de France"),
    ("Portugal", "Primeira Liga"), ("Portugal", "Liga Portugal 2"),
    ("Netherlands", "Eredivisie"), ("Netherlands", "Eerste Divisie"),
    ("Belgium", "First Division A"), ("Belgium", "First Division B"),
    ("Turkey", "Super Lig"), ("Turkey", "1. Lig"),
    ("Russia", "Premier League"),
    ("Greece", "Super League"),
    ("Scotland", "Premiership"), ("Scotland", "Championship"),
    ("Austria", "Bundesliga"),
    ("Switzerland", "Super League"),
    ("Denmark", "Superliga"),
    ("Norway", "Eliteserien"),
    ("Sweden", "Allsvenskan"),
    ("Poland", "Ekstraklasa"),
    ("Czech-Republic", "Czech Liga"),
    ("Croatia", "HNL"),
    ("Serbia", "Super Liga"),
    ("Romania", "Liga I"),
    ("Ukraine", "Premier League"),
    ("Israel", "Premier League"),
    ("Saudi-Arabia", "Pro League"),
    ("United-Arab-Emirates", "Pro League"),
    ("Brazil", "Serie A"), ("Brazil", "Serie B"),
    ("Argentina", "Liga Profesional"),
    ("Uruguay", "Primera Division"),
    ("Chile", "Primera Division"),
    ("Colombia", "Primera A"),
    ("Mexico", "Liga MX"),
    ("USA", "MLS"),
    ("Japan", "J1 League"),
    ("China", "Super League"),
    ("Australia", "A-League"),
    ("South-Korea", "K League 1"),
    ("World", "UEFA Champions League"),
    ("World", "UEFA Europa League"),
    ("World", "UEFA Europa Conference League"),
    ("World", "FIFA World Cup"),
    ("World", "UEFA European Championship"),
    ("World", "UEFA Nations League"),
    ("World", "Copa America"),
    ("World", "African Nations Cup"),
    ("World", "Friendlies"),
]

def is_allowed(m):
    return any(
        country.lower() in m['league']['country'].lower() and
        league.lower() in m['league']['name'].lower()
        for country, league in ALLOWED_LEAGUES
    )

def get_matches():
    italy_time = datetime.now(timezone.utc) + timedelta(hours=2)
    today = italy_time.strftime("%Y-%m-%d")
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    r = requests.get(url, headers=headers, params={"date": today})
    print(f"Cerco partite per {today}: {r.json().get('results', 0)} trovate")
    now_utc = datetime.now(timezone.utc)
    filtered = []
    for m in r.json().get("response", []):
        if not is_allowed(m):
            continue
        status = m["fixture"]["status"]["short"]
        if status in ["NS", "TBD"]:  # Solo partite non ancora iniziate
            filtered.append(m)
    print(f"Partite non ancora iniziate: {len(filtered)}")
    return filtered

def get_matches_tomorrow():
    italy_time = datetime.now(timezone.utc) + timedelta(hours=2)
    tomorrow = (italy_time + timedelta(days=1)).strftime("%Y-%m-%d")
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    r = requests.get(url, headers=headers, params={"date": tomorrow})
    print(f"Cerco partite per {tomorrow}: {r.json().get('results', 0)} trovate")
    return [m for m in r.json().get("response", []) if is_allowed(m)]

def get_live_matches():
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    r = requests.get(url, headers=headers, params={"live": "all"})
    return [
        m for m in r.json().get("response", [])
        if is_allowed(m) and (m['fixture']['status'].get('elapsed') or 0) >= 30
    ]

def get_team_stats(team_id, league_id, season):
    url = "https://v3.football.api-sports.io/teams/statistics"
    headers = {"x-apisports-key": APIFOOTBALL}
    r = requests.get(url, headers=headers, params={"team": team_id, "league": league_id, "season": season})
    return r.json().get("response", {})

def get_injuries(team_id, fixture_id):
    url = "https://v3.football.api-sports.io/injuries"
    headers = {"x-apisports-key": APIFOOTBALL}
    r = requests.get(url, headers=headers, params={"team": team_id, "fixture": fixture_id})
    return r.json().get("response", [])

def get_odds(home, away):
    url = "https://api.the-odds-api.com/v4/sports/soccer/odds/"
    params = {"apiKey": ODDS_KEY, "regions": "eu", "markets": "h2h,totals", "oddsFormat": "decimal"}
    try:
        r = requests.get(url, params=params)
        data = r.json()
        if not isinstance(data, list):
            return []
        for event in data:
            if not isinstance(event, dict):
                continue
            if home.lower() in event.get("home_team","").lower() or \
               away.lower() in event.get("away_team","").lower():
                return event.get("bookmakers", [])
    except:
        pass
    return []

def analyze_with_claude(match_data, stats_home, stats_away, injuries_home, injuries_away, odds):
    prompt = f"""
Sei un analista di scommesse sportive esperto. Analizza questa partita
e fornisci: 1) probabilita reali 1X2, 2) value bet migliore,
3) risultato esatto piu probabile, 4) confidence score 0-100.

PARTITA: {match_data['teams']['home']['name']} vs {match_data['teams']['away']['name']}
DATA: {match_data['fixture']['date']}
STATISTICHE CASA: {stats_home}
STATISTICHE OSPITE: {stats_away}
INFORTUNI CASA: {[i['player']['name'] for i in injuries_home]}
INFORTUNI OSPITI: {[i['player']['name'] for i in injuries_away]}
QUOTE MERCATO: {odds[:3] if odds else 'non disponibili'}

Le quote reali dei bookmaker sono disponibili nel campo QUOTE MERCATO.
Usale come riferimento principale — NON inventare quote diverse.
Se le quote non sono disponibili, indica N/D.

Rispondi SOLO in JSON senza backtick con questi campi:
prob_home, prob_draw, prob_away,
value_bet (scegli tra 1, X, 2 quella con piu valore),
quota_consigliata (prendi la quota REALE dal mercato per quella scelta),
over_under (Over 2.5 o Under 2.5 in base alle statistiche),
quota_over_under (quota REALE dal mercato se disponibile, altrimenti N/D),
gol_no_gol (Gol o No Gol in base alle statistiche),
quota_gol_no_gol (quota REALE dal mercato se disponibile, altrimenti N/D),
risultato_esatto, confidence, motivazione (max 3 righe).
"""
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text

def analyze_live_with_claude(match_data, odds):
    home = match_data['teams']['home']['name']
    away = match_data['teams']['away']['name']
    score = match_data['goals']
    minute = match_data['fixture']['status'].get('elapsed', '?')
    prompt = f"""
Sei un analista di scommesse sportive esperto in betting LIVE.
Analizza questa partita in corso e suggerisci la migliore giocata live.

PARTITA: {home} vs {away}
MINUTO: {minute}
PUNTEGGIO: {score['home']} - {score['away']}
QUOTE LIVE: {odds[:3] if odds else 'non disponibili'}

Le quote live reali sono disponibili nel campo QUOTE LIVE.
Usale come riferimento principale — NON inventare quote diverse.
Se le quote non sono disponibili, indica N/D.

Rispondi SOLO in JSON senza backtick con questi campi:
giocata_consigliata (scegli la migliore tra 1X2 live),
quota_live (quota REALE dal mercato live),
over_under_live (Over o Under adatto al minuto di gioco),
quota_over_under_live (quota REALE dal mercato live se disponibile, altrimenti N/D),
gol_no_gol_live (Gol o No Gol), 
quota_gol_no_gol_live (quota REALE dal mercato live se disponibile, altrimenti N/D),
motivazione_live (max 2 righe),
confidence_live (0-100), rischio (basso/medio/alto).
"""
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"})
    print(f"Telegram: {r.status_code}")

def format_message(match, analysis):
    try:
        a = json.loads(analysis.strip("` \njson"))
    except:
        a = {}
    home = match['teams']['home']['name']
    away = match['teams']['away']['name']
    league = match['league']['name']
    country = match['league']['country']
    kick_utc = datetime.fromisoformat(match['fixture']['date'].replace('Z', '+00:00'))
    kick_it = kick_utc.astimezone(timezone(timedelta(hours=2)))
    kickoff = kick_it.strftime("%d/%m/%Y %H:%M")
    confidence = int(a.get('confidence', 0))
    star = "\u2b50 <b>TOP VALUE BET</b>\n" if confidence >= 70 else ""
    return (
        f"\n{star}\u26bd <b>{home} vs {away}</b>\n"
        f"\U0001f3c6 {country} \u2014 {league}\n"
        f"\U0001f550 {kickoff} (ora italiana)\n\n"
        f"\U0001f4ca Probabilita stimate:\n"
        f"  1\ufe0f\u20e3 {home[:15]}: {a.get('prob_home','?')}%\n"
        f"  \u27a1\ufe0f Pareggio: {a.get('prob_draw','?')}%\n"
        f"  2\ufe0f\u20e3 {away[:15]}: {a.get('prob_away','?')}%\n\n"
        f"\U0001f4a1 <b>Value Bet: {a.get('value_bet','N/A')}</b>\n"
        f"   Quota consigliata: {a.get('quota_consigliata','?')}\n"
        f"\U0001f3af Risultato esatto: {a.get('risultato_esatto','?')}\n"
        f"\U0001f525 Confidence: {confidence}/100\n\n"
        f"\U0001f4dd {a.get('motivazione','')}\n"
    )

def format_live_message(match, analysis):
    try:
        a = json.loads(analysis.strip("` \njson"))
    except:
        a = {}
    home = match['teams']['home']['name']
    away = match['teams']['away']['name']
    league = match['league']['name']
    country = match['league']['country']
    score = match['goals']
    minute = match['fixture']['status'].get('elapsed', '?')
    return (
        f"\U0001f534 <b>LIVE \u2014 {home} vs {away}</b>\n"
        f"\U0001f3c6 {country} \u2014 {league}\n"
        f"\u23f1 Minuto: {minute}' | Punteggio: {score['home']}-{score['away']}\n\n"
        f"\U0001f3b0 <b>Giocata: {a.get('giocata_consigliata','N/A')}</b>\n"
        f"   Quota live: {a.get('quota_live','?')}\n"
        f"\u26a0\ufe0f Rischio: {a.get('rischio','?')}\n"
        f"\U0001f525 Confidence: {a.get('confidence_live','?')}/100\n\n"
        f"\U0001f4dd {a.get('motivazione_live','')}\n"
    )

def group_by_league(matches):
    leagues = {}
    for m in matches:
        key = f"{m['league']['country']} \u2014 {m['league']['name']}"
        if key not in leagues:
            leagues[key] = []
        leagues[key].append(m)
    return leagues

def show_stats():
    history = load_history()
    if not history:
        send_telegram("\U0001f4ca Nessuna previsione registrata ancora.")
        return
    total = len(history)
    correct = sum(1 for h in history if h.get('result') == 'win')
    pending = sum(1 for h in history if h.get('result') == 'pending')
    pct = round((correct / (total - pending)) * 100, 1) if (total - pending) > 0 else 0
    send_telegram(
        f"\U0001f4ca <b>Statistiche previsioni</b>\n\n"
        f"\U0001f4cb Totali: {total}\n"
        f"\u2705 Vinte: {correct}\n"
        f"\u274c Perse: {total - correct - pending}\n"
        f"\u23f3 In attesa: {pending}\n"
        f"\U0001f3af Precisione: {pct}%\n"
    )

def top_job():
    history = load_history()
    today = datetime.now().strftime("%Y-%m-%d")
    top = [h for h in history if h.get("date") == today and h.get("confidence", 0) >= 70]
    if not top:
        send_telegram("\u26a0\ufe0f Nessuna value bet con confidence >= 70 trovata oggi.")
        return
    top.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    msg = "\u2b50 <b>TOP VALUE BETS DI OGGI</b>\n\n"
    for h in top[:10]:
        msg += f"\u26bd <b>{h['match']}</b>\n"
        msg += f"\U0001f4a1 {h['value_bet']} | \U0001f3af {h['risultato_esatto']} | \U0001f525 {h['confidence']}/100\n\n"
    send_telegram(msg)

def riepilogo_serale():
    history = load_history()
    today = datetime.now().strftime("%Y-%m-%d")
    today_bets = [h for h in history if h.get('date') == today]
    if not today_bets:
        send_telegram("\u26a0\ufe0f Nessuna previsione registrata oggi.")
        return
    top = sorted([h for h in today_bets if h.get('confidence', 0) >= 70],
                 key=lambda x: x.get('confidence', 0), reverse=True)
    total = len(today_bets)
    msg = f"\U0001f319 <b>Riepilogo serale \u2014 {datetime.now().strftime('%d/%m/%Y')}</b>\n\n"
    msg += f"\U0001f4cb Partite analizzate oggi: <b>{total}</b>\n"
    msg += f"\u2b50 Value bet con confidence >= 70: <b>{len(top)}</b>\n\n"
    if top:
        msg += "\U0001f3c6 <b>Le migliori di oggi:</b>\n"
        for h in top[:5]:
            msg += f"\n\u26bd <b>{h['match']}</b>\n"
            msg += f"\U0001f4a1 {h['value_bet']} | \U0001f3af {h['risultato_esatto']} | \U0001f525 {h['confidence']}/100\n"
    send_telegram(msg)

def analyze_single_live(m):
    odds = get_odds(m['teams']['home']['name'], m['teams']['away']['name'])
    analysis = analyze_live_with_claude(m, odds)
    return format_live_message(m, analysis)

def run_analysis(matches, label="oggi"):
    global stop_analysis
    stop_analysis = False
    history = load_history()
    top_bets = []
    leagues = group_by_league(matches)
    send_telegram(f"\U0001f4c5 Trovate <b>{len(matches)}</b> partite in <b>{len(leagues)}</b> campionati. Analisi in corso...")

    for league_name, league_matches in leagues.items():
        if stop_analysis:
            send_telegram("\U0001f6d1 Analisi fermata.")
            return
        send_telegram(f"\U0001f3c6 <b>{league_name}</b> \u2014 {len(league_matches)} partite")
        time.sleep(1)

        for m in league_matches:
            if stop_analysis:
                send_telegram("\U0001f6d1 Analisi fermata.")
                return

            home_name = m['teams']['home']['name']
            away_name = m['teams']['away']['name']
            print(f"Analisi: {home_name} vs {away_name}...")

            sh   = get_team_stats(m['teams']['home']['id'], m['league']['id'], m['league']['season'])
            sa   = get_team_stats(m['teams']['away']['id'], m['league']['id'], m['league']['season'])
            ih   = get_injuries(m['teams']['home']['id'], m['fixture']['id'])
            ia   = get_injuries(m['teams']['away']['id'], m['fixture']['id'])
            odds = get_odds(home_name, away_name)
            analysis = analyze_with_claude(m, sh, sa, ih, ia, odds)
            msg = format_message(m, analysis)

            try:
                a = json.loads(analysis.strip("` \njson"))
                confidence = int(a.get('confidence', 0))
                history.append({
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "match": f"{home_name} vs {away_name}",
                    "value_bet": a.get('value_bet',''),
                    "risultato_esatto": a.get('risultato_esatto',''),
                    "confidence": confidence,
                    "result": "pending"
                })
                if confidence >= 70:
                    top_bets.append((confidence, msg))
            except:
                pass

            send_telegram(msg)
            print(f"Inviato: {home_name} vs {away_name}")
            time.sleep(5)

    save_history(history)

    if top_bets:
        top_bets.sort(key=lambda x: x[0], reverse=True)
        send_telegram(f"\u2b50 <b>TOP VALUE BETS {label.upper()} (confidence >= 70)</b>")
        for _, msg in top_bets[:5]:
            send_telegram(msg)
            time.sleep(3)

    send_telegram(f"\u2705 <b>Analisi {label} completata!</b>")

def daily_job():
    send_telegram("\U0001f50d Avvio analisi partite di oggi...")
    matches = get_matches()
    if not matches:
        send_telegram("\u26a0\ufe0f Nessuna partita trovata oggi nei campionati scommettibili.")
        return
    run_analysis(matches, "oggi")

def domani_job():
    italy_time = datetime.now(timezone.utc) + timedelta(hours=2)
    tomorrow = (italy_time + timedelta(days=1)).strftime("%d/%m/%Y")
    send_telegram(f"\U0001f50d Analisi partite di domani <b>{tomorrow}</b>...")
    matches = get_matches_tomorrow()
    if not matches:
        send_telegram("\u26a0\ufe0f Nessuna partita trovata domani nei campionati scommettibili.")
        return
    run_analysis(matches, "domani")

def live_job():
    global stop_live
    stop_live = False
    print(f"[{datetime.now()}] Analisi live...")
    matches = get_live_matches()
    if not matches:
        send_telegram("\u26bd Nessuna partita live in corso con almeno 30 minuti giocati.")
        return

    send_telegram(f"\U0001f534 <b>{len(matches)} partite live \u2014 analisi in corso...</b>")
    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(analyze_single_live, m): m for m in matches}
        for future in as_completed(futures):
            if stop_live:
                send_telegram("\U0001f6d1 Analisi live fermata!")
                return
            try:
                results.append(future.result())
            except Exception as e:
                print(f"Errore live: {e}")

    for msg in results:
        if stop_live:
            send_telegram("\U0001f6d1 Analisi live fermata!")
            return
        send_telegram(msg)
        time.sleep(2)
    send_telegram("\u2705 <b>Analisi live completata!</b>")

def listen_commands():
    global last_update_id, stop_analysis, stop_live
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
    while True:
        try:
            r = requests.get(url, params={"timeout": 30, "offset": last_update_id + 1}, timeout=35)
            updates = r.json().get("result", [])
            for update in updates:
                last_update_id = update["update_id"]
                msg = update.get("message", {}) or update.get("channel_post", {})
                text = msg.get("text", "").strip().lower()
                user_id = msg.get("from", {}).get("id", 0)
                if user_id != ADMIN_ID:
                    continue
                if text in ["/analisi", "/start"]:
                    threading.Thread(target=daily_job).start()
                elif text == "/domani":
                    threading.Thread(target=domani_job).start()
                elif text == "/live":
                    threading.Thread(target=live_job).start()
                elif text == "/stop":
                    stop_analysis = True
                    stop_live = True
                    send_telegram("\U0001f6d1 Analisi fermata! Scrivi /analisi o /live per riavviare.")
                elif text == "/stoplive":
                    stop_live = True
                    send_telegram("\U0001f6d1 Analisi live fermata! Scrivi /live per riavviare.")
                elif text == "/top":
                    top_job()
                elif text == "/riepilogo":
                    riepilogo_serale()
                elif text == "/stats":
                    show_stats()
                elif text == "/help":
                    send_telegram(
                        "\U0001f916 <b>Comandi disponibili:</b>\n\n"
                        "/analisi \u2014 Partite di oggi\n"
                        "/domani \u2014 Partite di domani\n"
                        "/live \u2014 Giocate live (partite al 30'+)\n"
                        "/top \u2014 Top value bet oggi (confidence >= 70)\n"
                        "/riepilogo \u2014 Riepilogo serale\n"
                        "/stop \u2014 Ferma analisi pre-partita e live\n"
                        "/stoplive \u2014 Ferma solo analisi live\n"
                        "/stats \u2014 Statistiche previsioni\n"
                        "/help \u2014 Questo messaggio\n"
                    )
        except Exception as e:
            print(f"Errore listener: {e}")
        time.sleep(2)

if __name__ == "__main__":
    print("Bot avviato!")
    send_telegram(
        "\U0001f916 <b>Bot avviato!</b>\n\n"
        "/analisi \u2014 Partite di oggi\n"
        "/domani \u2014 Partite di domani\n"
        "/live \u2014 Giocate live\n"
        "/top \u2014 Top value bet oggi\n"
        "/riepilogo \u2014 Riepilogo serale\n"
        "/stop \u2014 Ferma analisi\n"
        "/stats \u2014 Statistiche\n"
        "/help \u2014 Aiuto"
    )
    t = threading.Thread(target=listen_commands, daemon=True)
    t.start()
    while True:
        schedule.run_pending()
        time.sleep(60)
