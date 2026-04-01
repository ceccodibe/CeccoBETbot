import os, requests, anthropic, schedule, time, json, threading
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

APIFOOTBALL = os.getenv("APIFOOTBALL_KEY")
ODDS_KEY    = os.getenv("ODDS_API_KEY")
TG_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TG_CHAT     = os.getenv("TELEGRAM_CHAT_ID")
client      = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

last_update_id = 0

# ── Campionati consentiti (paese, nome lega) ───────────────────
ALLOWED_LEAGUES = [
    ("Italy", "Serie A"),
    ("Italy", "Serie B"),
    ("England", "Premier League"),
    ("Spain", "La Liga"),
    ("Germany", "Bundesliga"),
    ("France", "Ligue 1"),
    ("World", "UEFA Champions League"),
    ("World", "UEFA Europa League"),
    ("World", "UEFA Europa Conference League"),
    ("Netherlands", "Eredivisie"),
    ("Portugal", "Primeira Liga"),
    ("World", "Friendlies"),
]

# ── 1. Partite del giorno ──────────────────────────────────────
def get_matches():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    r = requests.get(url, headers=headers, params={"date": today})
    all_matches = r.json().get("response", [])

    filtered = [
        m for m in all_matches
        if any(
            country.lower() in m['league']['country'].lower() and
            league.lower() in m['league']['name'].lower()
            for country, league in ALLOWED_LEAGUES
        )
    ]
    return filtered

# ── 2. Statistiche squadra ─────────────────────────────────────
def get_team_stats(team_id, league_id, season):
    url = "https://v3.football.api-sports.io/teams/statistics"
    headers = {"x-apisports-key": APIFOOTBALL}
    params = {"team": team_id, "league": league_id, "season": season}
    r = requests.get(url, headers=headers, params=params)
    return r.json().get("response", {})

# ── 3. Infortuni e squalifiche ────────────────────────────────
def get_injuries(team_id, fixture_id):
    url = "https://v3.football.api-sports.io/injuries"
    headers = {"x-apisports-key": APIFOOTBALL}
    r = requests.get(url, headers=headers,
                     params={"team": team_id, "fixture": fixture_id})
    return r.json().get("response", [])

# ── 4. Quote bookmaker ────────────────────────────────────────
def get_odds(home, away):
    url = "https://api.the-odds-api.com/v4/sports/soccer/odds/"
    params = {
        "apiKey": ODDS_KEY,
        "regions": "eu",
        "markets": "h2h,totals",
        "oddsFormat": "decimal"
    }
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

# ── 5. Analisi AI + Value Bet ─────────────────────────────────
def analyze_with_claude(match_data, stats_home, stats_away,
                        injuries_home, injuries_away, odds):
    prompt = f"""
Sei un analista di scommesse sportive esperto. Analizza questa partita
e fornisci: 1) probabilità reali 1X2, 2) value bet migliore,
3) risultato esatto più probabile, 4) confidence score 0-100.

PARTITA: {match_data['teams']['home']['name']} vs
         {match_data['teams']['away']['name']}
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

# ── 6. Formatta e invia su Telegram ───────────────────────────
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "HTML"
    })
    print(f"Telegram response: {r.json()}")

def format_message(match, analysis):
    try:
        a = json.loads(analysis.strip("` \njson"))
    except:
        a = {}
    home = match['teams']['home']['name']
    away = match['teams']['away']['name']
    kick_utc = datetime.fromisoformat(match['fixture']['date'].replace('Z','+00:00'))
    kick_it = kick_utc + timedelta(hours=2)
    kickoff = kick_it.strftime("%H:%M")
    return f"""
⚽ <b>{home} vs {away}</b>
🕐 Calcio d'inizio: {kickoff}

📊 Probabilità stimate:
  1️⃣ {home[:15]}: {a.get('prob_home','?')}%
  ➡️ Pareggio: {a.get('prob_draw','?')}%
  2️⃣ {away[:15]}: {a.get('prob_away','?')}%

