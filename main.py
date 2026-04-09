import os, requests, anthropic, time, json, threading, schedule
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
from pymongo import MongoClient

def parse_json_safe(text):
    """Parsing JSON robusto che gestisce backtick, json prefix e testo extra"""
    try:
        clean = text.strip()
        clean = clean.replace('```json', '').replace('```', '').strip()
        if clean.startswith('json'):
            clean = clean[4:].strip()
        start = clean.find('{')
        end = clean.rfind('}')
        if start != -1 and end != -1:
            clean = clean[start:end+1]
        return json.loads(clean)
    except:
        return {}

load_dotenv()

APIFOOTBALL  = os.getenv("APIFOOTBALL_KEY")
ODDS_KEY     = os.getenv("ODDS_API_KEY")
TG_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TG_CHAT      = os.getenv("TELEGRAM_CHAT_ID")
TG_CHAT_LIVE = os.getenv("TELEGRAM_CHAT_LIVE") or os.getenv("TELEGRAM_CHAT_ID")
client       = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
print(f"Chat Live ID: {TG_CHAT_LIVE}")

mongo_client = MongoClient(os.getenv("MONGODB_URI"))
db = mongo_client["ceccobet"]
predictions_col = db["predictions"]

last_update_id = 0
stop_analysis  = False
stop_live      = False
ADMIN_IDS      = [8317266009, 2129248376]
CONFIDENCE_MIN = 68

COUNTRY_FLAGS = {
    "Italy": "\U0001f1ee\U0001f1f9",
    "England": "\U0001f3f4",
    "Spain": "\U0001f1ea\U0001f1f8",
    "Germany": "\U0001f1e9\U0001f1ea",
    "France": "\U0001f1eb\U0001f1f7",
    "Portugal": "\U0001f1f5\U0001f1f9",
    "Netherlands": "\U0001f1f3\U0001f1f1",
    "Belgium": "\U0001f1e7\U0001f1ea",
    "Turkey": "\U0001f1f9\U0001f1f7",
    "Brazil": "\U0001f1e7\U0001f1f7",
    "Argentina": "\U0001f1e6\U0001f1f7",
    "Colombia": "\U0001f1e8\U0001f1f4",
    "USA": "\U0001f1fa\U0001f1f8",
    "World": "\U0001f30d",
}

def get_flag(country):
    return COUNTRY_FLAGS.get(country, "\U0001f3c6")

ALLOWED_LEAGUES = [
    ("Italy", "Serie A"), ("Italy", "Serie B"), ("Italy", "Coppa Italia"),
    ("England", "Premier League"), ("England", "Championship"), ("England", "FA Cup"),
    ("France", "Ligue 1"), ("France", "Ligue 2"), ("France", "Coupe de France"),
    ("Spain", "La Liga"), ("Spain", "Copa del Rey"),
    ("Germany", "Bundesliga"), ("Germany", "2. Bundesliga"), ("Germany", "DFB Pokal"),
    ("Portugal", "Primeira Liga"),
    ("Netherlands", "Eredivisie"), ("Netherlands", "Eerste Divisie"),
    ("Belgium", "Pro League"),
    ("Argentina", "Liga Profesional"),
    ("Brazil", "Serie A"), ("Brazil", "Serie B"),
    ("Colombia", "Primera A"),
    ("Turkey", "Super Lig"),
    ("USA", "MLS"),
    ("World", "UEFA Champions League"),
    ("World", "UEFA Europa League"),
    ("World", "UEFA Europa Conference League"),
    ("World", "FIFA World Cup"),
]

EXCLUDE_KEYWORDS = [
    'u19','u18','u17','u16','u15','u23','u21','u20',
    'youth','under','reserve','riserve','primavera',
    ' w ','women','femminile','femenino','feminine','ladies','girls'
]

SPORT_KEYS = [
    "soccer_italy_serie_a", "soccer_italy_serie_b",
    "soccer_england_premier_league", "soccer_efl_champ",
    "soccer_france_ligue_one", "soccer_france_ligue_two",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga", "soccer_germany_bundesliga2",
    "soccer_portugal_primeira_liga",
    "soccer_netherlands_eredivisie",
    "soccer_belgium_first_div",
    "soccer_brazil_campeonato",
    "soccer_argentina_primera_division",
    "soccer_usa_mls",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
]

def is_allowed(m):
    league_name = m['league']['name'].lower()
    home = m['teams']['home']['name'].lower()
    away = m['teams']['away']['name'].lower()
    for kw in EXCLUDE_KEYWORDS:
        if kw in league_name or kw in home or kw in away:
            return False
    return any(
        country.lower() in m['league']['country'].lower() and
        league.lower() in m['league']['name'].lower()
        for country, league in ALLOWED_LEAGUES
    )

def load_history():
    try:
        return list(predictions_col.find({}, {"_id": 0}))
    except:
        return []

def add_prediction(pred):
    try:
        predictions_col.insert_one({k: v for k, v in pred.items() if k != '_id'})
    except Exception as e:
        print(f"Errore MongoDB insert: {e}")

def update_prediction(match_name, date, result, actual_result=None, score=None):
    try:
        update = {"result": result}
        if actual_result: update["actual_result"] = actual_result
        if score: update["score"] = score
        predictions_col.update_one(
            {"match": match_name, "date": date, "result": "pending"},
            {"$set": update}
        )
    except Exception as e:
        print(f"Errore MongoDB update: {e}")

