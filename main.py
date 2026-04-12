import os, requests, anthropic, time, json, threading, schedule
from pymongo import MongoClient
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

mongo_client = MongoClient(os.getenv("MONGODB_URI"))
db           = mongo_client["ceccobet"]
predictions_col = db["predictions"]

last_update_id = 0
stop_analysis  = False
stop_live      = False
ADMIN_IDS      = [8317266009, 2129248376]

ALLOWED_LEAGUES = [
    ("Italy", "Serie A"), ("Italy", "Serie B"), ("Italy", "Coppa Italia"),
    ("England", "Premier League"), ("England", "Championship"), ("England", "FA Cup"),
    ("France", "Ligue 1"), ("France", "Ligue 2"),
    ("Spain", "La Liga"), ("Spain", "Copa del Rey"),
    ("Germany", "Bundesliga"), ("Germany", "2. Bundesliga"),
    ("Portugal", "Primeira Liga"),
    ("Netherlands", "Eredivisie"),
    ("Belgium", "Pro League"),
    ("Argentina", "Liga Profesional"),
    ("Brazil", "Serie A"),
    ("Turkey", "Super Lig"),
    ("USA", "MLS"),
    ("World", "UEFA Champions League"),
    ("World", "UEFA Europa League"),
    ("World", "UEFA Europa Conference League"),
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
            time.sleep(3 * (attempt + 1))
    return {}

def add_prediction(pred):
    try:
        pred.pop('_id', None)
        predictions_col.insert_one(pred)
    except Exception as e:
        print(f"Errore MongoDB: {e}")

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

# ── Quote: UNA sola chiamata al giorno ───────────────────────
_odds_cache = []
_odds_cache_time = 0

def load_all_odds():
    global _odds_cache, _odds_cache_time
    now = time.time()
    if now - _odds_cache_time < 43200:  # Cache 12 ore
        return _odds_cache
    all_events = []
    # Lista campionati principali — una chiamata per ognuno
    sport_keys = [
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
    for sk in sport_keys:
        try:
            url = f"https://api.the-odds-api.com/v4/sports/{sk}/odds/"
            params = {"apiKey": ODDS_KEY, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal"}
            r = requests.get(url, params=params, timeout=10)
            data = r.json()
            if isinstance(data, list):
                all_events.extend(data)
            elif data.get("error_code") == "OUT_OF_USAGE_CREDITS":
                print("Odds: crediti esauriti!")
                break
            time.sleep(0.3)
        except Exception as e:
            print(f"Odds error {sk}: {e}")
    _odds_cache = all_events
    _odds_cache_time = now
    print(f"Quote caricate: {len(all_events)} eventi")
    return all_events

def get_odds(home, away):
    data = load_all_odds()
    h = home.lower().strip()
    a = away.lower().strip()
    for event in data:
        ev_home = event.get("home_team", "").lower()
        ev_away = event.get("away_team", "").lower()
        if (h in ev_home or ev_home in h or h.split()[0] in ev_home) and \
           (a in ev_away or ev_away in a or a.split()[0] in ev_away):
            # Restituisce le migliori quote h2h
            best = {"1": 0, "X": 0, "2": 0}
            for bk in event.get("bookmakers", []):
                for market in bk.get("markets", []):
                    if market.get("key") == "h2h":
                        for o in market.get("outcomes", []):
                            price = o.get("price", 0)
                            name = o.get("name", "").lower()
                            if name == ev_home and price > best["1"]:
                                best["1"] = price
                            elif name == "draw" and price > best["X"]:
                                best["X"] = price
                            elif name == ev_away and price > best["2"]:
                                best["2"] = price
            return best
    return {}

# ── Dati partite ─────────────────────────────────────────────
def get_matches(date=None):
    italy_time = datetime.now(timezone.utc) + timedelta(hours=2)
    if not date:
        date = italy_time.strftime("%Y-%m-%d")
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"date": date})
    print(f"Partite {date}: {data.get('results', 0)} trovate")
    return [m for m in data.get("response", [])
            if is_allowed(m) and m["fixture"]["status"]["short"] in ["NS", "TBD"]]

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

def get_recent_form(team_id, league_id, season):
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": APIFOOTBALL}
    data = api_get(url, headers, {"team": team_id, "league": league_id,
                                   "season": season, "last": 5, "status": "FT"})
    form = []
    for m in data.get("response", []):
        try:
            home_id = m['teams']['home']['id']
            hg = m['goals']['home'] or 0
            ag = m['goals']['away'] or 0
            r = "V" if (team_id == home_id and hg > ag) or (team_id != home_id and ag > hg) \
                else ("P" if hg == ag else "S")
            form.append(f"{m['teams']['home']['name']} {hg}-{ag} {m['teams']['away']['name']} ({r})")
        except:
            continue
    return form

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
    return ("1" if hg > ag else ("X" if hg == ag else "2")), f"{hg}-{ag}"

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

# ── Analisi Claude ───────────────────────────────────────────
def parse_json(text):
    try:
        clean = text.strip().replace('```json','').replace('```','').strip()
        if clean.startswith('json'):
            clean = clean[4:].strip()
        start = clean.find('{')
        end = clean.rfind('}')
        if start != -1 and end != -1:
            clean = clean[start:end+1]
        return json.loads(clean)
    except:
        return {}

def analyze_prematch(m, form_h, form_a, odds):
    home = m['teams']['home']['name']
    away = m['teams']['away']['name']
    league = m['league']['name'].lower()
    round_info = m['league'].get('round','').lower()
    if any(k in league for k in ['champions','europa','conference','cup','coppa','pokal']):
        if any(k in round_info for k in ['final','semi','quarter','quarti']):
            importanza = "FINALE/SEMIFINALE - tattica conservativa, meno gol"
        else:
            importanza = "COPPA - possibili rotazioni"
    else:
        importanza = "CAMPIONATO REGOLARE"

    prompt = f"""Analista scommesse esperto. Analizza {home} vs {away}.
IMPORTANZA: {importanza}
FORMA CASA (ultime 5): {form_h}
FORMA OSPITE (ultime 5): {form_a}
QUOTE 1X2: {odds}
Individua LA SINGOLA MIGLIORE value bet considerando:
1X2 (1/X/2), Doppia chance (1X/X2/12), Over/Under (Over o Under 2.5 o 1.5), GG/NG
Scegli quella con piu valore reale rispetto alle quote di mercato.
Stima una quota realistica se non disponibile.
Rispondi SOLO con JSON valido senza backtick:
{{"giocata":"es. 1 o X2 o Over 2.5 o GG","quota":X,"motivazione":"massimo 1 riga concisa","confidence":X}}"""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text

def analyze_live(m, odds):
    home = m['teams']['home']['name']
    away = m['teams']['away']['name']
    score = m['goals']
    minute = m['fixture']['status'].get('elapsed', '?')
    try:
        elapsed_int = int(str(minute).replace('+','').strip())
    except:
        elapsed_int = 45

    if elapsed_int <= 45:
        focus = "Giocate su risultato finale o Over/Under totale"
    elif elapsed_int <= 60:
        focus = "Analizza momentum. Giocate su risultato o GG/NG"
    else:
        focus = "Oltre 60'. Preferisci Over/Under 2T o prossimo gol"

    prompt = f"""Analista LIVE. {home} vs {away} - min {minute}' - {score.get('home',0)}-{score.get('away',0)}
QUOTE: {odds}
{focus}
Rispondi SOLO con JSON valido:
{{"giocata":"descrizione","quota":X,"motivazione":"max 2 righe","confidence":X,"rischio":"basso/medio/alto"}}"""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text

# ── Telegram ─────────────────────────────────────────────────
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=15)
            print(f"Telegram: {r.status_code}")
            return
        except Exception as e:
            print(f"Errore Telegram: {e}")
            time.sleep(3)

