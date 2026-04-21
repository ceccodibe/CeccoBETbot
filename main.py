import os, re, requests, anthropic, time, threading, schedule
from pymongo import MongoClient
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    ALLOWED_LEAGUES, EXCLUDE_KEYWORDS, SPORT_KEYS,
    CONFIDENCE_MIN, LIVE_MIN_MINUTE, LIVE_MAX_MINUTE,
    ODDS_CACHE_SECONDS, ADMIN_IDS,
)
from utils import (
    parse_json,
    send_telegram, send_telegram_live, send_telegram_admin,
    format_match_block, format_live_block, group_by_league,
)

load_dotenv()

APIFOOTBALL = os.getenv("APIFOOTBALL_KEY")
ODDS_KEY    = os.getenv("ODDS_API_KEY")
client      = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

mongo_client    = MongoClient(os.getenv("MONGODB_URI"))
db              = mongo_client["ceccobet"]
predictions_col = db["predictions"]

# Thread-safe stop signals
stop_analysis = threading.Event()
stop_live     = threading.Event()

# Thread-safe odds cache
_odds_cache      = []
_odds_cache_time = 0.0
_odds_lock       = threading.Lock()

# Cached system prompts — one-time cost, then served from Anthropic cache
_SYS_PREMATCH = [{
    "type": "text",
    "text": (
        "Sei un analista di scommesse calcistiche esperto. "
        "Rispondi SOLO con JSON valido, senza backtick o testo aggiuntivo."
    ),
    "cache_control": {"type": "ephemeral"},
}]
_SYS_LIVE = [{
    "type": "text",
    "text": (
        "Sei un analista live di scommesse calcistiche esperto. "
        "Rispondi SOLO con JSON valido, senza backtick o testo aggiuntivo."
    ),
    "cache_control": {"type": "ephemeral"},
}]


# ── Helpers ───────────────────────────────────────────────────

def is_allowed(m):
    league_name = m["league"]["name"].lower()
    home = m["teams"]["home"]["name"].lower()
    away = m["teams"]["away"]["name"].lower()
    for kw in EXCLUDE_KEYWORDS:
        if kw in league_name or kw in home or kw in away:
            return False
    return any(
        country.lower() in m["league"]["country"].lower() and
        league.lower() in m["league"]["name"].lower()
        for country, league in ALLOWED_LEAGUES
    )