def api_get(url, headers, params, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            return r.json()
        except Exception as e:
            print(f"Errore API ({attempt+1}/{retries}): {e}")
            time.sleep(3 * (attempt + 1))
    return {}

def get_matches():
    italy_time = datetime.now(timezone.utc) + timedelta(hours=2)
    today = italy_time.strftime("%Y-%m-%d")
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"date": today})
    print(f"Cerco partite per {today}: {data.get('results', 0)} trovate")
    return [m for m in data.get("response", [])
            if is_allowed(m) and m["fixture"]["status"]["short"] in ["NS", "TBD"]]

def get_matches_tomorrow():
    italy_time = datetime.now(timezone.utc) + timedelta(hours=2)
    tomorrow = (italy_time + timedelta(days=1)).strftime("%Y-%m-%d")
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"date": tomorrow})
    return [m for m in data.get("response", []) if is_allowed(m)]

def get_live_matches():
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"live": "all"})
    filtered = []
    for m in data.get("response", []):
        if not is_allowed(m):
            continue
        elapsed = m["fixture"]["status"].get("elapsed") or 0
        if not (30 <= elapsed <= 80):
            continue
        hg = m["goals"].get("home") or 0
        ag = m["goals"].get("away") or 0
        if abs(hg - ag) <= 1 or (hg + ag) >= 2:
            filtered.append(m)
    return filtered

def get_live_stats(fixture_id):
    url = "https://v3.football.api-sports.io/fixtures/statistics"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"fixture": fixture_id})
    stats = {}
    for team_stats in data.get("response", []):
        team_name = team_stats.get("team", {}).get("name", "")
        for s in team_stats.get("statistics", []):
            if s.get("type") in ["Ball Possession", "Total Shots", "Shots on Goal", "Corner Kicks", "Yellow Cards"]:
                if team_name not in stats:
                    stats[team_name] = {}
                stats[team_name][s["type"]] = s.get("value", 0) or 0
    return stats

def get_live_events(fixture_id):
    url = "https://v3.football.api-sports.io/fixtures/events"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"fixture": fixture_id})
    recent = []
    for e in data.get("response", [])[-5:]:
        minute = e.get("time", {}).get("elapsed", "?")
        etype = e.get("type", "")
        team = e.get("team", {}).get("name", "")
        player = e.get("player", {}).get("name", "")
        recent.append(f"{minute}' {team} - {etype} ({player})")
    return recent

def search_team_matches(team_name):
    italy_time = datetime.now(timezone.utc) + timedelta(hours=2)
    results = []
    for delta in [0, 1]:
        date = (italy_time + timedelta(days=delta)).strftime("%Y-%m-%d")
        url = "https://v3.football.api-sports.io/fixtures"
        headers = {"x-apisports-key": APIFOOTBALL}
        data = api_get(url, headers, {"date": date})
        for m in data.get("response", []):
            if team_name.lower() in m['teams']['home']['name'].lower() or \
               team_name.lower() in m['teams']['away']['name'].lower():
                results.append(m)
    return results

def get_h2h(home_id, away_id):
    url = "https://v3.football.api-sports.io/fixtures/headtohead"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"h2h": f"{home_id}-{away_id}", "last": 5})
    return data.get("response", [])

def get_standings(league_id, season):
    url = "https://v3.football.api-sports.io/standings"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"league": league_id, "season": season})
    try:
        standings = data["response"][0]["league"]["standings"][0]
        return [{"team": s["team"]["name"], "rank": s["rank"],
                 "points": s["points"], "form": s.get("form", "")}
                for s in standings[:8]]
    except:
        return []

def get_team_stats(team_id, league_id, season):
    url = "https://v3.football.api-sports.io/teams/statistics"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"team": team_id, "league": league_id, "season": season})
    return data.get("response", {})

def get_recent_form(team_id, league_id, season):
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"team": team_id, "league": league_id,
                                   "season": season, "last": 10, "status": "FT"})
    form = []
    home_record = {"V": 0, "P": 0, "S": 0, "gf": 0, "gs": 0}
    away_record = {"V": 0, "P": 0, "S": 0, "gf": 0, "gs": 0}
    for m in data.get("response", []):
        home_id = m['teams']['home']['id']
        hg = m['goals']['home'] or 0
        ag = m['goals']['away'] or 0
        is_home = team_id == home_id
        if is_home:
            r = "V" if hg > ag else ("P" if hg == ag else "S")
            home_record[r] += 1
            home_record["gf"] += hg
            home_record["gs"] += ag
            venue = "C"
        else:
            r = "V" if ag > hg else ("P" if hg == ag else "S")
            away_record[r] += 1
            away_record["gf"] += ag
            away_record["gs"] += hg
            venue = "T"
        form.append(f"[{venue}]{m['teams']['home']['name']} {hg}-{ag} {m['teams']['away']['name']}({r})")
    form.append(f"Casa:{home_record['V']}V{home_record['P']}P{home_record['S']}S GF:{home_record['gf']} GS:{home_record['gs']}")
    form.append(f"Trasferta:{away_record['V']}V{away_record['P']}P{away_record['S']}S GF:{away_record['gf']} GS:{away_record['gs']}")
    return form