def send_telegram_live(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, json={"chat_id": str(TG_CHAT_LIVE), "text": text, "parse_mode": "HTML"}, timeout=15)
            print(f"Telegram Live: {r.status_code}")
            return
        except Exception as e:
            print(f"Errore Telegram Live: {e}")
            time.sleep(3)

def send_telegram_admin(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, json={"chat_id": str(ADMIN_IDS[0]), "text": text, "parse_mode": "HTML"}, timeout=15)
            print(f"Telegram Admin: {r.status_code}")
            return
        except Exception as e:
            print(f"Errore Admin: {e}")
            time.sleep(3)

# ── Formattazione ─────────────────────────────────────────────
def format_prematch(m, a_raw, odds):
    a = parse_json(a_raw)
    home = m['teams']['home']['name']
    away = m['teams']['away']['name']
    kick_utc = datetime.fromisoformat(m['fixture']['date'].replace('Z', '+00:00'))
    kick_it = kick_utc.astimezone(timezone(timedelta(hours=2)))
    kickoff = kick_it.strftime("%H:%M")
    conf = int(a.get('confidence', 0))
    star = "\u2b50 <b>TOP VALUE BET</b>\n" if conf >= 65 else ""
    odds_str = ""
    if odds:
        odds_str = f"\U0001f4b0 1:{odds.get('1','?')} X:{odds.get('X','?')} 2:{odds.get('2','?')}\n"
    return (
        f"\u26bd <b>{home} vs {away}</b> \u2014 {kickoff}\n"
        f"{star}"
        f"\U0001f4a1 <b>{a.get('giocata','?')}</b> @ {a.get('quota','?')}\n"
        f"{odds_str}"
        f"\U0001f525 {conf}/100 | {a.get('motivazione','')}\n"
    )