def api_get(url, headers, params, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            code = e.response.status_code
            wait = 10 * (attempt + 1) if code == 429 else 3
            print(f"Errore API HTTP {code} ({attempt+1}/{retries})")
            time.sleep(wait)
        except Exception as e:
            print(f"Errore API ({attempt+1}/{retries}): {e}")
            time.sleep(3 * (attempt + 1))
    return {}


# ── MongoDB ───────────────────────────────────────────────────

def add_prediction(pred):
    """Upsert on fixture_id+date to avoid duplicates if /analisi is run twice."""
    try:
        pred.pop("_id", None)
        predictions_col.update_one(
            {"fixture_id": pred["fixture_id"], "date": pred["date"]},
            {"$setOnInsert": pred},
            upsert=True,
        )
    except Exception as e:
        print(f"Errore MongoDB: {e}")


def update_prediction(match_name, date, result, actual_result=None, score=None):
    try:
        update = {"result": result}
        if actual_result:
            update["actual_result"] = actual_result
        if score:
            update["score"] = score
        predictions_col.update_one(
            {"match": match_name, "date": date, "result": "pending"},
            {"$set": update},
        )
    except Exception as e:
        print(f"Errore MongoDB update: {e}")


# ── Bet verification ──────────────────────────────────────────

def verifica_giocata(giocata, hg, ag):
    g       = giocata.lower().strip()
    totale  = hg + ag
    entrambe = hg > 0 and ag > 0
    if g == "1":  return hg > ag
    if g == "x":  return hg == ag
    if g == "2":  return ag > hg
    if g == "1x": return hg >= ag
    if g == "x2": return ag >= hg
    if g == "12": return hg != ag
    if "over" in g:
        try:
            soglia = float("".join(c for c in g if c.isdigit() or c == "."))
            return totale > soglia
        except Exception:
            return None
    if "under" in g:
        try:
            soglia = float("".join(c for c in g if c.isdigit() or c == "."))
            return totale < soglia
        except Exception:
            return None
    if g in ["gg", "gol", "goal goal"]: return entrambe
    if g in ["ng", "no gol", "no goal"]: return not entrambe
    return None


# ── Odds API ──────────────────────────────────────────────────

_STRIP_WORDS = re.compile(
    r'\b(fc|ac|as|afc|cf|sc|rc|us|ss|ssd|asd|rcd|ca|cd|ud|real|atletico|'
    r'athletic|deportivo|sporting|calcio|city|united|town|county|rovers|'
    r'wanderers|hotspur|albion|wednesday|forest|palace|villa|ham)\b'
)

def _norm(name: str) -> str:
    n = re.sub(r"['\-.]", " ", name.lower())
    n = _STRIP_WORDS.sub("", n)
    return " ".join(n.split())


def _teams_match(api_name: str, odds_name: str) -> bool:
    a, b = _norm(api_name), _norm(odds_name)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    ta, tb = set(a.split()), set(b.split())
    common = ta & tb - {""}
    short  = min(len(ta), len(tb))
    return bool(common) and len(common) >= max(1, short - 1)


def load_all_odds():
    global _odds_cache, _odds_cache_time
    now = time.time()
    with _odds_lock:
        if now - _odds_cache_time < ODDS_CACHE_SECONDS:
            return _odds_cache
        all_events = []
        for sk in SPORT_KEYS:
            try:
                url    = f"https://api.the-odds-api.com/v4/sports/{sk}/odds/"
                params = {
                    "apiKey": ODDS_KEY, "regions": "eu",
                    "markets": "h2h,totals,btts", "oddsFormat": "decimal",
                }
                r    = requests.get(url, params=params, timeout=10)
                data = r.json()
                if isinstance(data, list):
                    all_events.extend(data)
                elif isinstance(data, dict):
                    code = data.get("error_code", "")
                    msg  = data.get("message", str(data))
                    print(f"Odds API errore [{sk}]: {code} — {msg}")
                    if code == "OUT_OF_USAGE_CREDITS":
                        break
                time.sleep(0.3)
            except Exception as e:
                print(f"Odds error {sk}: {e}")
        _odds_cache      = all_events
        _odds_cache_time = now
        print(f"Quote caricate: {len(all_events)} eventi")
        return _odds_cache


def get_odds(home, away):
    """Returns best odds across bookmakers: 1X2, Over/Under, GG/NG."""
    data = load_all_odds()
    print(f"[Odds] Cache contiene {len(data)} eventi. Cerco: '{home}' vs '{away}'")
    for event in data:
        ev_home = event.get("home_team", "")
        ev_away = event.get("away_team", "")
        if not (_teams_match(home, ev_home) and _teams_match(away, ev_away)):
            print(f"[Odds] No match: evento='{ev_home}' vs '{ev_away}'")
            continue
        best = {"1": 0, "X": 0, "2": 0, "GG": 0, "NG": 0}
        ev_home_l = ev_home.lower()
        ev_away_l = ev_away.lower()
        for bk in event.get("bookmakers", []):
            for market in bk.get("markets", []):
                mkey = market.get("key")
                for o in market.get("outcomes", []):
                    price  = o.get("price", 0)
                    name_l = o.get("name", "").lower()
                    point  = o.get("point", "")
                    if mkey == "h2h":
                        if name_l == ev_home_l and price > best["1"]:
                            best["1"] = price
                        elif name_l == "draw" and price > best["X"]:
                            best["X"] = price
                        elif name_l == ev_away_l and price > best["2"]:
                            best["2"] = price
                    elif mkey == "totals":
                        k = f"Over {point}" if name_l == "over" else f"Under {point}"
                        if price > best.get(k, 0):
                            best[k] = price
                    elif mkey == "btts":
                        if name_l == "yes" and price > best["GG"]:
                            best["GG"] = price
                        elif name_l == "no" and price > best["NG"]:
                            best["NG"] = price
        return best
    print(f"[Odds] nessun evento trovato per: {home} vs {away}")
    return {}


def get_real_quota(giocata, odds):
    """Returns the real bookmaker quote for the suggested bet, 0 if not found."""
    if not odds or not giocata:
        return 0
    g = giocata.strip()
    if g in odds and odds[g]:
        return odds[g]
    g_l = g.lower()
    for k, v in odds.items():
        if k.lower() == g_l and v:
            return v
    # partial match for "Over 2.5" variants
    for k, v in odds.items():
        if g_l in k.lower() and v:
            return v
    return 0


def calibrate_confidence(raw_conf, giocata):
    """Adjusts model confidence using historical hit rates from 425 resolved bets."""
    g = (giocata or "").lower().strip()
    # Historical accuracy by bet type
    if g == "1":                hist = 54.8
    elif g == "2":              hist = 31.8
    elif g == "x":              hist = 31.1
    elif g in ("x2","1x","12"): hist = 27.6
    else:                       hist = 35.0  # Over/Under/GG/NG: no reliable data yet

    adj = raw_conf
    # 60-69 band is anomalously poor (23.8%), worse than <50 (26.6%) — pull it down
    if 60 <= raw_conf <= 69:
        adj = raw_conf - 12
    # Cap overconfidence for historically weak bet types
    if hist < 35 and adj > 70:
        adj = 65

    return max(10, min(95, adj))


# ── Football API ──────────────────────────────────────────────

def get_team_stats(team_id, league_id, season):
    """Returns avg goals for/against, clean sheet % and no-score % for a team."""
    url  = "https://v3.football.api-sports.io/teams/statistics"
    data = api_get(url, {"x-apisports-key": APIFOOTBALL},
                   {"team": team_id, "league": league_id, "season": season})
    r = data.get("response", {})
    if not r:
        return {}
    try:
        played       = r["games"]["played"]["total"] or 1
        goals_for    = float(r["goals"]["for"]["average"]["total"] or 0)
        goals_ag     = float(r["goals"]["against"]["average"]["total"] or 0)
        clean_pct    = round(r["clean_sheet"]["total"] / played * 100)
        no_score_pct = round(r["failed_to_score"]["total"] / played * 100)
        return {
            "avg_gf": goals_for, "avg_ga": goals_ag,
            "clean_pct": clean_pct, "no_score_pct": no_score_pct,
        }
    except Exception:
        return {}


def get_h2h(home_id, away_id, limit=5):
    """Returns last N head-to-head results between two teams."""
    url  = "https://v3.football.api-sports.io/fixtures"
    data = api_get(url, {"x-apisports-key": APIFOOTBALL},
                   {"h2h": f"{home_id}-{away_id}", "last": limit, "status": "FT"})
    results = []
    for m in data.get("response", []):
        try:
            hg = m["goals"]["home"] or 0
            ag = m["goals"]["away"] or 0
            results.append(f"{m['teams']['home']['name']} {hg}-{ag} {m['teams']['away']['name']}")
        except Exception:
            continue
    return results


def get_matches(date=None):
    italy_time = datetime.now(timezone.utc) + timedelta(hours=2)
    if not date:
        date = italy_time.strftime("%Y-%m-%d")
    url    = "https://v3.football.api-sports.io/fixtures"
    data   = api_get(url, {"x-apisports-key": APIFOOTBALL}, {"date": date})
    print(f"Partite {date}: {data.get('results', 0)} trovate")
    return [m for m in data.get("response", [])
            if is_allowed(m) and m["fixture"]["status"]["short"] in ["NS", "TBD"]]


def get_live_matches():
    url  = "https://v3.football.api-sports.io/fixtures"
    data = api_get(url, {"x-apisports-key": APIFOOTBALL}, {"live": "all"})
    filtered = []
    for m in data.get("response", []):
        if not is_allowed(m):
            continue
        elapsed = m["fixture"]["status"].get("elapsed") or 0
        if not (LIVE_MIN_MINUTE <= elapsed <= LIVE_MAX_MINUTE):
            continue
        hg = m["goals"].get("home") or 0
        ag = m["goals"].get("away") or 0
        if abs(hg - ag) <= 1 or (hg + ag) >= 2:
            filtered.append(m)
    return filtered


def get_recent_form(team_id, league_id, season):
    url  = "https://v3.football.api-sports.io/fixtures"
    data = api_get(url, {"x-apisports-key": APIFOOTBALL},
                   {"team": team_id, "league": league_id, "season": season, "last": 5, "status": "FT"})
    form = []
    for m in data.get("response", []):
        try:
            home_id = m["teams"]["home"]["id"]
            hg = m["goals"]["home"] or 0
            ag = m["goals"]["away"] or 0
            r  = "V" if (team_id == home_id and hg > ag) or (team_id != home_id and ag > hg) \
                 else ("P" if hg == ag else "S")
            form.append(f"{m['teams']['home']['name']} {hg}-{ag} {m['teams']['away']['name']} ({r})")
        except Exception:
            continue
    return form


def get_fixture_result(fixture_id):
    url  = "https://v3.football.api-sports.io/fixtures"
    data = api_get(url, {"x-apisports-key": APIFOOTBALL}, {"id": fixture_id})
    fixtures = data.get("response", [])
    if not fixtures:
        return None, None, None, None
    m = fixtures[0]
    if m["fixture"]["status"]["short"] not in ["FT", "AET", "PEN"]:
        return None, None, None, None
    hg    = m["goals"]["home"] or 0
    ag    = m["goals"]["away"] or 0
    esito = "1" if hg > ag else ("X" if hg == ag else "2")
    return esito, f"{hg}-{ag}", hg, ag


def search_team_matches(team_name):
    italy_time = datetime.now(timezone.utc) + timedelta(hours=2)
    results    = []
    for delta in [0, 1]:
        date = (italy_time + timedelta(days=delta)).strftime("%Y-%m-%d")
        url  = "https://v3.football.api-sports.io/fixtures"
        data = api_get(url, {"x-apisports-key": APIFOOTBALL}, {"date": date})
        for m in data.get("response", []):
            if team_name.lower() in m["teams"]["home"]["name"].lower() or \
               team_name.lower() in m["teams"]["away"]["name"].lower():
                results.append(m)
    return results


# ── Claude analysis ───────────────────────────────────────────

def analyze_prematch(m, form_h, form_a, odds, stats_h=None, stats_a=None, h2h=None):
    home       = m["teams"]["home"]["name"]
    away       = m["teams"]["away"]["name"]
    league     = m["league"]["name"].lower()
    round_info = m["league"].get("round", "").lower()

    if any(k in league for k in ["champions", "europa", "conference", "cup", "coppa", "pokal"]):
        importanza = (
            "FINALE/SEMIFINALE \u2014 tattica conservativa, meno gol"
            if any(k in round_info for k in ["final", "semi", "quarter", "quarti"])
            else "COPPA \u2014 possibili rotazioni"
        )
    else:
        importanza = "CAMPIONATO REGOLARE"

    q1, qx, q2 = odds.get("1", 0), odds.get("X", 0), odds.get("2", 0)
    prob_info = ""
    if q1 and qx and q2:
        p1  = round(1/q1*100, 1)
        px  = round(1/qx*100, 1)
        p2  = round(1/q2*100, 1)
        p1x = round((1/q1 + 1/qx)*100, 1)
        px2 = round((1/qx + 1/q2)*100, 1)
        p12 = round((1/q1 + 1/q2)*100, 1)
        prob_info = f"Prob implicite: 1={p1}% X={px}% 2={p2}% | 1X={p1x}% X2={px2}% 12={p12}%"

    # Extended odds (Over/Under, GG/NG)
    ou_lines = [f"{k}={v}" for k, v in sorted(odds.items())
                if k not in ("1","X","2","GG","NG") and v]
    gg_ng    = f"GG={odds.get('GG',0)} NG={odds.get('NG',0)}"
    ext_odds = f"Over/Under: {', '.join(ou_lines) or 'N/D'} | {gg_ng}"

    # Team stats
    def fmt_stats(s, name):
        if not s:
            return f"{name}: dati non disponibili"
        return (
            f"{name}: media gol fatti={s['avg_gf']} subiti={s['avg_ga']} "
            f"clean sheet={s['clean_pct']}% no-gol={s['no_score_pct']}%"
        )

    prompt = (
        f"Analizza {home} vs {away}.\n"
        f"IMPORTANZA: {importanza}\n"
        f"FORMA CASA (ultime 5): {form_h}\n"
        f"FORMA OSPITE (ultime 5): {form_a}\n"
        f"{fmt_stats(stats_h, 'STATS CASA')}\n"
        f"{fmt_stats(stats_a, 'STATS OSPITE')}\n"
        f"H2H (ultimi 5): {h2h or 'N/D'}\n"
        f"QUOTE 1X2: 1={q1} X={qx} 2={q2}\n"
        f"{prob_info}\n"
        f"QUOTE ESTESE: {ext_odds}\n\n"
        f"REGOLE BASATE SU DATI STORICI (425 previsioni risolte):\n"
        f"- '1' ha 54.8% di successo storico: preferiscila quando la casa \xe8 favorita e in forma\n"
        f"- '2' ha 31.8%: usala solo se l'ospite \xe8 chiaramente superiore\n"
        f"- 'X', '1X', 'X2', '12': solo in caso di vera incertezza con valore nelle quote\n"
        f"- Over/Under e GG/NG: usa le quote reali qui sopra e le stats gol per valutare\n"
        f"- Assegna confidence 70+ solo con almeno 3 segnali concordanti (forma, stats, quote)\n"
        f"- La fascia 60-69 \xe8 storicamente la meno affidabile: vai a 70+ o resta sotto 60\n\n"
        f"Scegli LA MIGLIORE giocata tra: 1, X, 2, 1X, X2, 12, Over 1.5/2.5/3.5, Under 1.5/2.5/3.5, GG, NG.\n"
        'Rispondi SOLO con questo JSON: {"giocata":"es. 1 o Over 2.5 o GG","quota":X,"motivazione":"max 1 riga","confidence":X}'
    )

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=_SYS_PREMATCH,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def analyze_live(m, odds):
    home   = m["teams"]["home"]["name"]
    away   = m["teams"]["away"]["name"]
    score  = m["goals"]
    minute = m["fixture"]["status"].get("elapsed", "?")
    try:
        elapsed_int = int(str(minute).replace("+", "").strip())
    except Exception:
        elapsed_int = 45

    if elapsed_int <= 45:
        focus = "Prediligi Over/Under totale o GG/NG basandoti sul ritmo di gioco. Usa 1X2 solo se c'è un vantaggio netto."
    elif elapsed_int <= 60:
        focus = "Analizza il punteggio e il momentum. Priorità a Over/Under e GG/NG, poi eventualmente doppia chance."
    else:
        focus = "Oltre 60': punta su Over/Under parziale o GG/NG. Evita 1X2 a meno di situazione chiarissima."

    prompt = (
        f"{home} vs {away} \u2014 min {minute} \u2014 "
        f"{score.get('home',0)}-{score.get('away',0)}\n"
        f"QUOTE: {odds}\n"
        f"{focus}\n"
        f"REGOLA: nel contesto live Over/Under e GG/NG offrono il miglior valore. "
        f"Scegli la giocata con più valore reale tra Over/Under e GG/NG prima di considerare 1X2.\n"
        '{"giocata":"descrizione","quota":X,"motivazione":"max 2 righe","confidence":X,"rischio":"basso/medio/alto"}'
    )

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=_SYS_LIVE,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ── Jobs ──────────────────────────────────────────────────────

def run_analysis(matches, label="oggi"):
    stop_analysis.clear()
    leagues = group_by_league(matches)
    send_telegram(f"\U0001f4c5 <b>{len(matches)}</b> partite in <b>{len(leagues)}</b> campionati...")

    for league_name, league_matches in leagues.items():
        if stop_analysis.is_set():
            send_telegram("\U0001f6d1 Analisi fermata.")
            return
        blocks = []
        for m in league_matches:
            if stop_analysis.is_set():
                send_telegram("\U0001f6d1 Analisi fermata.")
                return
            home = m["teams"]["home"]["name"]
            away = m["teams"]["away"]["name"]
            print(f"Analisi: {home} vs {away}...")
            try:
                league_id = m["league"]["id"]
                season    = m["league"]["season"]
                home_id   = m["teams"]["home"]["id"]
                away_id   = m["teams"]["away"]["id"]
                form_h    = get_recent_form(home_id, league_id, season)
                form_a    = get_recent_form(away_id, league_id, season)
                stats_h   = get_team_stats(home_id, league_id, season)
                stats_a   = get_team_stats(away_id, league_id, season)
                h2h       = get_h2h(home_id, away_id)
                odds      = get_odds(home, away)
                a_raw     = analyze_prematch(m, form_h, form_a, odds, stats_h, stats_a, h2h)
                a         = parse_json(a_raw)
                giocata   = a.get("giocata", "")
                # use real bookmaker quote; fall back to Claude's value
                real_q    = get_real_quota(giocata, odds)
                try:
                    quota_num = real_q if real_q else float(str(a.get("quota", 1.0)).replace(",", "."))
                except Exception:
                    quota_num = 1.0
                cal_conf  = calibrate_confidence(int(a.get("confidence", 0)), giocata)
                blocks.append(format_match_block(m, a_raw, odds))
                add_prediction({
                    "date":       datetime.now().strftime("%Y-%m-%d"),
                    "match":      f"{home} vs {away}",
                    "league":     m["league"]["name"],
                    "country":    m["league"]["country"],
                    "fixture_id": m["fixture"]["id"],
                    "value_bet":  giocata,
                    "quota":      quota_num,
                    "confidence": cal_conf,
                    "result":     "pending",
                })
            except Exception as e:
                print(f"Errore {home} vs {away}: {e}")
                blocks.append(f"\u26a0\ufe0f Errore: {home} vs {away}\n")
            time.sleep(2)

        if blocks:
            msg = f"\U0001f3c6 <b>{league_name}</b>\n\n" + "\n".join(blocks)
            send_telegram(msg[:4000] + ("\n..." if len(msg) > 4000 else ""))
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
    tomorrow   = (italy_time + timedelta(days=1)).strftime("%Y-%m-%d")
    send_telegram("\U0001f50d Analisi partite di domani...")
    matches = get_matches(tomorrow)
    if not matches:
        send_telegram("\u26a0\ufe0f Nessuna partita trovata domani.")
        return
    run_analysis(matches, "domani")


def live_job():
    stop_live.clear()
    matches = get_live_matches()
    if not matches:
        send_telegram_live("\u26bd Nessuna partita live con valore (30'-80').")
        return
    send_telegram_live(f"\U0001f534 <b>{len(matches)} partite live \u2014 analisi in corso...</b>")
    results_by_league = {}

    def analyze_one(m):
        odds = get_odds(m["teams"]["home"]["name"], m["teams"]["away"]["name"])
        return format_live_block(m, analyze_live(m, odds))

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(analyze_one, m): m for m in matches}
        for future in as_completed(futures):
            if stop_live.is_set():
                send_telegram_live("\U0001f6d1 Analisi live fermata!")
                return
            try:
                m     = futures[future]
                block = future.result()
                key   = f"{m['league']['country']} \u2014 {m['league']['name']}"
                results_by_league.setdefault(key, []).append(block)
            except Exception as e:
                print(f"Errore live: {e}")

    for league_name, blocks in results_by_league.items():
        if stop_live.is_set():
            send_telegram_live("\U0001f6d1 Analisi live fermata!")
            return
        msg = f"\U0001f534 <b>{league_name}</b>\n\n" + "\n".join(blocks)
        send_telegram_live(msg[:4000] + ("\n..." if len(msg) > 4000 else ""))
        time.sleep(2)
    send_telegram_live("\u2705 <b>Analisi live completata!</b>")