💡 <b>Value Bet: {a.get('value_bet','N/A')}</b>
   Quota consigliata: {a.get('quota_consigliata','?')}
🎯 Risultato esatto: {a.get('risultato_esatto','?')}
🔥 Confidence: {a.get('confidence','?')}/100

📝 {a.get('motivazione','')}
"""

# ── 7. Raggruppa partite per campionato ───────────────────────
def group_by_league(matches):
    leagues = {}
    for m in matches:
        league_name = m['league']['name']
        country = m['league']['country']
        key = f"{country} — {league_name}"
        if key not in leagues:
            leagues[key] = []
        leagues[key].append(m)
    return leagues

# ── 8. Job principale ─────────────────────────────────────────
def daily_job():
    print(f"[{datetime.now()}] Avvio analisi partite...")
    send_telegram("🔍 Avvio analisi partite del giorno...")
    matches = get_matches()
    print(f"Trovate {len(matches)} partite nei campionati selezionati.")

    if len(matches) == 0:
        send_telegram("⚠️ Nessuna partita trovata oggi nei campionati selezionati.")
        return

    leagues = group_by_league(matches)
    send_telegram(f"📅 Trovate <b>{len(matches)}</b> partite in <b>{len(leagues)}</b> campionati. Analisi in corso...")

    for league_name, league_matches in leagues.items():
        send_telegram(f"🏆 <b>{league_name}</b> — {len(league_matches)} partite")
        time.sleep(1)

        for m in league_matches:
            fixture_id = m['fixture']['id']
            home_id    = m['teams']['home']['id']
            away_id    = m['teams']['away']['id']
            league_id  = m['league']['id']
            season     = m['league']['season']
            home_name  = m['teams']['home']['name']
            away_name  = m['teams']['away']['name']
            kick_utc   = datetime.fromisoformat(
                             m['fixture']['date'].replace('Z','+00:00'))

            print(f"Analisi: {home_name} vs {away_name}...")

            sh   = get_team_stats(home_id, league_id, season)
            sa   = get_team_stats(away_id, league_id, season)
            ih   = get_injuries(home_id, fixture_id)
            ia   = get_injuries(away_id, fixture_id)
            odds = get_odds(home_name, away_name)

            analysis = analyze_with_claude(m, sh, sa, ih, ia, odds)
            msg = format_message(m, analysis)

            notify_at = kick_utc - timedelta(hours=2)
            now_aware = datetime.now(kick_utc.tzinfo)
            delay = (notify_at - now_aware).total_seconds()

            if delay > 0:
                print(f"Invio tra {int(delay/60)} minuti...")
                time.sleep(delay)

            send_telegram(msg)
            print(f"Inviato: {home_name} vs {away_name}")
            time.sleep(5)

# ── 9. Listener comandi Telegram ──────────────────────────────
def listen_commands():
    global last_update_id
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
    while True:
        try:
            r = requests.get(url, params={"timeout": 30, "offset": last_update_id + 1}, timeout=35)
            updates = r.json().get("result", [])
            for update in updates:
                last_update_id = update["update_id"]
                msg = update.get("message", {}) or update.get("channel_post", {})
                text = msg.get("text", "")
                if text.strip().lower() in ["/analisi", "/start"]:
                    print("Comando /analisi ricevuto!")
                    threading.Thread(target=daily_job).start()
        except Exception as e:
            print(f"Errore listener: {e}")
        time.sleep(2)

# ── 10. Scheduler + avvio ─────────────────────────────────────
schedule.every().day.at("08:00").do(daily_job)

if __name__ == "__main__":
    print("Bot avviato!")
    print("Comandi disponibili: /analisi")
    send_telegram("🤖 Bot avviato! Scrivi /analisi per avviare l'analisi manualmente.")

    t = threading.Thread(target=listen_commands, daemon=True)
    t.start()

    while True:
        schedule.run_pending()
        time.sleep(60)
