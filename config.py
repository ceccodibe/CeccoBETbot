# ── Campionati analizzati (pre-match) ─────────────────────────
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

# ── Parole chiave da escludere ────────────────────────────────
EXCLUDE_KEYWORDS = [
    'u19', 'u18', 'u17', 'u16', 'u15', 'u23', 'u21', 'u20',
    'youth', 'under', 'reserve', 'riserve', 'primavera',
    ' w ', 'women', 'femminile', 'femenino', 'feminine', 'ladies', 'girls'
]

# ── Sport keys per The Odds API ───────────────────────────────
SPORT_KEYS = [
    "soccer_italy_serie_a", "soccer_italy_serie_b",
    "soccer_england_premier_league", "soccer_efl_champ", "soccer_england_league1",
    "soccer_france_ligue_one", "soccer_france_ligue_two",
    "soccer_spain_la_liga", "soccer_spain_segunda_division",
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

# ── Impostazioni generali ─────────────────────────────────────
BANKROLL           = 1000     # Bankroll default in euro
AUTO_NOTIFY_HOURS  = 2        # Ore prima del match per notifica automatica
CONFIDENCE_MIN     = 60       # Confidence minima per top value bet
LIVE_MIN_MINUTE    = 30       # Minuto minimo per analisi live
LIVE_MAX_MINUTE    = 80       # Minuto massimo per analisi live
ODDS_CACHE_SECONDS = 3600     # Cache quote prematch (1 ora) — live usa 5 min separati
ADMIN_IDS          = [8317266009, 2129248376]