def check_and_report_results():
    today     = datetime.now().strftime("%Y-%m-%d")
    today_bets = list(predictions_col.find({"date": today, "result": "pending"}, {"_id": 0}))
    if not today_bets:
        send_telegram("\u26a0\ufe0f Nessuna previsione da verificare oggi.")
        return
    updated = 0
    lines   = ["\U0001f4ca <b>Riepilogo di oggi:</b>\n"]
    for h in today_bets:
        fixture_id = h.get("fixture_id")
        if not fixture_id:
            continue
        esito_1x2, score, hg, ag = get_fixture_result(fixture_id)
        if esito_1x2 is None:
            continue
        giocata  = h.get("value_bet", "")
        corretto = verifica_giocata(giocata, hg, ag)
        if corretto is None:
            corretto = esito_1x2 == giocata
        update_prediction(h["match"], today, "win" if corretto else "loss", esito_1x2, score)
        updated += 1
        emoji = "\u2705" if corretto else "\u274c"
        lines.append(
            f"{emoji} <b>{h['match']}</b>\n"
            f"   {giocata} | {score} | {'presa' if corretto else 'sbagliata'}\n"
        )
    if not updated:
        send_telegram("\u23f3 Partite ancora in corso.")
        return
    wins = sum(1 for l in lines[1:] if l.startswith("\u2705"))
    pct  = round((wins / updated) * 100, 1)
    lines.insert(1, f"\u2705 {wins}/{updated} | \U0001f3af {pct}%\n")
    send_telegram("\n".join(lines[:15]))


