# CeccoBet Bot 🤖⚽

Bot Telegram per analisi scommesse sportive con AI.

## Comandi disponibili

| Comando | Descrizione |
|---------|-------------|
| `/analisi` | Analisi partite di oggi |
| `/domani` | Analisi partite di domani |
| `/live` | Giocate live (partite tra 30' e 80') |
| `/top` | Top value bet del giorno |
| `/multipla` | Multipla del giorno (top 2 bet) |
| `/cerca [squadra]` | Cerca partite di una squadra |
| `/alert` | Controlla value alert adesso |
| `/riepilogo` | Risultati previsioni di oggi |
| `/risultato` | Previsioni in attesa |
| `/stop` | Ferma analisi in corso |
| `/stoplive` | Ferma analisi live |
| `/stats` | Statistiche previsioni |
| `/help` | Lista comandi |

## Variabili d'ambiente richieste

```
APIFOOTBALL_KEY=         # Chiave API-Football (api-football.com)
ODDS_API_KEY=            # Chiave The Odds API (the-odds-api.com)
ANTHROPIC_API_KEY=       # Chiave Anthropic Claude (console.anthropic.com)
TELEGRAM_TOKEN=          # Token bot Telegram (@BotFather)
TELEGRAM_CHAT_ID=        # ID canale principale
TELEGRAM_CHAT_LIVE=      # ID canale live
TZ=Europe/Rome           # Timezone italiana
```

## Campionati analizzati

🇮🇹 Serie A, Serie B, Coppa Italia  
🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League, Championship, FA Cup  
🇫🇷 Ligue 1, Ligue 2, Coupe de France  
🇪🇸 La Liga, Copa del Rey  
🇩🇪 Bundesliga, 2. Bundesliga, DFB Pokal  
🇵🇹 Primeira Liga  
🇳🇱 Eredivisie, Eerste Divisie  
🇧🇪 Pro League  
🇦🇷 Liga Profesional  
🇧🇷 Serie A, Serie B  
🇨🇴 Primera A  
🇹🇷 Super Lig  
🇺🇸 MLS  
🌍 Champions League, Europa League, Conference League, World Cup  

## Deployment

Il bot gira su [Railway](https://railway.app).  
Si riavvia automaticamente in caso di crash (max 10 volte).

## Costo mensile stimato

| Servizio | Costo |
|----------|-------|
| Anthropic Claude API | ~€5-15/mese |
| The Odds API | Piano a pagamento |
| API-Football | Piano a pagamento |
| Railway hosting | ~€3-5/mese |