def get_injuries(team_id, fixture_id):
    url = "https://v3.football.api-sports.io/injuries"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"team": team_id, "fixture": fixture_id})
    return data.get("response", [])

def get_news_sentiment(team_name):
    try:
        query = team_name.replace(" ", "+")
        url = f"https://news.google.com/rss/search?q={query}+calcio&hl=it&gl=IT&ceid=IT:it"
        r = requests.get(url, timeout=8)
        root = ET.fromstring(r.content)
        return [item.find("title").text[:80] for item in root.findall(".//item")[:2]
                if item.find("title") is not None]
    except:
        return []

_odds_cache = []
_odds_cache_time = 0

def load_all_odds():
    """Carica tutte le quote in UNA sola chiamata — risparmia crediti API"""
    global _odds_cache, _odds_cache_time
    now = time.time()
    if now - _odds_cache_time < 43200:  # Cache per 12 ore
        return _odds_cache
    all_events = []
    try:
        # Una sola chiamata per tutto il calcio
        url = "https://api.the-odds-api.com/v4/sports/soccer/odds/"
        params = {
            "apiKey": ODDS_KEY,
            "regions": "eu,it",
            "markets": "h2h",
            "oddsFormat": "decimal",
            "dateFormat": "iso"
        }
        r = requests.get(url, params=params, timeout=30)
        data = r.json()
        if isinstance(data, list):
            all_events = data
            print(f"Quote caricate: {len(all_events)} eventi totali")
        else:
            print(f"Odds error: {data.get('error_code','?')} - {data.get('message','')}")
    except Exception as e:
        print(f"Odds exception: {e}")
    _odds_cache = all_events
    _odds_cache_time = now
    return all_events

def get_odds(home, away):
    data = load_all_odds()
    h = home.lower().strip()
    a = away.lower().strip()
    for event in data:
        if not isinstance(event, dict):
            continue
        ev_home = event.get("home_team", "").lower()
        ev_away = event.get("away_team", "").lower()
        if (h in ev_home or ev_home in h or h.split()[0] in ev_home) and \
           (a in ev_away or ev_away in a or a.split()[0] in ev_away):
            return event.get("bookmakers", [])
    return []

def confronto_quote(home, away):
    data = load_all_odds()
    h = home.lower().strip()
    a = away.lower().strip()
    best = {"1": (0, ""), "X": (0, ""), "2": (0, "")}
    for event in data:
        if not isinstance(event, dict):
            continue
        ev_home = event.get("home_team", "").lower()
        ev_away = event.get("away_team", "").lower()
        if (h in ev_home or ev_home in h or h.split()[0] in ev_home) and \
           (a in ev_away or ev_away in a or a.split()[0] in ev_away):
            for bk in event.get("bookmakers", []):
                bk_name = bk.get("title", "")
                for market in bk.get("markets", []):
                    if market.get("key") == "h2h":
                        for o in market.get("outcomes", []):
                            price = o.get("price", 0)
                            name = o.get("name", "").lower()
                            if name == event.get("home_team","").lower():
                                if price > best["1"][0]: best["1"] = (price, bk_name)
                            elif name == "draw":
                                if price > best["X"][0]: best["X"] = (price, bk_name)
                            elif name == event.get("away_team","").lower():
                                if price > best["2"][0]: best["2"] = (price, bk_name)
            return best
    return {}

def calcola_ev_reale(prob_stimata, quota):
    try:
        prob = float(prob_stimata) / 100
        q = float(str(quota).replace(',','.'))
        if q <= 1:
            return None, None
        ev = (prob * (q - 1)) - (1 - prob)
        valore = prob - (1 / q)
        return round(ev * 100, 2), round(valore * 100, 2)
    except:
        return None, None

def get_calibrazione_confidence():
    try:
        history = list(predictions_col.find(
            {"result": {"$in": ["win", "loss"]}},
            {"_id": 0, "confidence": 1, "result": 1}
        ))
        if len(history) < 20:
            return 1.0
        wins_alto = sum(1 for h in history if h.get("confidence", 0) >= 70 and h.get("result") == "win")
        total_alto = sum(1 for h in history if h.get("confidence", 0) >= 70)
        acc_alto = wins_alto / total_alto if total_alto > 0 else 0.7
        calibrazione = acc_alto / 0.70
        return round(min(max(calibrazione, 0.5), 1.5), 2)
    except:
        return 1.0

def get_match_importance(match_data):
    league = match_data['league']['name'].lower()
    round_info = match_data['league'].get('round', '').lower()
    if any(k in league for k in ['champions', 'europa', 'conference', 'world cup', 'coppa', 'cup', 'pokal', 'fa cup']):
        if any(k in round_info for k in ['final', 'finale', 'semi', 'quarter', 'quarti']):
            return "FINALE/SEMIFINALE DI COPPA - tattica conservativa, meno gol"
        return "PARTITA DI COPPA - possibili rotazioni"
    if any(k in round_info for k in ['38', '37', '36']):
        return "ULTIMA GIORNATA - alta tensione, titolo/retrocessione in gioco"
    return "CAMPIONATO REGOLARE"