def show_pending():
    today   = datetime.now().strftime("%Y-%m-%d")
    pending = list(predictions_col.find({"date": today, "result": "pending"}, {"_id": 0}))
    if not pending:
        send_telegram("\u26a0\ufe0f Nessuna previsione in attesa oggi.")
        return
    msg = f"\u23f3 <b>{len(pending)} previsioni in attesa:</b>\n\n"
    for h in pending:
        msg += f"\u26bd <b>{h['match']}</b>\n\U0001f4a1 {h['value_bet']} | \U0001f525 {h['confidence']}/100\n\n"
    send_telegram(msg[:4000])


def show_stats():
    try:
        total = predictions_col.count_documents({})
        if total == 0:
            send_telegram("\U0001f4ca Nessuna previsione ancora.")
            return
        wins    = predictions_col.count_documents({"result": "win"})
        losses  = predictions_col.count_documents({"result": "loss"})
        pending = predictions_col.count_documents({"result": "pending"})
        pct     = round((wins / (total - pending)) * 100, 1) if (total - pending) > 0 else 0
        send_telegram(
            f"\U0001f4ca <b>Statistiche CeccoBet</b>\n\n"
            f"\U0001f4cb Totali: {total}\n"
            f"\u2705 Vinte: {wins}\n"
            f"\u274c Perse: {losses}\n"
            f"\u23f3 In attesa: {pending}\n"
            f"\U0001f3af Precisione: {pct}%\n"
        )
    except Exception as e:
        print(f"Errore stats: {e}")
        send_telegram("\u26a0\ufe0f Errore statistiche.")


