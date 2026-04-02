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

ADMIN_ID = 8317266009  # Solo tu puoi dare comandi

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

# ── Campionati scommettibili Sisal ────────────────────────────
ALLOWED_LEAGUES = [
    # Italia
    ("Italy", "Serie A"), ("Italy", "Serie B"), ("Italy", "Serie C"),
    ("Italy", "Coppa Italia"),
    # Inghilterra
    ("England", "Premier League"), ("England", "Championship"),
    ("England", "League One"), ("England", "League Two"),
    ("England", "FA Cup"), ("England", "EFL Cup"),
    # Spagna
    ("Spain", "La Liga"), ("Spain", "Segunda Division"),
    ("Spain", "Copa del Rey"),
    # Germania
    ("Germany", "Bundesliga"), ("Germany", "2. Bundesliga"),
    ("Germany", "DFB Pokal"),
    # Francia
    ("France", "Ligue 1"), ("France", "Ligue 2"),
    ("France", "Coupe de France"),
    # Portogallo
    ("Portugal", "Primeira Liga"), ("Portugal", "Liga Portugal 2"),
    # Olanda
    ("Netherlands", "Eredivisie"), ("Netherlands", "Eerste Divisie"),
    # Belgio
    ("Belgium", "First Division A"), ("Belgium", "First Division B"),
    # Turchia
    ("Turkey", "Süper Lig"), ("Turkey", "1. Lig"),
    # Russia
    ("Russia", "Premier League"),
    # Grecia
    ("Greece", "Super League"),
    # Scozia
    ("Scotland", "Premiership"), ("Scotland", "Championship"),
    # Austria
    ("Austria", "Bundesliga"),
    # Svizzera
    ("Switzerland", "Super League"),
    # Danimarca
    ("Denmark", "Superliga"),
    # Norvegia
    ("Norway", "Eliteserien"),
    # Svezia
    ("Sweden", "Allsvenskan"),
    # Polonia
    ("Poland", "Ekstraklasa"),
    # Repubblica Ceca
    ("Czech-Republic", "Czech Liga"),
    # Croazia
    ("Croatia", "HNL"),
    # Serbia
    ("Serbia", "Super Liga"),
    # Romania
    ("Romania", "Liga I"),
    # Ucraina
    ("Ukraine", "Premier League"),
    # Israele
    ("Israel", "Premier League"),
    # Arabia Saudita
    ("Saudi-Arabia", "Pro League"),
    # Emirati Arabi
    ("United-Arab-Emirates", "Pro League"),
    # Brasile
    ("Brazil", "Serie A"), ("Brazil", "Serie B"),
    # Argentina
    ("Argentina", "Liga Profesional"),
    # Uruguay
    ("Uruguay", "Primera Division"),
    # Cile
    ("Chile", "Primera Division"),
    # Colombia
    ("Colombia", "Primera A"),
    # Messico
    ("Mexico", "Liga MX"),
    # USA
    ("USA", "MLS"),
    # Giappone
    ("Japan", "J1 League"),
    # Cina
    ("China", "Super League"),
    # Australia
    ("Australia", "A-League"),
    # Corea del Sud
    ("South-Korea", "K League 1"),
    # Europa e Mondo
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

# ── 1. Partite di oggi (ora italiana) ─────────────────────────
def get_matches():
    italy_time = datetime.now(timezone.utc) + timedelta(hours=2)
    today = italy_time.strftime("%Y-%m-%d")
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    r = requests.get(url, headers=headers, params={"date": today})
    print(f"Cerco partite per {today}: {r.json().get('results', 0)} trovate")
    all_matches = r.json().get("response", [])
    filtered = [m for m in all_matches if is_allowed(m)]
    print(f"Dopo filtro: {len(filtered)} partite scommettibili")
    return filtered

# ── 2. Partite live (solo da minuto 30 in poi) ────────────────
def get_live_matches():
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    r = requests.get(url, headers=headers, params={"live": "all"})
    all_matches = r.json().get("response", [])
    return [
        m for m in all_matches
        if is_allowed(m) and (m['fixture']['status'].get('elapsed') or 0) >= 30
    ]

# ── 3. Statistiche squadra ────────────────────────────────────
def get_team_stats(team_id, league_id, season):
    url = "https://v3.football.api-sports.io/teams/statistics"
    headers = {"x-apisports-key": APIFOOTBALL}
    r = requests.get(url, headers=headers, params={"team": team_id, "league": league_id, "season": season})
    return r.json().get("response", {})

# ── 4. Infortuni e squalifiche ───────────────────────────────
def get_injuries(team_id, fixture_id):
    url = "https://v3.football.api-sports.io/injuries"
    headers = {"x-apisports-key": APIFOOTBALL}
    r = requests.get(url, headers=headers, params={"team": team_id, "fixture": fixture_id})
    return r.json().get("response", [])

# ── 5. Quote bookmaker ────────────────────────────────────────
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

# ── 6. Analisi AI pre-partita ─────────────────────────────────
def analyze_with_claude(match_data, stats_home, stats_away, injuries_home, injuries_away, odds):
    prompt = f"""
Sei un analista di scommesse sportive esperto. Analizza questa partita
e fornisci: 1) probabilità reali 1X2, 2) value bet migliore,
3) risultato esatto più probabile, 4) confidence score 0-100.

PARTITA: {match_data['teams']['home']['name']} vs {match_data['teams']['away']['name']}
DATA: {match_data['fixture']['date']}
STATISTICHE CASA: {stats_home}
STATISTICHE OSPITE: {stats_away}
INFORTUNI CASA: {[i['player']['name'] for i in injuries_home]}
INFORTUNI OSPITI: {[i['player']['name'] for i in injuries_away]}
QUOTE MERCATO: {odds[:3] if odds else 'non disponibili'}

Rispondi SOLO in JSON senza backtick con questi campi:
prob_home, prob_draw, prob_away, value_bet, quota_consigliata,
risultato_esatto, confidence, motivazione (max 3 righe).
"""
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text

# ── 7. Analisi AI live ────────────────────────────────────────
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

Rispondi SOLO in JSON senza backtick con questi campi:
giocata_consigliata, quota_live, motivazione_live (max 2 righe),
confidence_live (0-100), rischio (basso/medio/alto).
"""
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text

# ── 8. Formatta e invia su Telegram ──────────────────────────
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
    star = "⭐ <b>TOP VALUE BET</b>\n" if confidence >= 70 else ""
    return f"""
{star}⚽ <b>{home} vs {away}</b>
🏆 {country} — {league}
🕐 {kickoff} (ora italiana)

📊 Probabilità stimate:
  1️⃣ {home[:15]}: {a.get('prob_home','?')}%
  ➡️ Pareggio: {a.get('prob_draw','?')}%
  2️⃣ {away[:15]}: {a.get('prob_away','?')}%

💡 <b>Value Bet: {a.get('value_bet','N/A')}</b>
   Quota consigliata: {a.get('quota_consigliata','?')}
🎯 Risultato esatto: {a.get('risultato_esatto','?')}
🔥 Confidence: {confidence}/100

📝 {a.get('motivazione','')}
"""

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
    return f"""
🔴 <b>LIVE — {home} vs {away}</b>
🏆 {country} — {league}
⏱ Minuto: {minute}' | Punteggio: {score['home']}-{score['away']}

🎰 <b>Giocata: {a.get('giocata_consigliata','N/A')}</b>
   Quota live: {a.get('quota_live','?')}
⚠️ Rischio: {a.get('rischio','?')}
🔥 Confidence: {a.get('confidence_live','?')}/100

📝 {a.get('motivazione_live','')}
"""

# ── 9. Raggruppa per campionato ───────────────────────────────
def group_by_league(matches):
    leagues = {}
    for m in matches:
        key = f"{m['league']['country']} — {m['league']['name']}"
        if key not in leagues:
            leagues[key] = []
        leagues[key].append(m)
    return leagues

# ── 10. Statistiche previsioni ────────────────────────────────
def show_stats():
    history = load_history()
    if not history:
        send_telegram("📊 Nessuna previsione registrata ancora.")
        return
    total = len(history)
    correct = sum(1 for h in history if h.get('result') == 'win')
    pending = sum(1 for h in history if h.get('result') == 'pending')
    pct = round((correct / (total - pending)) * 100, 1) if (total - pending) > 0 else 0
    send_telegram(f"""
📊 <b>Statistiche previsioni</b>

📋 Totali: {total}
✅ Vinte: {correct}
❌ Perse: {total - correct - pending}
⏳ In attesa: {pending}
🎯 Precisione: {pct}%
""")

# ── 11. Analisi singola live (parallelismo) ───────────────────
def analyze_single_live(m):
    odds = get_odds(m['teams']['home']['name'], m['teams']['away']['name'])
    analysis = analyze_live_with_claude(m, odds)
    return format_live_message(m, analysis)

# ── 12. Job principale ────────────────────────────────────────
def daily_job():
    global stop_analysis
    stop_analysis = False
    print(f"[{datetime.now()}] Avvio analisi...")
    send_telegram("🔍 Avvio analisi partite di oggi...")

    matches = get_matches()
    if len(matches) == 0:
        send_telegram("⚠️ Nessuna partita trovata oggi nei campionati scommettibili.")
        return

    leagues = group_by_league(matches)
    send_telegram(f"📅 Trovate <b>{len(matches)}</b> partite in <b>{len(leagues)}</b> campionati. Analisi in corso...")

    history = load_history()
    top_bets = []

    for league_name, league_matches in leagues.items():
        if stop_analysis:
            send_telegram("🛑 Analisi fermata.")
            return
        send_telegram(f"🏆 <b>{league_name}</b> — {len(league_matches)} partite")
        time.sleep(1)

        for m in league_matches:
            if stop_analysis:
                send_telegram("🛑 Analisi fermata.")
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
        send_telegram("⭐ <b>TOP VALUE BETS DI OGGI (confidence ≥ 70)</b>")
        for _, msg in top_bets[:5]:
            send_telegram(msg)
            time.sleep(3)

    send_telegram("✅ <b>Analisi completata!</b>")

# ── 13. Job live ──────────────────────────────────────────────
def live_job():
    print(f"[{datetime.now()}] Analisi live...")
    matches = get_live_matches()
    if not matches:
        send_telegram("⚽ Nessuna partita live in corso con almeno 30 minuti giocati.")
        return

    send_telegram(f"🔴 <b>{len(matches)} partite live — analisi in corso...</b>")
    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(analyze_single_live, m): m for m in matches}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                print(f"Errore live: {e}")

    for msg in results:
        send_telegram(msg)
        time.sleep(2)
    send_telegram("✅ <b>Analisi live completata!</b>")

# ── 14. Listener comandi ──────────────────────────────────────
def listen_commands():
    global last_update_id, stop_analysis
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
                elif text == "/live":
                    threading.Thread(target=live_job).start()
                elif text == "/stop":
                    stop_analysis = True
                    send_telegram("🛑 Analisi fermata! Scrivi /analisi per riavviare.")
                elif text == "/stats":
                    show_stats()
                elif text == "/help":
                    send_telegram("""
🤖 <b>Comandi disponibili:</b>

/analisi — Partite di oggi (campionati Sisal)
/live — Giocate live (partite al 30'+)
/stop — Ferma analisi in corso
/stats — Statistiche previsioni
/help — Questo messaggio
""")
        except Exception as e:
            print(f"Errore listener: {e}")
        time.sleep(2)

# ── 15. Scheduler + avvio ─────────────────────────────────────
schedule.every(30).minutes.do(live_job)

if __name__ == "__main__":
    print("Bot avviato!")
    send_telegram("""🤖 <b>Bot avviato!</b>

/analisi — Partite di oggi
/live — Giocate live
/stop — Ferma analisi
/stats — Statistiche
/help — Aiuto""")

    t = threading.Thread(target=listen_commands, daemon=True)
    t.start()

    while True:
        schedule.run_pending()
        time.sleep(60)