def analyze_with_claude(match_data, stats_home, stats_away, injuries_home,
                        injuries_away, odds, h2h, form_home, form_away, standings,
                        news_home=None, news_away=None):
    importanza = get_match_importance(match_data)
    home = match_data['teams']['home']['name']
    away = match_data['teams']['away']['name']
    infortuni_h = [i['player']['name'] for i in injuries_home]
    infortuni_a = [i['player']['name'] for i in injuries_away]
    prompt = f"""Analista scommesse. Analizza {home} vs {away}.
IMPORTANZA: {importanza}
FORMA CASA(10): {form_home}
FORMA OSPITE(10): {form_away}
CLASSIFICA: {standings}
H2H: {h2h[:3] if h2h else 'N/D'}
INFORTUNI: Casa={infortuni_h} Ospite={infortuni_a}
NOTIZIE: Casa={news_home[:2] if news_home else 'N/D'} Ospite={news_away[:2] if news_away else 'N/D'}
QUOTE: {odds[:2] if odds else 'N/D'}
Rispondi SOLO con JSON valido senza backtick:
{{"prob_home":X,"prob_draw":X,"prob_away":X,"value_bet":"1 o X o 2","quota_consigliata":X,"over_under":"Over/Under 2.5","quota_over_under":X,"gol_no_gol":"Gol o No Gol","quota_gol_no_gol":X,"risultato_esatto":"X-X","confidence":X,"motivazione":"max 2 righe"}}"""
    msg = client.messages.create(
        model="claude-opus-4-5", max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text

def analyze_live_with_claude(match_data, odds, stats=None, events=None):
    home = match_data['teams']['home']['name']
    away = match_data['teams']['away']['name']
    score = match_data['goals']
    minute = match_data['fixture']['status'].get('elapsed', '?')
    hg = score.get('home') or 0
    ag = score.get('away') or 0
    elapsed = int(str(minute).replace('+','')) if str(minute).replace('+','').isdigit() else 45
    if elapsed <= 45:
        fase = "PRIMO TEMPO"
        focus = "Giocate su risultato finale o Over/Under totale"
    elif elapsed <= 60:
        fase = "INIZIO 2T"
        focus = "Analizza momentum. Giocate su risultato, GG/NG o Over/Under"
    else:
        fase = "2T AVANZATO"
        focus = "Preferisci Over/Under 2T, prossimo gol, o risultato finale"
    momentum = ""
    if stats:
        home_s = stats.get(home, {})
        away_s = stats.get(away, {})
        hs = home_s.get("Total Shots", 0)
        as_ = away_s.get("Total Shots", 0)
        hp = home_s.get("Ball Possession", "50%")
        if hs > as_ + 3:
            momentum = f"{home} domina ({hs} tiri vs {as_}, poss {hp})"
        elif as_ > hs + 3:
            momentum = f"{away} domina ({as_} tiri vs {hs})"
        else:
            momentum = f"Equilibrio ({hs} tiri {home} vs {as_} {away}, poss {hp})"
    prompt = f"""Analista LIVE. {home} vs {away} - {fase} min {minute}' - {hg}-{ag}
EVENTI: {events[-3:] if events else 'N/D'}
MOMENTUM: {momentum}
QUOTE: {odds[:2] if odds else 'N/D'}
{focus}
Rispondi SOLO con JSON valido senza backtick:
{{"giocata_consigliata":"descrizione","quota_live":X,"momentum":"una riga","motivazione_live":"max 2 righe","confidence_live":X,"rischio":"basso/medio/alto"}}"""
    msg = client.messages.create(
        model="claude-opus-4-5", max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, json={"chat_id": TG_CHAT, "text": text,
                                          "parse_mode": "HTML"}, timeout=15)
            print(f"Telegram: {r.status_code}")
            return
        except Exception as e:
            print(f"Errore Telegram ({attempt+1}/3): {e}")
            time.sleep(3)

def send_telegram_live(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, json={"chat_id": str(TG_CHAT_LIVE), "text": text,
                                          "parse_mode": "HTML"}, timeout=15)
            print(f"Telegram Live: {r.status_code}")
            return
        except Exception as e:
            print(f"Errore Telegram Live ({attempt+1}/3): {e}")
            time.sleep(3)

def send_telegram_admin(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, json={"chat_id": str(ADMIN_IDS[0]), "text": text,
                                          "parse_mode": "HTML"}, timeout=15)
            print(f"Telegram Admin: {r.status_code}")
            return
        except Exception as e:
            print(f"Errore Telegram Admin ({attempt+1}/3): {e}")
            time.sleep(3)

def format_match_block(match, analysis, best_odds=None):
    a = parse_json_safe(analysis)
    home = match['teams']['home']['name']
    away = match['teams']['away']['name']
    kick_utc = datetime.fromisoformat(match['fixture']['date'].replace('Z', '+00:00'))
    kick_it = kick_utc.astimezone(timezone(timedelta(hours=2)))
    kickoff = kick_it.strftime("%H:%M")
    confidence = int(a.get('confidence', 0))
    ev, _ = calcola_ev_reale(a.get('prob_home' if a.get('value_bet','') == '1' else
                              'prob_draw' if a.get('value_bet','') == 'X' else 'prob_away', 0),
                              a.get('quota_consigliata', 0))
    ev_str = f" \U0001f4c8 EV:{'+' if ev and ev > 0 else ''}{ev}%" if ev is not None else ""
    star = "\u2b50 <b>TOP VALUE BET</b>\n" if confidence >= CONFIDENCE_MIN else ""
    best_str = ""
    if best_odds:
        parts = [f"{esito}:{q}" for esito, (q, bk) in best_odds.items() if q > 0]
        if parts:
            best_str = "\U0001f4b0 Best: " + " | ".join(parts) + "\n"
    return (
        f"{'━'*28}\n"
        f"{star}"
        f"\u26bd <b>{home}</b> vs <b>{away}</b>\n"
        f"\U0001f550 {kickoff} (IT)\n\n"
        f"\U0001f4ca <b>Probabilita:</b>\n"
        f"   1\ufe0f\u20e3 {home[:12]}: <b>{a.get('prob_home','?')}%</b>\n"
        f"   \u274f Pareggio: <b>{a.get('prob_draw','?')}%</b>\n"
        f"   2\ufe0f\u20e3 {away[:12]}: <b>{a.get('prob_away','?')}%</b>\n\n"
        f"\U0001f3af <b>Scommesse:</b>\n"
        f"   \U0001f4a1 {a.get('value_bet','?')} @ <b>{a.get('quota_consigliata','?')}</b>{ev_str}\n"
        f"   \U0001f4ca O/U: {a.get('over_under','?')} @ <b>{a.get('quota_over_under','?')}</b>\n"
        f"   \u26bd GG/NG: {a.get('gol_no_gol','?')} @ <b>{a.get('quota_gol_no_gol','?')}</b>\n\n"
        f"\U0001f3af Esatto: <b>{a.get('risultato_esatto','?')}</b> | \U0001f525 <b>{confidence}/100</b>\n"
        f"{best_str}"
        f"\U0001f4dd {a.get('motivazione','')}\n"
    )

def format_live_block(match, analysis):
    a = parse_json_safe(analysis)
    home = match['teams']['home']['name']
    away = match['teams']['away']['name']
    score = match['goals']
    minute = match['fixture']['status'].get('elapsed', '?')
    momentum = a.get('momentum', '')
    rischio = a.get('rischio', 'N/D')
    rischio_emoji = "\U0001f7e2" if rischio == "basso" else ("\U0001f7e1" if rischio == "medio" else "\U0001f534")
    return (
        f"{'━'*28}\n"
        f"\U0001f534 <b>{home}</b> vs <b>{away}</b>\n"
        f"\u23f1 {minute}' | <b>{score['home']}-{score['away']}</b>\n"
        + (f"\U0001f4ca <i>{momentum}</i>\n" if momentum else "")
        + f"\n\U0001f3af <b>Giocata:</b> {a.get('giocata_consigliata','N/A')} @ <b>{a.get('quota_live','?')}</b>\n"
        f"{rischio_emoji} Rischio: <b>{rischio}</b> | \U0001f525 <b>{a.get('confidence_live','?')}/100</b>\n"
        f"\U0001f4dd {a.get('motivazione_live','')}\n"
    )

def group_by_league(matches):
    leagues = {}
    for m in matches:
        country = m['league']['country']
        flag = get_flag(country)
        key = f"{flag} {country} \u2014 {m['league']['name']}"
        if key not in leagues:
            leagues[key] = []
        leagues[key].append(m)
    return leagues

def show_stats():
    try:
        total = predictions_col.count_documents({})
        wins = predictions_col.count_documents({"result": "win"})
        losses = predictions_col.count_documents({"result": "loss"})
        pending = predictions_col.count_documents({"result": "pending"})
        pct = round((wins / (total - pending)) * 100, 1) if (total - pending) > 0 else 0
        send_telegram(
            f"\U0001f4ca <b>Statistiche CeccoBet</b>\n\n"
            f"\U0001f4cb Totali: {total}\n"
            f"\u2705 Vinte: {wins}\n"
            f"\u274c Perse: {losses}\n"
            f"\u23f3 In attesa: {pending}\n"
            f"\U0001f3af Precisione: {pct}%\n"
        )
    except:
        send_telegram("\u26a0\ufe0f Errore nel recupero statistiche.")

def top_job():
    today = datetime.now().strftime("%Y-%m-%d")
    top = list(predictions_col.find(
        {"date": today, "confidence": {"$gte": CONFIDENCE_MIN}},
        {"_id": 0}
    ).sort("confidence", -1).limit(10))
    if not top:
        send_telegram(f"\u26a0\ufe0f Nessuna value bet con confidence >= {CONFIDENCE_MIN} oggi.")
        return
    msg = "\u2b50 <b>TOP VALUE BETS DI OGGI</b>\n\n"
    for h in top:
        msg += f"\u26bd <b>{h['match']}</b>\n"
        msg += f"\U0001f4a1 {h['value_bet']} | \U0001f525 {h['confidence']}/100\n\n"
    send_telegram(msg)

def multipla_job():
    today = datetime.now().strftime("%Y-%m-%d")
    top = list(predictions_col.find(
        {"date": today, "confidence": {"$gte": CONFIDENCE_MIN}},
        {"_id": 0}
    ).sort("confidence", -1).limit(2))
    if not top:
        send_telegram(f"\u26a0\ufe0f Nessuna value bet con confidence >= {CONFIDENCE_MIN}.")
        return
    quota_combined = 1.0
    for s in top:
        try:
            q = float(str(s.get('quota', 1.0)).replace(',', '.'))
            if q > 1:
                quota_combined *= q
        except:
            pass
    quota_combined = round(quota_combined, 2)
    msg = "\U0001f3af <b>MULTIPLA DEL GIORNO</b>\n\n"
    for i, s in enumerate(top, 1):
        msg += f"{i}. \u26bd <b>{s['match']}</b>\n   \U0001f4a1 {s['value_bet']} | \U0001f525 {s['confidence']}/100\n\n"
    msg += f"\U0001f4b0 <b>Quota combinata: {quota_combined}</b>\n"
    send_telegram(msg)

def risultato_job():
    today = datetime.now().strftime("%Y-%m-%d")
    pending = list(predictions_col.find({"result": "pending", "date": today}, {"_id": 0}))
    if not pending:
        send_telegram("\u26a0\ufe0f Nessuna previsione in attesa per oggi.")
        return
    msg = "\U0001f4cb <b>Previsioni in attesa:</b>\n\n"
    for i, h in enumerate(pending):
        msg += f"{i+1}. <b>{h['match']}</b>\n   \U0001f4a1 {h['value_bet']} | \U0001f525 {h['confidence']}/100\n\n"
    send_telegram(msg)

def get_fixture_result(fixture_id):
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"id": fixture_id})
    fixtures = data.get("response", [])
    if not fixtures:
        return None, None
    m = fixtures[0]
    if m["fixture"]["status"]["short"] not in ["FT", "AET", "PEN"]:
        return None, None
    hg = m["goals"]["home"]
    ag = m["goals"]["away"]
    result = "1" if hg > ag else ("X" if hg == ag else "2")
    return result, f"{hg}-{ag}"

