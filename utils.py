import os, requests, time, json
from datetime import datetime, timedelta, timezone

TG_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TG_CHAT      = os.getenv("TELEGRAM_CHAT_ID")
TG_CHAT_LIVE = os.getenv("TELEGRAM_CHAT_LIVE") or os.getenv("TELEGRAM_CHAT_ID")

from config import ADMIN_IDS

# ── Invio messaggi Telegram ───────────────────────────────────
def send_telegram(text, retries=3):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for attempt in range(retries):
        try:
            r = requests.post(url, json={"chat_id": TG_CHAT, "text": text,
                                          "parse_mode": "HTML"}, timeout=15)
            print(f"Telegram: {r.status_code}")
            return
        except Exception as e:
            print(f"Errore Telegram ({attempt+1}/{retries}): {e}")
            time.sleep(3)

def send_telegram_live(text, retries=3):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for attempt in range(retries):
        try:
            r = requests.post(url, json={"chat_id": str(TG_CHAT_LIVE), "text": text,
                                          "parse_mode": "HTML"}, timeout=15)
            print(f"Telegram Live: {r.status_code}")
            return
        except Exception as e:
            print(f"Errore Telegram Live ({attempt+1}/{retries}): {e}")
            time.sleep(3)

def send_telegram_admin(text, retries=3):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for attempt in range(retries):
        try:
            r = requests.post(url, json={"chat_id": str(ADMIN_IDS[0]), "text": text,
                                          "parse_mode": "HTML"}, timeout=15)
            print(f"Telegram Admin: {r.status_code}")
            return
        except Exception as e:
            print(f"Errore Telegram Admin ({attempt+1}/{retries}): {e}")
            time.sleep(3)

# ── Formattazione messaggi ────────────────────────────────────
def format_match_block(match, analysis, best_odds=None):
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
    from config import CONFIDENCE_MIN
    star = "\u2b50 <b>TOP VALUE BET</b>\n" if confidence >= CONFIDENCE_MIN else ""
    best_str = ""
    if best_odds:
        parts = [f"{esito}: {q} ({bk})" for esito, (q, bk) in best_odds.items() if q > 0]
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
    momentum = a.get('momentum', '')
    momentum_str = f"\U0001f4ca {momentum}\n" if momentum else ""
    return (
        f"\U0001f534 <b>{home} vs {away}</b> \u23f1 {minute}' | {score['home']}-{score['away']}\n"
        f"{momentum_str}"
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
