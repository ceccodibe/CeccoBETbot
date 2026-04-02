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
AUTO_NOTIFY_HOURS = 2  # Notifica automatica X ore prima

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

# ── API con retry automatico ──────────────────────────────────
def api_get(url, headers, params, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            return r.json()
        except Exception as e:
            print(f"Errore API (tentativo {attempt+1}/{retries}): {e}")
            time.sleep(3)
    return {}

# ── 1. Partite di oggi (solo non iniziate) ────────────────────
def get_matches():
    italy_time = datetime.now(timezone.utc) + timedelta(hours=2)
    today = italy_time.strftime("%Y-%m-%d")
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"date": today})
    print(f"Cerco partite per {today}: {data.get('results', 0)} trovate")
    filtered = []
    for m in data.get("response", []):
        if not is_allowed(m):
            continue
        status = m["fixture"]["status"]["short"]
        if status in ["NS", "TBD"]:
            filtered.append(m)
    print(f"Partite non ancora iniziate: {len(filtered)}")
    return filtered

# ── 2. Partite di domani ──────────────────────────────────────
def get_matches_tomorrow():
    italy_time = datetime.now(timezone.utc) + timedelta(hours=2)
    tomorrow = (italy_time + timedelta(days=1)).strftime("%Y-%m-%d")
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"date": tomorrow})
    print(f"Cerco partite per {tomorrow}: {data.get('results', 0)} trovate")
    return [m for m in data.get("response", []) if is_allowed(m)]

# ── 3. Partite live (dal 30' in poi) ──────────────────────────
def get_live_matches():
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"live": "all"})
    return [
        m for m in data.get("response", [])
        if is_allowed(m) and (m['fixture']['status'].get('elapsed') or 0) >= 30
    ]

# ── 4. Cerca partite per squadra ──────────────────────────────
def search_team_matches(team_name):
    italy_time = datetime.now(timezone.utc) + timedelta(hours=2)
    today = italy_time.strftime("%Y-%m-%d")
    tomorrow = (italy_time + timedelta(days=1)).strftime("%Y-%m-%d")
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    results = []
    for date in [today, tomorrow]:
        data = api_get(url, headers, {"date": date})
        for m in data.get("response", []):
            home = m['teams']['home']['name'].lower()
            away = m['teams']['away']['name'].lower()
            if team_name.lower() in home or team_name.lower() in away:
                results.append(m)
    return results

# ── 5. H2H tra due squadre ────────────────────────────────────
def get_h2h(home_id, away_id):
    url = "https://v3.football.api-sports.io/fixtures/headtohead"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"h2h": f"{home_id}-{away_id}", "last": 5})
    return data.get("response", [])

# ── 6. Classifica ─────────────────────────────────────────────
def get_standings(league_id, season):
    url = "https://v3.football.api-sports.io/standings"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"league": league_id, "season": season})
    try:
        standings = data["response"][0]["league"]["standings"][0]
        return [{
            "team": s["team"]["name"],
            "rank": s["rank"],
            "points": s["points"],
            "form": s.get("form", "")
        } for s in standings[:20]]
    except:
        return []

# ── 7. Statistiche squadra ────────────────────────────────────
def get_team_stats(team_id, league_id, season):
    url = "https://v3.football.api-sports.io/teams/statistics"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"team": team_id, "league": league_id, "season": season})
    return data.get("response", {})

# ── 8. Forma recente (ultime 5 partite) ───────────────────────
def get_recent_form(team_id, league_id, season):
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"team": team_id, "league": league_id, "season": season, "last": 5, "status": "FT"})
    matches = data.get("response", [])
    form = []
    for m in matches:
        home_id = m['teams']['home']['id']
        home_goals = m['goals']['home']
        away_goals = m['goals']['away']
        if team_id == home_id:
            result = "V" if home_goals > away_goals else ("P" if home_goals == away_goals else "S")
            form.append(f"{m['teams']['home']['name']} {home_goals}-{away_goals} {m['teams']['away']['name']} ({result})")
        else:
            result = "V" if away_goals > home_goals else ("P" if home_goals == away_goals else "S")
            form.append(f"{m['teams']['home']['name']} {home_goals}-{away_goals} {m['teams']['away']['name']} ({result})")
    return form

# ── 9. Infortuni ──────────────────────────────────────────────
def get_injuries(team_id, fixture_id):
    url = "https://v3.football.api-sports.io/injuries"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"team": team_id, "fixture": fixture_id})
    return data.get("response", [])