def check_and_report_results():
    today = datetime.now().strftime("%Y-%m-%d")
    today_bets = list(predictions_col.find({"date": today, "result": "pending"}, {"_id": 0}))
    if not today_bets:
        send_telegram("\u26a0\ufe0f Nessuna previsione da verificare oggi.")
        return
    updated = 0
    lines_out = ["\U0001f4ca <b>Riepilogo previsioni di oggi:</b>\n"]
    for h in today_bets:
        fixture_id = h.get("fixture_id")
        if not fixture_id:
            continue
        actual_result, score = get_fixture_result(fixture_id)
        if not actual_result:
            continue
        predicted = h.get("value_bet", "")
        correct = actual_result == predicted
        esito = "win" if correct else "loss"
        update_prediction(h["match"], today, esito, actual_result, score)
        updated += 1
        emoji = "\u2705" if correct else "\u274c"
        lines_out.append(f"{emoji} <b>{h['match']}</b>\n   Prev: {predicted} | Risultato: {actual_result} ({score})\n")
    if updated == 0:
        send_telegram("\u23f3 Partite ancora in corso.")
        return
    wins = sum(1 for h in today_bets if h.get("result") == "win")
    total = updated
    pct = round((wins / total) * 100, 1) if total > 0 else 0
    lines_out.insert(1, f"\u2705 {wins}/{total} | \U0001f3af {pct}%\n")
    send_telegram("\n".join(lines_out[:15]))