def format_live(m, a_raw):
    a = parse_json(a_raw)
    home = m['teams']['home']['name']
    away = m['teams']['away']['name']
    score = m['goals']
    minute = m['fixture']['status'].get('elapsed', '?')
    rischio = a.get('rischio', 'N/D')
    rischio_emoji = "\U0001f7e2" if rischio == "basso" else ("\U0001f7e1" if rischio == "medio" else "\U0001f534")
    return (
        f"\U0001f534 <b>{home} vs {away}</b> {minute}' | {score.get('home',0)}-{score.get('away',0)}\n"
        f"\U0001f4a1 <b>{a.get('giocata','N/A')}</b> @ {a.get('quota','?')}\n"
        f"{rischio_emoji} {rischio} | \U0001f525 {a.get('confidence','?')}/100 | {a.get('motivazione','')}\n"
    )

def group_by_league(matches):
    leagues = {}
    for m in matches:
        key = f"{m['league']['country']} \u2014 {m['league']['name']}"
        leagues.setdefault(key, []).append(m)
    return leagues

# ── Jobs ─────────────────────────────────────────────────────
def run_analysis(matches, label="oggi"):
    global stop_analysis
    stop_analysis = False
    leagues = group_by_league(matches)
    send_telegram(f"\U0001f4c5 <b>{len(matches)}</b> partite in <b>{len(leagues)}</b> campionati...")

    for league_name, league_matches in leagues.items():
        if stop_analysis:
            send_telegram("\U0001f6d1 Analisi fermata.")
            return
        blocks = []
        for m in league_matches:
            if stop_analysis:
                send_telegram("\U0001f6d1 Analisi fermata.")
                return
            home = m['teams']['home']['name']
            away = m['teams']['away']['name']
            print(f"Analisi: {home} vs {away}...")
            try:
                league_id = m['league']['id']
                season = m['league']['season']
                home_id = m['teams']['home']['id']
                away_id = m['teams']['away']['id']
                form_h = get_recent_form(home_id, league_id, season)
                form_a = get_recent_form(away_id, league_id, season)
                odds = get_odds(home, away)
                a_raw = analyze_prematch(m, form_h, form_a, odds)
                a = parse_json(a_raw)
                block = format_prematch(m, a_raw, odds)
                blocks.append(block)
                try:
                    quota_num = float(str(a.get('quota', 1.0)).replace(',', '.'))
                except:
                    quota_num = 1.0
                add_prediction({
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "match": f"{home} vs {away}",
                    "fixture_id": m["fixture"]["id"],
                    "value_bet": a.get('giocata', ''),
                    "quota": quota_num,
                    "confidence": int(a.get('confidence', 0)),
                    "result": "pending"
                })
            except Exception as e:
                print(f"Errore {home} vs {away}: {e}")
                blocks.append(f"\u26a0\ufe0f Errore: {home} vs {away}\n")
            time.sleep(2)

        if blocks:
            msg = f"\U0001f3c6 <b>{league_name}</b>\n\n" + "\n".join(blocks)
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
    tomorrow = (italy_time + timedelta(days=1)).strftime("%Y-%m-%d")
    send_telegram(f"\U0001f50d Analisi partite di domani...")
    matches = get_matches(tomorrow)
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

    def analyze_one_live(m):
        odds = get_odds(m['teams']['home']['name'], m['teams']['away']['name'])
        a_raw = analyze_live(m, odds)
        return format_live(m, a_raw)

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(analyze_one_live, m): m for m in matches}
        for future in as_completed(futures):
            if stop_live:
                send_telegram_live("\U0001f6d1 Analisi live fermata!")
                return
            try:
                m = futures[future]
                block = future.result()
                key = f"{m['league']['country']} \u2014 {m['league']['name']}"
                results_by_league.setdefault(key, []).append(block)
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