# ── 10. Quote bookmaker ───────────────────────────────────────
def get_odds(home, away):
    url = "https://api.the-odds-api.com/v4/sports/soccer/odds/"
    params = {"apiKey": ODDS_KEY, "regions": "eu", "markets": "h2h,totals", "oddsFormat": "decimal"}
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if not isinstance(data, list):
            return []
        for event in data:
            if not isinstance(event, dict):
                continue
            if home.lower() in event.get("home_team","").lower() or \
               away.lower() in event.get("away_team","").lower():
                return event.get("bookmakers", [])
    except Exception as e:
        print(f"Errore odds: {e}")
    return []

# ── 11. Analisi AI pre-partita ────────────────────────────────
def analyze_with_claude(match_data, stats_home, stats_away, injuries_home, injuries_away, odds, h2h, form_home, form_away, standings):
    prompt = f"""
Sei un analista di scommesse sportive esperto. Analizza questa partita
e fornisci un'analisi completa e accurata.

PARTITA: {match_data['teams']['home']['name']} vs {match_data['teams']['away']['name']}
DATA: {match_data['fixture']['date']}

STATISTICHE CASA: {stats_home}
STATISTICHE OSPITE: {stats_away}
FORMA RECENTE CASA (ultime 5): {form_home}
FORMA RECENTE OSPITE (ultime 5): {form_away}
CLASSIFICA: {standings[:10] if standings else 'non disponibile'}
TESTA A TESTA (ultimi 5): {h2h}
INFORTUNI CASA: {[i['player']['name'] for i in injuries_home]}
INFORTUNI OSPITI: {[i['player']['name'] for i in injuries_away]}
QUOTE MERCATO REALI: {odds[:3] if odds else 'non disponibili'}

Le quote reali dei bookmaker sono nel campo QUOTE MERCATO REALI.
Usale come riferimento principale per quota_consigliata, quota_over_under e quota_gol_no_gol.
NON inventare quote diverse da quelle reali. Se non disponibili scrivi N/D.

Rispondi SOLO in JSON senza backtick con questi campi:
prob_home, prob_draw, prob_away,
value_bet (1 X o 2), quota_consigliata (dalla quota reale),
over_under (Over 2.5 o Under 2.5), quota_over_under (reale o N/D),
gol_no_gol (Gol o No Gol), quota_gol_no_gol (reale o N/D),
risultato_esatto, confidence, motivazione (max 3 righe).
"""
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text

# ── 12. Analisi AI live ───────────────────────────────────────
def analyze_live_with_claude(match_data, odds):
    home = match_data['teams']['home']['name']
    away = match_data['teams']['away']['name']
    score = match_data['goals']
    minute = match_data['fixture']['status'].get('elapsed', '?')
    prompt = f"""
Sei un analista di scommesse sportive esperto in betting LIVE.
Analizza questa partita in corso e suggerisci le migliori giocate live.

PARTITA: {home} vs {away}
MINUTO: {minute}
PUNTEGGIO: {score['home']} - {score['away']}
QUOTE LIVE REALI: {odds[:3] if odds else 'non disponibili'}

Usa le quote reali dal campo QUOTE LIVE REALI.
NON inventare quote diverse. Se non disponibili scrivi N/D.

Rispondi SOLO in JSON senza backtick con questi campi:
giocata_consigliata (1 X o 2 live), quota_live (reale o N/D),
over_under_live (Over/Under adatto al minuto), quota_over_under_live (reale o N/D),
gol_no_gol_live (Gol o No Gol), quota_gol_no_gol_live (reale o N/D),
motivazione_live (max 2 righe),
confidence_live (0-100), rischio (basso/medio/alto).
"""
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text

# ── 13. Telegram ──────────────────────────────────────────────
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=15)
            print(f"Telegram: {r.status_code}")
            return
        except Exception as e:
            print(f"Errore Telegram (tentativo {attempt+1}/3): {e}")
            time.sleep(3)

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
        f"\U0001f4a1 <b>Value Bet: {a.get('value_bet','N/A')}</b> @ {a.get('quota_consigliata','?')}\n"
        f"\U0001f4ca <b>Over/Under: {a.get('over_under','N/A')}</b> @ {a.get('quota_over_under','?')}\n"
        f"\u26bd <b>Gol/No Gol: {a.get('gol_no_gol','N/A')}</b> @ {a.get('quota_gol_no_gol','?')}\n"
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
        f"\U0001f3b0 <b>Giocata: {a.get('giocata_consigliata','N/A')}</b> @ {a.get('quota_live','?')}\n"
        f"\U0001f4ca <b>Over/Under: {a.get('over_under_live','N/A')}</b> @ {a.get('quota_over_under_live','?')}\n"
        f"\u26bd <b>Gol/No Gol: {a.get('gol_no_gol_live','N/A')}</b> @ {a.get('quota_gol_no_gol_live','?')}\n"
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