def top_job():
    today = datetime.now().strftime("%Y-%m-%d")
    top   = list(predictions_col.find(
        {"date": today, "confidence": {"$gte": CONFIDENCE_MIN}}, {"_id": 0}
    ).sort("confidence", -1).limit(10))
    if not top:
        send_telegram("\u26a0\ufe0f Nessuna top value bet oggi.")
        return
    msg = "\u2b50 <b>TOP VALUE BETS DI OGGI</b>\n\n"
    for h in top:
        msg += f"\u26bd <b>{h['match']}</b>\n\U0001f4a1 {h['value_bet']} | \U0001f525 {h['confidence']}/100\n\n"
    send_telegram(msg)


def multipla_job():
    today = datetime.now().strftime("%Y-%m-%d")
    top   = list(predictions_col.find(
        {"date": today, "confidence": {"$gte": CONFIDENCE_MIN}}, {"_id": 0}
    ).sort("confidence", -1).limit(5))
    if len(top) < 3:
        send_telegram(f"\u26a0\ufe0f Solo {len(top)} value bet oggi, servono almeno 3 per la multipla.")
        return
    selezioni      = top[:min(5, len(top))]
    quota_combined = 1.0
    for s in selezioni:
        try:
            q = float(str(s.get("quota", 1.0)).replace(",", "."))
            if q > 1:
                quota_combined *= q
        except Exception:
            pass
    quota_combined = round(quota_combined, 2)
    msg = f"\U0001f3af <b>MULTIPLA DEL GIORNO ({len(selezioni)} selezioni)</b>\n\n"
    for i, h in enumerate(selezioni, 1):
        msg += f"{i}. \u26bd <b>{h['match']}</b>\n   \U0001f4a1 {h.get('value_bet','')} | \U0001f525 {h['confidence']}/100\n\n"
    msg += f"\U0001f4b0 Quota combinata: <b>{quota_combined}</b>"
    send_telegram(msg)


