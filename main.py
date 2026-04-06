import os, requests, anthropic, time, json, threading, schedule
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()

APIFOOTBALL  = os.getenv("APIFOOTBALL_KEY")
ODDS_KEY     = os.getenv("ODDS_API_KEY")
TG_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TG_CHAT      = os.getenv("TELEGRAM_CHAT_ID")
TG_CHAT_LIVE = os.getenv("TELEGRAM_CHAT_LIVE") or os.getenv("TELEGRAM_CHAT_ID")
client       = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
print(f"Chat Live ID: {TG_CHAT_LIVE}")

last_update_id = 0
stop_analysis  = False
stop_live      = False
ADMIN_IDS      = [8317266009, 2129248376]

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

def api_get(url, headers, params, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            return r.json()
        except Exception as e:
            print(f"Errore API ({attempt+1}/{retries}): {e}")
            time.sleep(3)
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
        diff = abs(hg - ag)
        # Filtra: partite equilibrate (0-0, 1-0, 0-1, 1-1) o con molti gol
        total_goals = hg + ag
        has_value = diff <= 1 or total_goals >= 2
        if has_value:
            filtered.append(m)
    return filtered

def get_live_stats(fixture_id):
    """Prende statistiche live: possesso, tiri, angoli, cartellini"""
    url = "https://v3.football.api-sports.io/fixtures/statistics"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"fixture": fixture_id})
    stats = {}
    for team_stats in data.get("response", []):
        team_name = team_stats.get("team", {}).get("name", "")
        for s in team_stats.get("statistics", []):
            stat_type = s.get("type", "")
            val = s.get("value", 0) or 0
            if stat_type in ["Ball Possession", "Total Shots", "Shots on Goal",
                              "Corner Kicks", "Yellow Cards", "Red Cards"]:
                if team_name not in stats:
                    stats[team_name] = {}
                stats[team_name][stat_type] = val
    return stats

def get_live_events(fixture_id):
    """Prende gli eventi recenti della partita"""
    url = "https://v3.football.api-sports.io/fixtures/events"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"fixture": fixture_id})
    events = data.get("response", [])
    # Ultimi 5 eventi
    recent = []
    for e in events[-5:]:
        minute = e.get("time", {}).get("elapsed", "?")
        etype = e.get("type", "")
        detail = e.get("detail", "")
        team = e.get("team", {}).get("name", "")
        player = e.get("player", {}).get("name", "")
        recent.append(f"{minute}' {team} - {etype} {detail} ({player})")
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
                for s in standings[:20]]
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
                                   "season": season, "last": 5, "status": "FT"})
    form = []
    for m in data.get("response", []):
        home_id = m['teams']['home']['id']
        hg = m['goals']['home']
        ag = m['goals']['away']
        r = "V" if (team_id == home_id and hg > ag) or (team_id != home_id and ag > hg) else \
            ("P" if hg == ag else "S")
        form.append(f"{m['teams']['home']['name']} {hg}-{ag} {m['teams']['away']['name']} ({r})")
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
        items = root.findall(".//item")[:3]
        return [item.find("title").text[:100] for item in items if item.find("title") is not None]
    except:
        return []

SPORT_KEYS = [
    "soccer_italy_serie_a", "soccer_italy_serie_b",
    "soccer_england_epl", "soccer_efl_champ", "soccer_england_league1",
    "soccer_france_ligue_one", "soccer_france_ligue_two",
    "soccer_spain_la_liga", "soccer_spain_segunda_division",
    "soccer_germany_bundesliga", "soccer_germany_bundesliga2",
    "soccer_portugal_primeira_liga",
    "soccer_netherlands_eredivisie",
    "soccer_belgium_first_div",
    "soccer_turkey_super_lig",
    "soccer_brazil_campeonato", "soccer_brazil_campeonato_serie_b",
    "soccer_argentina_primera_division",
    "soccer_colombia_primera_a",
    "soccer_usa_mls",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
]