# ── 14. Statistiche previsioni ────────────────────────────────
def show_stats():
    history = load_history()
    if not history:
        send_telegram("\U0001f4ca Nessuna previsione registrata ancora.")
        return
    total = len(history)
    correct = sum(1 for h in history if h.get('result') == 'win')
    pending = sum(1 for h in history if h.get('result') == 'pending')
    lost = total - correct - pending
    pct = round((correct / (total - pending)) * 100, 1) if (total - pending) > 0 else 0
    send_telegram(
        f"\U0001f4ca <b>Statistiche previsioni</b>\n\n"
        f"\U0001f4cb Totali: {total}\n"
        f"\u2705 Vinte: {correct}\n"
        f"\u274c Perse: {lost}\n"
        f"\u23f3 In attesa: {pending}\n"
        f"\U0001f3af Precisione: {pct}%\n"
    )

# ── 15. Top value bet ─────────────────────────────────────────
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

# ── 16. Risultato manuale ─────────────────────────────────────
def risultato_job(text):
    history = load_history()
    today = datetime.now().strftime("%Y-%m-%d")
    pending = [h for h in history if h.get('result') == 'pending' and h.get('date') == today]

    if not pending:
        send_telegram("\u26a0\ufe0f Nessuna previsione in attesa per oggi.")
        return

    msg = "\U0001f4cb <b>Previsioni in attesa di risultato:</b>\n\n"
    for i, h in enumerate(pending):
        msg += f"{i+1}. <b>{h['match']}</b>\n"
        msg += f"   \U0001f4a1 {h['value_bet']} | \U0001f525 {h['confidence']}/100\n\n"
    msg += "Rispondi con:\n<code>/vinta 1</code> oppure <code>/persa 1</code>\n(il numero corrisponde alla previsione)"
    send_telegram(msg)

def aggiorna_risultato(idx, esito):
    history = load_history()
    today = datetime.now().strftime("%Y-%m-%d")
    pending = [h for h in history if h.get('result') == 'pending' and h.get('date') == today]
    if idx < 1 or idx > len(pending):
        send_telegram("\u26a0\ufe0f Numero non valido.")
        return
    match_name = pending[idx-1]['match']
    for h in history:
        if h.get('match') == match_name and h.get('date') == today and h.get('result') == 'pending':
            h['result'] = esito
            break
    save_history(history)
    emoji = "\u2705" if esito == "win" else "\u274c"
    send_telegram(f"{emoji} Previsione aggiornata: <b>{match_name}</b> \u2014 {'Vinta' if esito == 'win' else 'Persa'}")

# ── 17. Analisi singola match ─────────────────────────────────
def analyze_match(m):
    home_id    = m['teams']['home']['id']
    away_id    = m['teams']['away']['id']
    home_name  = m['teams']['home']['name']
    away_name  = m['teams']['away']['name']
    league_id  = m['league']['id']
    season     = m['league']['season']
    fixture_id = m['fixture']['id']

    sh       = get_team_stats(home_id, league_id, season)
    sa       = get_team_stats(away_id, league_id, season)
    ih       = get_injuries(home_id, fixture_id)
    ia       = get_injuries(away_id, fixture_id)
    odds     = get_odds(home_name, away_name)
    h2h      = get_h2h(home_id, away_id)
    form_h   = get_recent_form(home_id, league_id, season)
    form_a   = get_recent_form(away_id, league_id, season)
    standings = get_standings(league_id, season)

    analysis = analyze_with_claude(m, sh, sa, ih, ia, odds, h2h, form_h, form_a, standings)
    return format_message(m, analysis), analysis

def analyze_single_live(m):
    odds = get_odds(m['teams']['home']['name'], m['teams']['away']['name'])
    analysis = analyze_live_with_claude(m, odds)
    return format_live_message(m, analysis)

# ── 18. Job principale ────────────────────────────────────────
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

            try:
                msg, analysis = analyze_match(m)
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

                # Notifica automatica X ore prima
                kick_utc = datetime.fromisoformat(m['fixture']['date'].replace('Z','+00:00'))
                now_aware = datetime.now(kick_utc.tzinfo)
                delay = (kick_utc - timedelta(hours=AUTO_NOTIFY_HOURS) - now_aware).total_seconds()
                if delay > 0 and confidence >= 70:
                    def delayed_send(msg=msg, delay=delay):
                        time.sleep(delay)
                        send_telegram(f"\u23f0 <b>Promemoria automatico!</b>\n{msg}")
                    threading.Thread(target=delayed_send, daemon=True).start()

            except Exception as e:
                print(f"Errore analisi {home_name} vs {away_name}: {e}")
                msg = f"\u26a0\ufe0f Errore analisi: {home_name} vs {away_name}"

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
        send_telegram("\u26a0\ufe0f Nessuna partita trovata domani.")
        return
    run_analysis(matches, "domani")