def value_alert_job():
    data = load_all_odds()
    alerts = []
    for event in data:
        if not isinstance(event, dict):
            continue
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        for bk in event.get("bookmakers", [])[:2]:
            for market in bk.get("markets", []):
                if market.get("key") == "h2h":
                    for outcome in market.get("outcomes", []):
                        quota = outcome.get("price", 0)
                        if quota < 1.3 or quota > 5:
                            continue
                        prob_reale = (1 / quota) * 1.05
                        ev = round((prob_reale * (quota - 1) - (1 - prob_reale)) * 100, 1)
                        if ev > 5:
                            alerts.append({"match": f"{home} vs {away}", "outcome": outcome.get("name",""),
                                          "quota": quota, "ev": ev, "bookmaker": bk.get("title","")})
    if not alerts:
        return
    alerts.sort(key=lambda x: x["ev"], reverse=True)
    msg = "\U0001f6a8 <b>VALUE ALERT!</b>\n\n"
    for a in alerts[:5]:
        msg += f"\u26bd <b>{a['match']}</b>\n  \U0001f4a1 {a['outcome']} @ {a['quota']} | EV: +{a['ev']}% ({a['bookmaker']})\n\n"
    send_telegram(msg)

def watchdog():
    errors = []
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/getMe"
        r = requests.get(url, timeout=10)
        if not r.json().get("ok"):
            errors.append("Telegram API non risponde")
    except:
        errors.append("Telegram non raggiungibile")
    try:
        url = "https://v3.football.api-sports.io/status"
        headers = {"x-apisports-key": APIFOOTBALL}
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        remaining = data.get("response", {}).get("requests", {}).get("current", 0)
        limit = data.get("response", {}).get("requests", {}).get("limit_day", 100)
        if remaining >= limit * 0.9:
            errors.append(f"API-Football: quasi esaurite ({remaining}/{limit})")
    except:
        errors.append("API-Football non raggiungibile")
    if errors:
        msg = "\u26a0\ufe0f <b>Watchdog Alert:</b>\n\n"
        for e in errors:
            msg += e + "\n"
        send_telegram_admin(msg)
    else:
        print(f"[Watchdog] OK - {datetime.now().strftime('%H:%M')}")