_odds_cache = []
_odds_cache_time = 0

def load_all_odds():
    global _odds_cache, _odds_cache_time
    now = time.time()
    if now - _odds_cache_time < 300:
        return _odds_cache
    all_events = []
    for sk in SPORT_KEYS:
        try:
            url = f"https://api.the-odds-api.com/v4/sports/{sk}/odds/"
            params = {"apiKey": ODDS_KEY, "regions": "eu,it", "markets": "h2h,totals", "oddsFormat": "decimal"}
            r = requests.get(url, params=params, timeout=10)
            data = r.json()
            if isinstance(data, list):
                all_events.extend(data)
        except:
            pass
    _odds_cache = all_events
    _odds_cache_time = now
    print(f"Quote caricate: {len(all_events)} eventi totali")
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

def analyze_with_claude(match_data, stats_home, stats_away, injuries_home,
                        injuries_away, odds, h2h, form_home, form_away, standings,
                        news_home=None, news_away=None):
    prompt = f"""
Sei un analista di scommesse sportive esperto. Analizza questa partita.

PARTITA: {match_data['teams']['home']['name']} vs {match_data['teams']['away']['name']}
DATA: {match_data['fixture']['date']}
STATISTICHE CASA: {stats_home}
STATISTICHE OSPITE: {stats_away}
FORMA RECENTE CASA: {form_home}
FORMA RECENTE OSPITE: {form_away}
CLASSIFICA: {standings[:10] if standings else 'non disponibile'}
H2H (ultimi 5): {h2h}
INFORTUNI CASA: {[i['player']['name'] for i in injuries_home]}
INFORTUNI OSPITI: {[i['player']['name'] for i in injuries_away]}
NOTIZIE RECENTI CASA: {news_home if news_home else 'nessuna'}
NOTIZIE RECENTI OSPITE: {news_away if news_away else 'nessuna'}
QUOTE REALI: {odds[:3] if odds else 'non disponibili'}

Usa le quote reali. Se non disponibili scrivi N/D.
Tieni conto delle notizie recenti.

Rispondi SOLO in JSON senza backtick:
prob_home, prob_draw, prob_away,
value_bet (1 X o 2), quota_consigliata,
over_under (Over 2.5 o Under 2.5), quota_over_under,
gol_no_gol (Gol o No Gol), quota_gol_no_gol,
risultato_esatto, confidence (0-100), motivazione (max 3 righe).
"""
    msg = client.messages.create(
        model="claude-opus-4-5", max_tokens=800,
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

    # Determina fase di gioco
    if int(str(minute).replace('+','')) <= 45:
        fase = "PRIMO TEMPO"
        focus = "Considera che manca ancora il secondo tempo. Preferisci giocate su risultato finale o Over/Under totale."
    elif int(str(minute).replace('+','')) <= 60:
        fase = "INIZIO SECONDO TEMPO"
        focus = "Analizza bene il momentum. Suggerisci giocate su risultato finale, GG/NG o Over/Under totale."
    else:
        fase = "SECONDO TEMPO AVANZATO"
        focus = "Siamo oltre il 60'. Preferisci giocate su Over/Under nel secondo tempo, prossimo gol, o risultato finale se c'e valore chiaro."

    # Calcola momentum da statistiche
    momentum = ""
    if stats:
        home_stats = stats.get(home, {})
        away_stats = stats.get(away, {})
        home_shots = home_stats.get("Total Shots", 0)
        away_shots = away_stats.get("Total Shots", 0)
        home_pos = home_stats.get("Ball Possession", "50%")
        away_pos = away_stats.get("Ball Possession", "50%")
        if home_shots > away_shots + 3:
            momentum = f"{home} domina con {home_shots} tiri vs {away_shots}"
        elif away_shots > home_shots + 3:
            momentum = f"{away} domina con {away_shots} tiri vs {home_shots}"
        else:
            momentum = f"Partita equilibrata — {home_shots} tiri {home} vs {away_shots} tiri {away}"
        momentum += f" | Possesso: {home} {home_pos} — {away} {away_pos}"

    prompt = f"""
Sei un analista esperto di scommesse sportive LIVE.

PARTITA: {home} vs {away}
FASE: {fase} — Minuto: {minute}'
PUNTEGGIO: {hg} - {ag}
EVENTI RECENTI: {events if events else 'nessuno'}
STATISTICHE LIVE: {stats if stats else 'non disponibili'}
MOMENTUM: {momentum if momentum else 'non disponibile'}
QUOTE LIVE: {odds[:3] if odds else 'non disponibili'}

{focus}

IMPORTANTE: Fornisci SEMPRE una giocata concreta basandoti su tutti i dati.
Se le quote non sono disponibili, stimale in modo realistico.
Non rispondere mai con tutti N/D.

Rispondi SOLO in JSON senza backtick:
giocata_consigliata (descrizione chiara es. 'Over 2.5 Totale', 'Under 0.5 2T', '1 Vittoria Casa', 'Prossimo gol: Casa'),
quota_live (reale o stima realistica),
momentum (una riga su chi sta dominando),
motivazione_live (2-3 righe che spiegano la giocata in base a statistiche e fase di gioco),
confidence_live (0-100),
rischio (basso/medio/alto).
"""
    msg = client.messages.create(
        model="claude-opus-4-5", max_tokens=700,
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
    """Formatta UN singolo match come blocco di testo (per messaggi raggruppati)"""
    try:
        clean = analysis.strip().strip('`').strip()
        if clean.startswith('json'):
            clean = clean[4:].strip()
        a = json.loads(clean)
    except:
        a = {}
    home = match['teams']['home']['name']
    away = match['teams']['away']['name']
    kick_utc = datetime.fromisoformat(match['fixture']['date'].replace('Z', '+00:00'))
    kick_it = kick_utc.astimezone(timezone(timedelta(hours=2)))
    kickoff = kick_it.strftime("%d/%m/%Y %H:%M")
    confidence = int(a.get('confidence', 0))
    star = "\u2b50 <b>TOP VALUE BET</b>\n" if confidence >= 60 else ""
    best_str = ""
    if best_odds:
        parts = []
        for esito, (q, bk) in best_odds.items():
            if q > 0:
                parts.append(f"{esito}: {q} ({bk})")
        if parts:
            best_str = "\U0001f4b0 " + " | ".join(parts) + "\n"
    return (
        f"{star}\u26bd <b>{home} vs {away}</b> \u2014 {kickoff}\n"
        f"\U0001f4ca 1:{a.get('prob_home','?')}% X:{a.get('prob_draw','?')}% 2:{a.get('prob_away','?')}%\n"
        f"\U0001f4a1 <b>{a.get('value_bet','?')}</b> @ {a.get('quota_consigliata','?')} | "
        f"O/U: {a.get('over_under','?')} @ {a.get('quota_over_under','?')} | "
        f"GG/NG: {a.get('gol_no_gol','?')} @ {a.get('quota_gol_no_gol','?')}\n"
        f"\U0001f3af Esatto: {a.get('risultato_esatto','?')} | \U0001f525 {confidence}/100\n"
        f"{best_str}"
        f"\U0001f4dd {a.get('motivazione','')}\n"
    )

def format_live_block(match, analysis):
    """Formatta UN singolo match live come blocco di testo"""
    try:
        clean = analysis.strip().strip('`').strip()
        if clean.startswith('json'):
            clean = clean[4:].strip()
        a = json.loads(clean)
    except:
        a = {}
    home = match['teams']['home']['name']
    away = match['teams']['away']['name']
    score = match['goals']
    minute = match['fixture']['status'].get('elapsed', '?')
    return (
        f"\U0001f534 <b>{home} vs {away}</b> \u23f1 {minute}' | {score['home']}-{score['away']}\n"
        f"\U0001f3b0 <b>{a.get('giocata_consigliata','N/A')}</b> @ {a.get('quota_live','?')}\n"
        f"\u26a0\ufe0f Rischio: {a.get('rischio','?')} | \U0001f525 {a.get('confidence_live','?')}/100\n"
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

def top_job():
    history = load_history()
    today = datetime.now().strftime("%Y-%m-%d")
    top = [h for h in history if h.get("date") == today and h.get("confidence", 0) >= 60]
    if not top:
        send_telegram("\u26a0\ufe0f Nessuna value bet con confidence >= 60 oggi.")
        return
    top.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    msg = "\u2b50 <b>TOP VALUE BETS DI OGGI</b>\n\n"
    for h in top[:10]:
        msg += f"\u26bd <b>{h['match']}</b>\n"
        msg += f"\U0001f4a1 {h['value_bet']} | \U0001f3af {h['risultato_esatto']} | \U0001f525 {h['confidence']}/100\n\n"
    send_telegram(msg)

def multipla_job():
    history = load_history()
    today = datetime.now().strftime("%Y-%m-%d")
    top = [h for h in history if h.get("date") == today and h.get("confidence", 0) >= 60]
    if not top:
        send_telegram("\u26a0\ufe0f Nessuna value bet con confidence >= 60 per la multipla.")
        return
    top.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    selezioni = top[:2]
    quota_combined = 1.0
    for s in selezioni:
        try:
            q = s.get('quota', 1.0)
            if q and str(q) not in ['N/D', '', 'None', '?']:
                quota_combined *= float(str(q).replace(',', '.'))
        except:
            pass
    quota_combined = round(quota_combined, 2)
    msg = "\U0001f3af <b>MULTIPLA DEL GIORNO</b>\n\n"
    for i, s in enumerate(selezioni, 1):
        msg += f"{i}. \u26bd <b>{s['match']}</b>\n"
        msg += f"   \U0001f4a1 {s['value_bet']} | \U0001f525 {s['confidence']}/100\n\n"
    msg += f"\U0001f4b0 <b>Quota combinata: {quota_combined}</b>\n"
    send_telegram(msg)

def risultato_job():
    history = load_history()
    today = datetime.now().strftime("%Y-%m-%d")
    pending = [h for h in history if h.get('result') == 'pending' and h.get('date') == today]
    if not pending:
        send_telegram("\u26a0\ufe0f Nessuna previsione in attesa per oggi.")
        return
    msg = "\U0001f4cb <b>Previsioni in attesa:</b>\n\n"
    for i, h in enumerate(pending):
        msg += f"{i+1}. <b>{h['match']}</b>\n"
        msg += f"   \U0001f4a1 {h['value_bet']} | \U0001f525 {h['confidence']}/100\n\n"
    msg += "Rispondi con:\n<code>/vinta 1</code> o <code>/persa 1</code>"
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
    send_telegram(f"{emoji} <b>{match_name}</b> \u2014 {'Vinta' if esito == 'win' else 'Persa'}")

def get_fixture_result(fixture_id):
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"id": fixture_id})
    fixtures = data.get("response", [])
    if not fixtures:
        return None, None
    m = fixtures[0]
    status = m["fixture"]["status"]["short"]
    if status not in ["FT", "AET", "PEN"]:
        return None, None
    hg = m["goals"]["home"]
    ag = m["goals"]["away"]
    result = "1" if hg > ag else ("X" if hg == ag else "2")
    return result, f"{hg}-{ag}"

def check_and_report_results():
    history = load_history()
    today = datetime.now().strftime("%Y-%m-%d")
    today_bets = [h for h in history if h.get("date") == today and h.get("result") == "pending"]
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
        h["result"] = "win" if correct else "loss"
        h["actual_result"] = actual_result
        h["score"] = score
        updated += 1
        emoji = "\u2705" if correct else "\u274c"
        esito = "presa" if correct else "sbagliata"
        lines_out.append(emoji + " <b>" + h["match"] + "</b>\n   Prev: " + predicted + " | Risultato: " + actual_result + " (" + str(score) + ") - " + esito + "\n")
    save_history(history)
    if updated == 0:
        send_telegram("\u23f3 Partite ancora in corso o risultati non disponibili.")
        return
    wins = sum(1 for h in today_bets if h.get("result") == "win")
    losses = sum(1 for h in today_bets if h.get("result") == "loss")
    total = wins + losses
    pct = round((wins / total) * 100, 1) if total > 0 else 0
    lines_out.insert(1, "\u2705 Vinte: " + str(wins) + " | \u274c Perse: " + str(losses) + " | \U0001f3af Precisione: " + str(pct) + "%\n")
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
                        prob_implicita = 1 / quota
                        prob_reale = prob_implicita * 1.05
                        ev = round((prob_reale * (quota - 1) - (1 - prob_reale)) * 100, 1)
                        if ev > 5:
                            alerts.append({
                                "match": f"{home} vs {away}",
                                "outcome": outcome.get("name", ""),
                                "quota": quota,
                                "ev": ev,
                                "bookmaker": bk.get("title", "")
                            })
    if not alerts:
        return
    alerts.sort(key=lambda x: x["ev"], reverse=True)
    msg = "\U0001f6a8 <b>VALUE ALERT!</b>\n\n"
    for a in alerts[:5]:
        msg += "\u26bd <b>" + a["match"] + "</b>\n"
        msg += "  \U0001f4a1 " + a["outcome"] + " @ " + str(a["quota"]) + "\n"
        msg += "  \U0001f4c8 EV: +" + str(a["ev"]) + "% (" + a["bookmaker"] + ")\n\n"
    send_telegram(msg)

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
    history = load_history()
    leagues = group_by_league(matches)
    send_telegram(f"\U0001f4c5 Trovate <b>{len(matches)}</b> partite in <b>{len(leagues)}</b> campionati...")

    for league_name, league_matches in leagues.items():
        if stop_analysis:
            send_telegram("\U0001f6d1 Analisi fermata.")
            return

        # Analizza tutte le partite del campionato
        league_blocks = []
        for m in league_matches:
            if stop_analysis:
                send_telegram("\U0001f6d1 Analisi fermata.")
                return
            home_name = m['teams']['home']['name']
            away_name = m['teams']['away']['name']
            print(f"Analisi: {home_name} vs {away_name}...")
            try:
                block, analysis = analyze_match(m)
                clean = analysis.strip().strip('`').strip()
                if clean.startswith('json'):
                    clean = clean[4:].strip()
                a = json.loads(clean)
                confidence = int(a.get('confidence', 0))
                try:
                    quota_num = float(str(a.get('quota_consigliata', 1.0)).replace(',', '.'))
                except:
                    quota_num = 1.0
                history.append({
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "match": f"{home_name} vs {away_name}",
                    "fixture_id": m["fixture"]["id"],
                    "value_bet": a.get('value_bet', ''),
                    "quota": quota_num,
                    "risultato_esatto": a.get('risultato_esatto', ''),
                    "confidence": confidence,
                    "result": "pending"
                })
                league_blocks.append(block)
            except Exception as e:
                print(f"Errore analisi {home_name} vs {away_name}: {e}")
                league_blocks.append(f"\u26a0\ufe0f Errore analisi: {home_name} vs {away_name}\n")

        # Manda UN messaggio per campionato
        if league_blocks:
            msg = f"\U0001f3c6 <b>{league_name}</b>\n\n" + "\n".join(league_blocks)
            # Telegram max 4096 chars
            if len(msg) > 4000:
                msg = msg[:4000] + "\n..."
            send_telegram(msg)
            time.sleep(2)

    save_history(history)
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
        send_telegram_live("\u26bd Nessuna partita live con almeno 30 minuti giocati.")
        return
    send_telegram_live(f"\U0001f534 <b>{len(matches)} partite live \u2014 analisi in corso...</b>")

    # Analizza in parallelo
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
                key = f"{m['league']['country']} \u2014 {m['league']['name']}"
                if key not in results_by_league:
                    results_by_league[key] = []
                results_by_league[key].append(block)
            except Exception as e:
                print(f"Errore live: {e}")

    # Manda un messaggio per campionato
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
        home = m['teams']['home']['name']
        away = m['teams']['away']['name']
        league = m['league']['name']
        kick_utc = datetime.fromisoformat(m['fixture']['date'].replace('Z', '+00:00'))
        kick_it = kick_utc.astimezone(timezone(timedelta(hours=2)))
        kickoff = kick_it.strftime("%d/%m/%Y %H:%M")
        msg += f"\u26bd <b>{home} vs {away}</b>\n\U0001f3c6 {league}\n\U0001f550 {kickoff}\n\n"
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
                elif text_lower == "/multipla":
                    multipla_job()
                elif text_lower == "/alert":
                    threading.Thread(target=value_alert_job).start()
                elif text_lower == "/riepilogo":
                    threading.Thread(target=check_and_report_results).start()
                elif text_lower == "/risultato":
                    risultato_job()
                elif text_lower.startswith("/vinta "):
                    try:
                        aggiorna_risultato(int(text.split(" ")[1]), "win")
                    except:
                        send_telegram("\u26a0\ufe0f Uso: /vinta 1")
                elif text_lower.startswith("/persa "):
                    try:
                        aggiorna_risultato(int(text.split(" ")[1]), "loss")
                    except:
                        send_telegram("\u26a0\ufe0f Uso: /persa 1")
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
                        "\U0001f916 <b>Comandi disponibili:</b>\n\n"
                        "/analisi \u2014 Partite di oggi\n"
                        "/domani \u2014 Partite di domani\n"
                        "/live \u2014 Giocate live (30'-80')\n"
                        "/top \u2014 Top value bet oggi\n"
                        "/multipla \u2014 Multipla del giorno\n"
                        "/cerca [squadra] \u2014 Cerca squadra\n"
                        "/alert \u2014 Value alert adesso\n"
                        "/riepilogo \u2014 Risultati di oggi\n"
                        "/risultato \u2014 Previsioni in attesa\n"
                        "/vinta [n] \u2014 Segna come vinta\n"
                        "/persa [n] \u2014 Segna come persa\n"
                        "/stop \u2014 Ferma tutto\n"
                        "/stoplive \u2014 Ferma solo live\n"
                        "/stats \u2014 Statistiche\n"
                        "/help \u2014 Questo messaggio\n"
                    )
        except Exception as e:
            print(f"Errore listener: {e}")
        time.sleep(2)

if __name__ == "__main__":
    print("Bot avviato!")
    send_telegram_admin(
        "\U0001f916 <b>CeccoBet Bot avviato!</b>\n\n"
        "/analisi \u2014 Partite di oggi\n"
        "/domani \u2014 Partite di domani\n"
        "/live \u2014 Giocate live\n"
        "/top \u2014 Top value bet\n"
        "/multipla \u2014 Multipla del giorno\n"
        "/cerca [squadra] \u2014 Cerca squadra\n"
        "/alert \u2014 Value alert adesso\n"
        "/riepilogo \u2014 Risultati di oggi\n"
        "/help \u2014 Aiuto"
    )
    t = threading.Thread(target=listen_commands, daemon=True)
    t.start()
    schedule.every().day.at("23:00").do(check_and_report_results)
    schedule.every(30).minutes.do(value_alert_job)
    while True:
        schedule.run_pending()
        time.sleep(60)
