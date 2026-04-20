import os, requests, anthropic, time, threading, schedule
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
    try:
        pred.pop("_id", None)
        predictions_col.insert_one(pred)
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
                params = {"apiKey": ODDS_KEY, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal"}
                r      = requests.get(url, params=params, timeout=10)
                data   = r.json()
                if isinstance(data, list):
                    all_events.extend(data)
                elif data.get("error_code") == "OUT_OF_USAGE_CREDITS":
                    print("Odds: crediti esauriti!")
                    break
                time.sleep(0.3)
            except Exception as e:
                print(f"Odds error {sk}: {e}")
        _odds_cache      = all_events
        _odds_cache_time = now
        print(f"Quote caricate: {len(all_events)} eventi")
        return _odds_cache


def get_odds(home, away):
    data = load_all_odds()
    h, a = home.lower().strip(), away.lower().strip()
    for event in data:
        ev_home = event.get("home_team", "").lower()
        ev_away = event.get("away_team", "").lower()
        if (h in ev_home or ev_home in h or h.split()[0] in ev_home) and \
           (a in ev_away or ev_away in a or a.split()[0] in ev_away):
            best = {"1": 0, "X": 0, "2": 0}
            for bk in event.get("bookmakers", []):
                for market in bk.get("markets", []):
                    if market.get("key") == "h2h":
                        for o in market.get("outcomes", []):
                            price = o.get("price", 0)
                            name  = o.get("name", "").lower()
                            if name == ev_home and price > best["1"]:
                                best["1"] = price
                            elif name == "draw" and price > best["X"]:
                                best["X"] = price
                            elif name == ev_away and price > best["2"]:
                                best["2"] = price
            return best
    return {}


# ── Football API ──────────────────────────────────────────────

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

def analyze_prematch(m, form_h, form_a, odds):
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

    prompt = (
        f"Analizza {home} vs {away}.\n"
        f"IMPORTANZA: {importanza}\n"
        f"FORMA CASA (ultime 5): {form_h}\n"
        f"FORMA OSPITE (ultime 5): {form_a}\n"
        f"QUOTE 1X2: 1={q1} X={qx} 2={q2}\n"
        f"{prob_info}\n\n"
        f"REGOLE BASATE SU DATI STORICI (425 previsioni risolte):\n"
        f"- '1' ha 54.8% di successo: preferiscila quando la casa è favorita e in forma\n"
        f"- '2' ha 31.8%: usala solo se l'ospite è chiaramente superiore\n"
        f"- 'X', '1X', 'X2', '12': solo in caso di vera incertezza con valore nelle quote\n"
        f"- Over/Under e GG/NG: usali SOLO se la forma di entrambe le squadre lo giustifica chiaramente\n"
        f"- Assegna confidence 70+ solo con almeno 3 segnali concordanti (forma, quota, storia)\n"
        f"- La fascia 60-69 è storicamente la meno affidabile: vai a 70+ o resta sotto 60\n\n"
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
                odds      = get_odds(home, away)
                a_raw     = analyze_prematch(m, form_h, form_a, odds)
                a         = parse_json(a_raw)
                blocks.append(format_match_block(m, a_raw, odds))
                try:
                    quota_num = float(str(a.get("quota", 1.0)).replace(",", "."))
                except Exception:
                    quota_num = 1.0
                add_prediction({
                    "date":       datetime.now().strftime("%Y-%m-%d"),
                    "match":      f"{home} vs {away}",
                    "league":     m["league"]["name"],
                    "country":    m["league"]["country"],
                    "fixture_id": m["fixture"]["id"],
                    "value_bet":  a.get("giocata", ""),
                    "quota":      quota_num,
                    "confidence": int(a.get("confidence", 0)),
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