def check_and_report_results():
    today = datetime.now().strftime("%Y-%m-%d")
    today_bets = list(predictions_col.find({"date": today, "result": "pending"}, {"_id": 0}))
    if not today_bets:
        send_telegram("\u26a0\ufe0f Nessuna previsione da verificare oggi.")
        return
    updated = 0
    lines = ["\U0001f4ca <b>Riepilogo di oggi:</b>\n"]
    for h in today_bets:
        fixture_id = h.get("fixture_id")
        if not fixture_id:
            continue
        actual, score = get_fixture_result(fixture_id)
        if not actual:
            continue
        correct = actual == h.get("value_bet", "")
        update_prediction(h["match"], today, "win" if correct else "loss", actual, score)
        updated += 1
        emoji = "\u2705" if correct else "\u274c"
        lines.append(f"{emoji} <b>{h['match']}</b> | Prev:{h.get('value_bet','?')} Risultato:{actual} ({score})\n")
    if not updated:
        send_telegram("\u23f3 Partite ancora in corso.")
        return
    wins = sum(1 for l in lines if l.startswith("\u2705"))
    pct = round((wins / updated) * 100, 1)
    lines.insert(1, f"\u2705 {wins}/{updated} | \U0001f3af {pct}%\n")
    send_telegram("\n".join(lines[:15]))

def show_stats():
    try:
        total = predictions_col.count_documents({})
        if total == 0:
            send_telegram("\U0001f4ca Nessuna previsione ancora.")
            return
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
        send_telegram("\u26a0\ufe0f Errore statistiche.")

def top_job():
    today = datetime.now().strftime("%Y-%m-%d")
    top = list(predictions_col.find({"date": today, "confidence": {"$gte": 65}}, {"_id": 0}).sort("confidence", -1).limit(10))
    if not top:
        send_telegram("\u26a0\ufe0f Nessuna top value bet oggi.")
        return
    msg = "\u2b50 <b>TOP VALUE BETS DI OGGI</b>\n\n"
    for h in top:
        msg += f"\u26bd <b>{h['match']}</b>\n\U0001f4a1 {h['value_bet']} | \U0001f525 {h['confidence']}/100\n\n"
    send_telegram(msg)

def multipla_job():
    today = datetime.now().strftime("%Y-%m-%d")
    top = list(predictions_col.find({"date": today, "confidence": {"$gte": 65}}, {"_id": 0}).sort("confidence", -1).limit(2))
    if not top:
        send_telegram("\u26a0\ufe0f Nessuna value bet per la multipla.")
        return
    quota = round(float(top[0].get('quota',1)) * float(top[1].get('quota',1)) if len(top) > 1 else float(top[0].get('quota',1)), 2)
    msg = "\U0001f3af <b>MULTIPLA DEL GIORNO</b>\n\n"
    for i, h in enumerate(top, 1):
        msg += f"{i}. <b>{h['match']}</b> \u2014 {h['value_bet']} | \U0001f525 {h['confidence']}/100\n\n"
    msg += f"\U0001f4b0 Quota combinata: <b>{quota}</b>"
    send_telegram(msg)