def cerca_job(team_name):
    send_telegram(f"\U0001f50d Cerco partite di <b>{team_name}</b>...")
    matches = search_team_matches(team_name)
    if not matches:
        send_telegram(f"\u26a0\ufe0f Nessuna partita trovata per <b>{team_name}</b>.")
        return
    msg = f"\U0001f4cb <b>Partite {team_name}:</b>\n\n"
    for m in matches:
        kick_utc = datetime.fromisoformat(m["fixture"]["date"].replace("Z", "+00:00"))
        kick_it  = kick_utc.astimezone(timezone(timedelta(hours=2)))
        msg += (
            f"\u26bd <b>{m['teams']['home']['name']} vs {m['teams']['away']['name']}</b>\n"
            f"\U0001f3c6 {m['league']['name']} | \U0001f550 {kick_it.strftime('%d/%m %H:%M')}\n\n"
        )
    send_telegram(msg)


def watchdog():
    errors = []
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{os.getenv('TELEGRAM_TOKEN')}/getMe", timeout=10
        )
        if not r.json().get("ok"):
            errors.append("Telegram non risponde")
    except Exception:
        errors.append("Telegram non raggiungibile")
    if errors:
        send_telegram_admin("\u26a0\ufe0f <b>Watchdog:</b>\n" + "\n".join(errors))
    else:
        print(f"[Watchdog] OK \u2014 {datetime.now().strftime('%H:%M')}")