def analyze_match(m):
    home_id    = m['teams']['home']['id']
    away_id    = m['teams']['away']['id']
    home_name  = m['teams']['home']['name']
    away_name  = m['teams']['away']['name']
    league_id  = m['league']['id']
    season     = m['league']['season']
    fixture_id = m['fixture']['id']
    sh        = get_team_stats(home_id, league_id, season)
    sa        = get_team_stats(away_id, league_id, season)
    ih        = get_injuries(home_id, fixture_id)
    ia        = get_injuries(away_id, fixture_id)
    odds      = get_odds(home_name, away_name)
    h2h       = get_h2h(home_id, away_id)
    form_h    = get_recent_form(home_id, league_id, season)
    form_a    = get_recent_form(away_id, league_id, season)
    standings = get_standings(league_id, season)
    best_odds = confronto_quote(home_name, away_name)
    news_h    = get_news_sentiment(home_name)
    news_a    = get_news_sentiment(away_name)
    analysis  = analyze_with_claude(m, sh, sa, ih, ia, odds, h2h, form_h, form_a, standings, news_h, news_a)
    return format_match_block(m, analysis, best_odds), analysis

def analyze_single_live(m):
    fixture_id = m['fixture']['id']
    odds   = get_odds(m['teams']['home']['name'], m['teams']['away']['name'])
    stats  = get_live_stats(fixture_id)
    events = get_live_events(fixture_id)
    analysis = analyze_live_with_claude(m, odds, stats, events)
    return format_live_block(m, analysis)

def run_analysis(matches, label="oggi"):
    global stop_analysis
    stop_analysis = False
    leagues = group_by_league(matches)
    send_telegram(f"\U0001f4c5 Trovate <b>{len(matches)}</b> partite in <b>{len(leagues)}</b> campionati...")
    send_telegram("\U0001f4ca Caricamento quote...")
    load_all_odds()

    for league_name, league_matches in leagues.items():
        if stop_analysis:
            send_telegram("\U0001f6d1 Analisi fermata.")
            return

        # Filtra solo partite con quote
        league_matches = [m for m in league_matches if get_odds(m['teams']['home']['name'], m['teams']['away']['name'])]
        if not league_matches:
            continue

        league_blocks = []

        def analyze_single_prematch(m):
            home_name = m['teams']['home']['name']
            away_name = m['teams']['away']['name']
            print(f"Analisi: {home_name} vs {away_name}...")
            block, analysis = analyze_match(m)
            a = parse_json_safe(analysis)
            confidence = int(a.get('confidence', 0))
            try:
                quota_num = float(str(a.get('quota_consigliata', 1.0)).replace(',', '.'))
            except:
                quota_num = 1.0
            vb = a.get('value_bet', '')
            prob_map = {"1": a.get('prob_home',0), "X": a.get('prob_draw',0), "2": a.get('prob_away',0)}
            ev, _ = calcola_ev_reale(prob_map.get(vb, 0), quota_num)
            pred = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "match": f"{home_name} vs {away_name}",
                "fixture_id": m["fixture"]["id"],
                "value_bet": vb,
                "quota": quota_num,
                "ev_reale": ev,
                "risultato_esatto": a.get('risultato_esatto', ''),
                "confidence": confidence,
                "result": "pending"
            }
            return block, pred, ev

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(analyze_single_prematch, m): m for m in league_matches}
            for future in as_completed(futures):
                if stop_analysis:
                    send_telegram("\U0001f6d1 Analisi fermata.")
                    return
                try:
                    block, pred, ev = future.result()
                    calibrazione = get_calibrazione_confidence()
                    pred["confidence"] = max(0, min(100, round(pred["confidence"] * calibrazione)))
                    print(f"OK: {pred['match']} | conf:{pred['confidence']} | ev:{ev}")
                    if ev is not None and ev < 3:
                        add_prediction(pred)
                        continue
                    add_prediction(pred)
                    league_blocks.append(block)
                except Exception as e:
                    m = futures[future]
                    print(f"Errore: {m['teams']['home']['name']} vs {m['teams']['away']['name']}: {e}")

        if league_blocks:
            msg = f"\U0001f3c6 <b>{league_name}</b>\n\n" + "\n".join(league_blocks)
            if len(msg) > 4000:
                msg = msg[:4000] + "\n..."
            send_telegram(msg)
            time.sleep(2)

    send_telegram(f"\u2705 <b>Analisi {label} completata!</b>")

