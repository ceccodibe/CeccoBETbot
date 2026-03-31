import os, requests, anthropic, schedule, time, json
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

APIFOOTBALL = os.getenv("APIFOOTBALL_KEY")
ODDS_KEY    = os.getenv("ODDS_API_KEY")
TG_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TG_CHAT     = os.getenv("TELEGRAM_CHAT_ID")
client      = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ── 1. Partite del giorno ──────────────────────────────────────
def get_matches():
    today = datetime.now().strftime("%Y-%m-%d")
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    r = requests.get(url, headers=headers, params={"date": today})
    return r.json()["response"]

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
    requests.post(url, json={
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "HTML"
    })

def format_message(match, analysis):
    try:
        a = json.loads(analysis.strip("` \njson"))
    except:
        a = {}
    home = match['teams']['home']['name']
    away = match['teams']['away']['name']
    kickoff = match['fixture']['date'][11:16]
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

# ── 7. Job principale ─────────────────────────────────────────
def daily_job():
    print(f"[{datetime.now()}] Avvio analisi partite...")
    matches = get_matches()
    print(f"Trovate {len(matches)} partite oggi.")

    for m in matches:
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

        # Invia 2h prima del calcio d'inizio
        notify_at = kick_utc - timedelta(hours=2)
        now_aware = datetime.now(kick_utc.tzinfo)
        delay = (notify_at - now_aware).total_seconds()

        if delay > 0:
            print(f"Invio tra {int(delay/60)} minuti...")
            time.sleep(delay)

        send_telegram(msg)
        print(f"Inviato: {home_name} vs {away_name}")
        time.sleep(5)

# ── 8. Scheduler ──────────────────────────────────────────────
schedule.every().day.at("08:00").do(daily_job)
send_telegram("🤖 Test bot funzionante!")
daily_job()

if __name__ == "__main__":
    print("Bot avviato. In attesa delle 08:00...")
    print("Premi Ctrl+C per fermarlo.")
    while True:
        schedule.run_pending()
        time.sleep(60)