# ── Command listener ──────────────────────────────────────────

_HELP = (
    "\U0001f916 <b>Comandi CeccoBet:</b>\n\n"
    "/analisi \u2014 Partite di oggi\n"
    "/domani \u2014 Partite di domani\n"
    "/live \u2014 Giocate live\n"
    "/top \u2014 Top value bet oggi\n"
    "/alert \u2014 Top value bet oggi\n"
    "/multipla \u2014 Multipla (3-5 selezioni)\n"
    "/cerca [squadra] \u2014 Cerca squadra\n"
    "/riepilogo \u2014 Risultati di oggi\n"
    "/risultato \u2014 Previsioni in attesa\n"
    "/stats \u2014 Statistiche storiche\n"
    "/stop \u2014 Ferma analisi\n"
    "/stoplive \u2014 Ferma live\n"
    "/help \u2014 Questo messaggio\n"
)


def listen_commands():
    last_update_id = 0
    url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_TOKEN')}/getUpdates"
    while True:
        try:
            r       = requests.get(url, params={"timeout": 30, "offset": last_update_id + 1}, timeout=35)
            updates = r.json().get("result", [])
            for update in updates:
                last_update_id = update["update_id"]
                msg        = update.get("message", {}) or update.get("channel_post", {})
                text       = msg.get("text", "").strip()
                text_lower = text.lower()
                user_id    = msg.get("from", {}).get("id", 0)
                if user_id not in ADMIN_IDS:
                    continue
                if text_lower == "/start":
                    send_telegram_admin("\U0001f916 <b>CeccoBet attivo!</b>\nUsa /help per i comandi.")
                elif text_lower == "/analisi":
                    threading.Thread(target=daily_job, daemon=True).start()
                elif text_lower == "/domani":
                    threading.Thread(target=domani_job, daemon=True).start()
                elif text_lower == "/live":
                    threading.Thread(target=live_job, daemon=True).start()
                elif text_lower == "/stop":
                    stop_analysis.set()
                    stop_live.set()
                    send_telegram("\U0001f6d1 Fermato!")
                elif text_lower == "/stoplive":
                    stop_live.set()
                    send_telegram_live("\U0001f6d1 Live fermato!")
                elif text_lower in ("/top", "/alert"):
                    top_job()
                elif text_lower == "/multipla":
                    multipla_job()
                elif text_lower == "/riepilogo":
                    threading.Thread(target=check_and_report_results, daemon=True).start()
                elif text_lower == "/risultato":
                    show_pending()
                elif text_lower == "/stats":
                    show_stats()
                elif text_lower.startswith("/cerca "):
                    team = text[7:].strip()
                    if team:
                        threading.Thread(target=cerca_job, args=(team,), daemon=True).start()
                elif text_lower == "/help":
                    send_telegram(_HELP)
        except Exception as e:
            print(f"Errore listener: {e}")
        time.sleep(2)


if __name__ == "__main__":
    print("Bot avviato!")
    threading.Thread(target=listen_commands, daemon=True).start()
    schedule.every().day.at("22:00").do(check_and_report_results)
    schedule.every(6).hours.do(watchdog)
    send_telegram_admin(
        "\U0001f916 <b>CeccoBet attivo!</b>\n\n"
        "/analisi \u2014 Partite di oggi\n"
        "/domani \u2014 Partite di domani\n"
        "/live \u2014 Giocate live\n"
        "/top \u2014 Top value bet\n"
        "/multipla \u2014 Multipla (3-5 selezioni)\n"
        "/riepilogo \u2014 Risultati oggi\n"
        "/risultato \u2014 Previsioni in attesa\n"
        "/stats \u2014 Statistiche\n"
        "/help \u2014 Aiuto"
    )
    while True:
        schedule.run_pending()
        time.sleep(60)