def live_job():
    global stop_live
    stop_live = False
    print(f"[{datetime.now()}] Analisi live...")
    matches = get_live_matches()
    if not matches:
        send_telegram("\u26bd Nessuna partita live con almeno 30 minuti giocati.")
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

def cerca_job(team_name):
    send_telegram(f"\U0001f50d Cerco partite di <b>{team_name}</b>...")
    matches = search_team_matches(team_name)
    if not matches:
        send_telegram(f"\u26a0\ufe0f Nessuna partita trovata per <b>{team_name}</b> oggi o domani.")
        return
    msg = f"\U0001f4cb <b>Partite trovate per {team_name}:</b>\n\n"
    for m in matches:
        home = m['teams']['home']['name']
        away = m['teams']['away']['name']
        league = m['league']['name']
        kick_utc = datetime.fromisoformat(m['fixture']['date'].replace('Z', '+00:00'))
        kick_it = kick_utc.astimezone(timezone(timedelta(hours=2)))
        kickoff = kick_it.strftime("%d/%m/%Y %H:%M")
        msg += f"\u26bd <b>{home} vs {away}</b>\n\U0001f3c6 {league}\n\U0001f550 {kickoff}\n\n"
    msg += "Vuoi analizzare? Scrivi <code>/analisi</code>"
    send_telegram(msg)

# ── 19. Listener comandi ──────────────────────────────────────
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
                text = msg.get("text", "").strip()
                text_lower = text.lower()
                user_id = msg.get("from", {}).get("id", 0)
                if user_id != ADMIN_ID:
                    continue
                if text_lower in ["/analisi", "/start"]:
                    threading.Thread(target=daily_job).start()
                elif text_lower == "/domani":
                    threading.Thread(target=domani_job).start()
                elif text_lower == "/live":
                    threading.Thread(target=live_job).start()
                elif text_lower == "/stop":
                    stop_analysis = True
                    stop_live = True
                    send_telegram("\U0001f6d1 Analisi fermata!")
                elif text_lower == "/stoplive":
                    stop_live = True
                    send_telegram("\U0001f6d1 Analisi live fermata!")
                elif text_lower == "/top":
                    top_job()
                elif text_lower == "/risultato":
                    risultato_job(text)
                elif text_lower.startswith("/vinta "):
                    try:
                        idx = int(text.split(" ")[1])
                        aggiorna_risultato(idx, "win")
                    except:
                        send_telegram("\u26a0\ufe0f Uso corretto: /vinta 1")
                elif text_lower.startswith("/persa "):
                    try:
                        idx = int(text.split(" ")[1])
                        aggiorna_risultato(idx, "loss")
                    except:
                        send_telegram("\u26a0\ufe0f Uso corretto: /persa 1")
                elif text_lower.startswith("/cerca "):
                    team = text[7:].strip()
                    if team:
                        threading.Thread(target=cerca_job, args=(team,)).start()
                    else:
                        send_telegram("\u26a0\ufe0f Uso corretto: /cerca Juventus")
                elif text_lower == "/stats":
                    show_stats()
                elif text_lower == "/help":
                    send_telegram(
                        "\U0001f916 <b>Comandi disponibili:</b>\n\n"
                        "/analisi \u2014 Partite di oggi\n"
                        "/domani \u2014 Partite di domani\n"
                        "/live \u2014 Giocate live (partite al 30'+)\n"
                        "/top \u2014 Top value bet oggi\n"
                        "/cerca [squadra] \u2014 Cerca partite di una squadra\n"
                        "/risultato \u2014 Vedi previsioni in attesa\n"
                        "/vinta [n] \u2014 Segna previsione come vinta\n"
                        "/persa [n] \u2014 Segna previsione come persa\n"
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
        "/cerca [squadra] \u2014 Cerca squadra\n"
        "/risultato \u2014 Aggiorna risultati\n"
        "/stats \u2014 Statistiche\n"
        "/help \u2014 Aiuto"
    )
    t = threading.Thread(target=listen_commands, daemon=True)
    t.start()
    while True:
        time.sleep(60)