def daily_job():
    send_telegram("\U0001f50d Avvio analisi partite di oggi...")
    matches = get_matches()
    if not matches:
        send_telegram("\u26a0\ufe0f Nessuna partita trovata oggi.")
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
    matches = get_live_matches()
    if not matches:
        send_telegram_live("\u26bd Nessuna partita live con valore (30'-80').")
        return
    send_telegram_live(f"\U0001f534 <b>{len(matches)} partite live \u2014 analisi in corso...</b>")
    results_by_league = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(analyze_single_live, m): m for m in matches}
        for future in as_completed(futures):
            if stop_live:
                send_telegram_live("\U0001f6d1 Analisi live fermata!")
                return
            try:
                m = futures[future]
                block = future.result()
                country = m['league']['country']
                flag = get_flag(country)
                key = f"{flag} {country} \u2014 {m['league']['name']}"
                if key not in results_by_league:
                    results_by_league[key] = []
                results_by_league[key].append(block)
            except Exception as e:
                print(f"Errore live: {e}")
    for league_name, blocks in results_by_league.items():
        if stop_live:
            send_telegram_live("\U0001f6d1 Analisi live fermata!")
            return
        msg = f"\U0001f534 <b>{league_name}</b>\n\n" + "\n".join(blocks)
        if len(msg) > 4000:
            msg = msg[:4000] + "\n..."
        send_telegram_live(msg)
        time.sleep(2)
    send_telegram_live("\u2705 <b>Analisi live completata!</b>")

def cerca_job(team_name):
    send_telegram(f"\U0001f50d Cerco partite di <b>{team_name}</b>...")
    matches = search_team_matches(team_name)
    if not matches:
        send_telegram(f"\u26a0\ufe0f Nessuna partita trovata per <b>{team_name}</b>.")
        return
    msg = f"\U0001f4cb <b>Partite trovate per {team_name}:</b>\n\n"
    for m in matches:
        kick_utc = datetime.fromisoformat(m['fixture']['date'].replace('Z', '+00:00'))
        kick_it = kick_utc.astimezone(timezone(timedelta(hours=2)))
        msg += f"\u26bd <b>{m['teams']['home']['name']} vs {m['teams']['away']['name']}</b>\n"
        msg += f"\U0001f3c6 {m['league']['name']} | \U0001f550 {kick_it.strftime('%d/%m %H:%M')}\n\n"
    send_telegram(msg)

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
                if user_id not in ADMIN_IDS:
                    continue
                if text_lower == "/start":
                    send_telegram_admin("\U0001f916 <b>CeccoBet Bot attivo!</b>\nUsa /help per vedere i comandi.")
                elif text_lower == "/analisi":
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
                elif text_lower == "/multipla":
                    multipla_job()
                elif text_lower == "/alert":
                    threading.Thread(target=value_alert_job).start()
                elif text_lower == "/riepilogo":
                    threading.Thread(target=check_and_report_results).start()
                elif text_lower == "/risultato":
                    risultato_job()
                elif text_lower.startswith("/cerca "):
                    team = text[7:].strip()
                    if team:
                        threading.Thread(target=cerca_job, args=(team,)).start()
                    else:
                        send_telegram("\u26a0\ufe0f Uso: /cerca Juventus")
                elif text_lower == "/stats":
                    show_stats()
                elif text_lower == "/help":
                    send_telegram(
                        "\U0001f916 <b>Comandi CeccoBet:</b>\n\n"
                        "<b>Analisi:</b>\n"
                        "/analisi \u2014 Partite di oggi\n"
                        "/domani \u2014 Partite di domani\n"
                        "/live \u2014 Giocate live (30'-80')\n\n"
                        "<b>Scommesse:</b>\n"
                        "/top \u2014 Top value bet oggi\n"
                        "/multipla \u2014 Multipla del giorno\n"
                        "/alert \u2014 Value alert adesso\n\n"
                        "<b>Statistiche:</b>\n"
                        "/riepilogo \u2014 Risultati di oggi\n"
                        "/stats \u2014 Statistiche storiche\n\n"
                        "<b>Utilita:</b>\n"
                        "/cerca [squadra] \u2014 Cerca squadra\n"
                        "/stop \u2014 Ferma analisi\n"
                        "/stoplive \u2014 Ferma live\n"
                        "/help \u2014 Questo messaggio\n"
                    )
        except Exception as e:
            print(f"Errore listener: {e}")
        time.sleep(2)

if __name__ == "__main__":
    print("Bot avviato!")
    send_telegram_admin(
        "\U0001f916 <b>CeccoBet \u2014 Sistema analisi scommesse</b>\n"
        "\u2500" * 25 + "\n"
        "\u2705 Bot attivo e operativo\n\n"
        "\U0001f4cb <b>Comandi principali:</b>\n"
        "/analisi \u2014 Partite di oggi\n"
        "/domani \u2014 Partite di domani\n"
        "/live \u2014 Giocate live\n"
        "/top \u2014 Top value bet\n"
        "/multipla \u2014 Multipla del giorno\n\n"
        "\U0001f4cb <b>Altri comandi:</b>\n"
        "/cerca [squadra] \u2014 Cerca squadra\n"
        "/alert \u2014 Value alert\n"
        "/riepilogo \u2014 Risultati oggi\n"
        "/stats \u2014 Statistiche\n"
        "/help \u2014 Lista completa\n"
        "\u2500" * 25
    )
    t = threading.Thread(target=listen_commands, daemon=True)
    t.start()
    schedule.every().day.at("10:00").do(daily_job)
    schedule.every().day.at("23:00").do(check_and_report_results)
    schedule.every(30).minutes.do(value_alert_job)
    schedule.every(6).hours.do(watchdog)
    while True:
        schedule.run_pending()
        time.sleep(60)
