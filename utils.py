import os, requests, time, json
from datetime import datetime, timedelta, timezone

TG_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TG_CHAT      = os.getenv("TELEGRAM_CHAT_ID")
TG_CHAT_LIVE = os.getenv("TELEGRAM_CHAT_LIVE") or os.getenv("TELEGRAM_CHAT_ID")

from config import ADMIN_IDS, CONFIDENCE_MIN


def parse_json(text):
    try:
        clean = text.strip().replace("```json", "").replace("```", "").strip()
        if clean.startswith("json"):
            clean = clean[4:].strip()
        start = clean.find("{")
        end   = clean.rfind("}")
        if start != -1 and end != -1:
            clean = clean[start:end+1]
        return json.loads(clean)
    except Exception:
        return {}


def _send(chat_id, text, label="", retries=3):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for attempt in range(retries):
        try:
            r = requests.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=15,
            )
            if not r.ok:
                print(f"Telegram {label} errore {r.status_code}: {r.text[:120]}")
            return
        except Exception as e:
            print(f"Errore Telegram {label} ({attempt+1}/{retries}): {e}")
            time.sleep(3)


def send_telegram(text):
    _send(TG_CHAT, text, label="main")


def send_telegram_live(text):
    _send(str(TG_CHAT_LIVE), text, label="live")


def send_telegram_admin(text):
    _send(str(ADMIN_IDS[0]), text, label="admin")


def format_match_block(match, analysis, odds=None):
    a        = parse_json(analysis) if isinstance(analysis, str) else analysis
    home     = match["teams"]["home"]["name"]
    away     = match["teams"]["away"]["name"]
    kick_utc = datetime.fromisoformat(match["fixture"]["date"].replace("Z", "+00:00"))
    kick_it  = kick_utc.astimezone(timezone(timedelta(hours=2)))
    kickoff  = kick_it.strftime("%H:%M")
    conf     = int(a.get("confidence", 0))
    star     = "\u2b50 <b>TOP VALUE BET</b>\n" if conf >= CONFIDENCE_MIN else ""
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


def format_live_block(match, analysis):
    a       = parse_json(analysis) if isinstance(analysis, str) else analysis
    home    = match["teams"]["home"]["name"]
    away    = match["teams"]["away"]["name"]
    score   = match["goals"]
    minute  = match["fixture"]["status"].get("elapsed", "?")
    rischio = a.get("rischio", "N/D")
    rischio_emoji = (
        "\U0001f7e2" if rischio == "basso" else
        ("\U0001f7e1" if rischio == "medio" else "\U0001f534")
    )
    return (
        f"\U0001f534 <b>{home} vs {away}</b> {minute}' | "
        f"{score.get('home',0)}-{score.get('away',0)}\n"
        f"\U0001f4a1 <b>{a.get('giocata','N/A')}</b> @ {a.get('quota','?')}\n"
        f"{rischio_emoji} {rischio} | \U0001f525 {a.get('confidence','?')}/100 | "
        f"{a.get('motivazione','')}\n"
    )


def group_by_league(matches):
    leagues = {}
    for m in matches:
        key = f"{m['league']['country']} \u2014 {m['league']['name']}"
        leagues.setdefault(key, []).append(m)
    return leagues