def cerca_job(team_name):
    send_telegram(f"\U0001f50d Cerco partite di <b>{team_name}</b>...")
    matches = search_team_matches(team_name)
    if not matches:
        send_telegram(f"\u26a0\ufe0f Nessuna partita trovata per <b>{team_name}</b>.")
        return
    msg = f"\U0001f4cb <b>Partite {team_name}:</b>\n\n"
    for m in matches:
        kick_utc = datetime.fromisoformat(m['fixture']['date'].replace('Z', '+00:00'))
        kick_it = kick_utc.astimezone(timezone(timedelta(hours=2)))
        msg += f"\u26bd <b>{m['teams']['home']['name']} vs {m['teams']['away']['name']}</b>\n"
        msg += f"\U0001f3c6 {m['league']['name']} | \U0001f550 {kick_it.strftime('%d/%m %H:%M')}\n\n"
    send_telegram(msg)

def watchdog():
    errors = []
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getMe", timeout=10)
        if not r.json().get("ok"):
            errors.append("Telegram non risponde")
    except:
        errors.append("Telegram non raggiungibile")
    if errors:
        send_telegram_admin("\u26a0\ufe0f <b>Watchdog:</b>\n" + "\n".join(errors))
    else:
        print(f"[Watchdog] OK - {datetime.now().strftime('%H:%M')}")

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
                    send_telegram_admin("\U0001f916 <b>CeccoBet attivo!</b>\nUsa /help per i comandi.")
                elif text_lower == "/analisi":
                    threading.Thread(target=daily_job).start()
                elif text_lower == "/domani":
                    threading.Thread(target=domani_job).start()
                elif text_lower == "/live":
                    threading.Thread(target=live_job).start()
                elif text_lower == "/stop":
                    stop_analysis = True
                    stop_live = True
                    send_telegram("\U0001f6d1 Fermato!")
                elif text_lower == "/stoplive":
                    stop_live = True
                    send_telegram_live("\U0001f6d1 Live fermato!")
                elif text_lower == "/top":
                    top_job()
                elif text_lower == "/multipla":
                    multipla_job()
                elif text_lower == "/riepilogo":
                    threading.Thread(target=check_and_report_results).start()
                elif text_lower == "/stats":
                    show_stats()
                elif text_lower.startswith("/cerca "):
                    team = text[7:].strip()
                    if team:
                        threading.Thread(target=cerca_job, args=(team,)).start()
                elif text_lower == "/help":
                    send_telegram(
                        "\U0001f916 <b>Comandi CeccoBet:</b>\n\n"
                        "/analisi \u2014 Partite di oggi\n"
                        "/domani \u2014 Partite di domani\n"
                        "/live \u2014 Giocate live\n"
                        "/top \u2014 Top value bet oggi\n"
                        "/multipla \u2014 Multipla del giorno\n"
                        "/cerca [squadra] \u2014 Cerca squadra\n"
                        "/riepilogo \u2014 Risultati di oggi\n"
                        "/stats \u2014 Statistiche storiche\n"
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
        "\U0001f916 <b>CeccoBet attivo!</b>\n\n"
        "/analisi \u2014 Partite di oggi\n"
        "/domani \u2014 Partite di domani\n"
        "/live \u2014 Giocate live\n"
        "/top \u2014 Top value bet\n"
        "/multipla \u2014 Multipla\n"
        "/riepilogo \u2014 Risultati oggi\n"
        "/stats \u2014 Statistiche\n"
        "/help \u2014 Aiuto"
    )
    t = threading.Thread(target=listen_commands, daemon=True)
    t.start()
    schedule.every().day.at("10:00").do(daily_job)
    schedule.every().day.at("23:00").do(check_and_report_results)
    schedule.every(6).hours.do(watchdog)
    while True:
        schedule.run_pending()
        time.sleep(60)
