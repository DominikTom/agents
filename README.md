# MyBed Agents

AI chief of staff dla CEO MyBed Group. Czyta WhatsApp, Gmail, kalendarz, MyBed Group OS i hurtownię sprzedaży (dash). Z tego robi:

- **Dziś (podgląd dnia na żywo)** — strona `/dzis`: mini kalendarz dnia i plan z typem każdej sprawy (decyzja / odpowiedz / do zrobienia / zadanie OS / czekasz na), wyłapany z maili, WhatsAppa i MyBed OS. Odświeża się co minutę; „Zrobione”, „Jutro”, „Obsłużone”, szybkie dodawanie sprawy.
- **Sprawy zamykają się same, gdy załatwisz je gdziekolwiek**: co 15 min AI szuka dowodów w dalszym ciągu rozmowy, w INNYCH czatach WhatsApp, w mailach i w Twoich notatkach (z czatu z Claude). Mail, na który odpisałeś na WhatsAppie (albo odwrotnie), nie wisi w „Czeka na Twoją odpowiedź”. Raporty dostają listę spraw zamkniętych w okresie i nie pokazują ich jako otwartych.
- **Poranny briefing, podsumowanie dnia i tygodnia** (piątek). Wysyła je e-mailem i zapisuje w panelu. Sekcje, kolejność, długość, godziny, model AI i własne instrukcje ustawiasz w **Studio raportów**.
- **Rozumienie czatów**: raz dziennie AI streszcza każdy aktywny czat (kontekst odpowiedzi, znaczniki głosówek i plików, role osób w firmie). Wychodzą z tego ustalenia, otwarte kwestie, informacja „czeka na Twoją odpowiedź” i zobowiązania.
- **Zobowiązania**: co obiecałeś, o co Cię proszono i co inni obiecali Tobie. Po zatwierdzeniu trafiają do MyBed OS jako prywatne zadanie.
- **Pulpit**: KPI sprzedaży, rozmowy i maile czekające na odpowiedź, kalendarz, zadania z OS.
- **Raporty dnia zespołu** (Zespół): źródłem jest **MyBed OS → Daily Update** (kolekcja `dailyUpdates`). Analizę AI i zmiany w zadaniach robi OS, a zatwierdza sam autor raportu. Agenci tylko czytają: pokazują raport, zmiany w OS, braki raportu (lista uczestników Daily Update, dni robocze ze świętami, od 10:00 następnego dnia roboczego) i eskalacje (prośby bez reakcji, zadania „czekam” w 3 raportach z rzędu, seria dni bez raportu, stare blokery z raportów). Okres przejściowy: dla osób bez raportu w OS liczy się jeszcze Slack (AI czyta kanały daily). Dzień przełączenia na „tylko OS” ustawiasz na stronie Zespół. Kontrakt danych: repo OS, `docs/DAILY_REPORTS_FOR_AGENTS.md`.
- **Finanse (CFO Studio)**: karta „Finanse” na Pulpicie (wynik realny ostatniego zamkniętego miesiąca vs księgi Incro, marża I 3M, prognoza bieżącego miesiąca, koszt zamówień w produkcji, alerty CFO), sekcja „Finanse (CFO Studio)” w raportach (włączasz w Studio) i narzędzia `cfo_*` w MCP — w Claude wystarcza jeden konektor (Agents). Liczby i logika zostają w CFO Studio (mybed-cfo-studio.vercel.app); Agents czyta je przez serwer MCP CFO Studio kluczem API (`CFO_STUDIO_API_KEY`). Tylko Delta Industries (MyBed) — bez MittoHome i NOMO.
- **Liczby w raportach**: tabela KPI (okresy 1/3/7/30 dni, sklepy, Meta i Google Ads, zmiana % do poprzedniego okresu) liczona kodem z hurtowni dash, nie przez AI. Okresy i wskaźniki wybierasz w Studio.
- **Skąd dane**: strona w panelu, która pokazuje dla każdego źródła, co czytamy, jak często i które sekcje raportów z niego korzystają.
- **Serwer MCP** dla Claude.ai: pytania do całej bazy wiedzy z czatu **i zmiany z czatu** — „co mam dziś” (`today_view`), dodaj / zamknij / odłóż sprawę (`add_item`, `close_item`, `update_item`), „umowa Eli podpisana” (`add_note` — notatka sama zamyka pasujące sprawy i jest rozstrzygająca dla raportów), „odpisałem mu” (`mark_handled`), ustawienia raportów (`get_report_settings`, `update_report_settings`, `run_report`), reklamy per marka (`get_marketing`), przypisanie konta reklamowego (`set_ad_account`), stan danych (`get_data_health`), finanse Delty z CFO Studio (`cfo_overview`, `cfo_guide`, `cfo_list_datasets`, `cfo_get_dataset`, `cfo_forecast`, `cfo_order`, `cfo_invoices` — z sumą, grupowaniem i filtrem producenta, `cfo_producer_check` — zlecone vs wydane vs zafakturowane vs kontrola faktur, `cfo_describe`, `cfo_sql`, oraz `cfo_tools` + `cfo_call` do każdego narzędzia CFO Studio, także nowych, bez wdrażania Agents). Połączenie wymaga logowania i zgody w panelu.
- **Marki i sklepy**: MyBed = mybed.pl, mybed.de, mojepostel.cz (Shoper -1/-2/-3); MittoHome = mittohome.pl; NOMO = nomosleep.pl. Spółki (Delta Industries, All Good Things) nigdy nie są traktowane jak sklep — sklep bierzemy z numeru zamówienia. Konta Meta → sklep: `src/connectors/dash.py` (`META_ACCOUNT_SHOP`) + własne przypisania z MCP (`set_ad_account`); konto bez przypisania raport pokazuje osobno.

Wygląd: design system MyBed Group OS (`pipeline/docs/DESIGN.md`) — Geist, indygo + cyjan, gęsty panel, jasny/ciemny motyw, mobile.

## Architektura

```
whatsapp-bridge (Node, Baileys) ──► scheduler (Python) ──► Postgres (events, chat_digests, commitments, reports, settings)
Gmail / Calendar (Google API) ───►      │                        ▲
MyBed OS (Supabase os_entities) ◄──────┤                        │
dash (Supabase fact_daily_*) ──────────┘            agents (FastAPI: panel + MCP) ◄── Caddy (HTTPS)
CFO Studio (MCP /api/mcp, klucz API) ──────────────────────►  karta Finanse, sekcja raportów, narzędzia cfo_*
```

| Kontener | Co robi |
|---|---|
| `agents` | panel WWW, API, MCP (`/mcp/mcp`), OAuth |
| `scheduler` | synchronizacje (WhatsApp co 5 min, Gmail i Slack co 10 min, kalendarz co 30 min), streszczenia czatów co godzinę 8–20, „na żywo” co 15 min 7–20 (AI czyta nowe wątki mailowe i zamyka sprawy załatwione w innych kanałach), raporty dnia zespołu (8:50, 12:50, 16:50–19:50), raporty wg Studio, alarm „WhatsApp rozłączony” |
| `whatsapp-bridge` | połączenie z WhatsApp jako „połączone urządzenie”; łączenie kodem QR albo kodem parowania z panelu (**Źródła → WhatsApp**) |
| `postgres` | baza wiedzy (port tylko na 127.0.0.1) |
| `caddy` | HTTPS dla `agents.mybed.pl` |

## Wdrożenie / aktualizacja

```bash
git pull
cp -n .env.example .env        # przy pierwszej instalacji; potem uzupełnij nowe zmienne (niżej)
docker compose up -d --build
```

Potem w panelu: **Źródła → WhatsApp** → zeskanuj kod QR (albo „Połącz numerem telefonu”).

### Nowe zmienne w `.env` (względem poprzedniej wersji)

| Zmienna | Skąd |
|---|---|
| `BRIDGE_TOKEN` | `openssl rand -hex 32` — wspólny sekret agentów i bridge'a |
| `OS_SUPABASE_SERVICE_KEY` | Supabase → projekt **mybed-group-os** → Settings → API keys → secret (service_role) |
| `DASH_SUPABASE_SERVICE_KEY` | Supabase → projekt z hurtownią (dash) → Settings → API keys → secret |
| `PANEL_SUPABASE_SERVICE_KEY` | Supabase (organizacja Mybed2) → projekt **panel-zamowien** → Settings → API keys → secret. Dzisiejsza sprzedaż na żywo z API sklepów (co 10 min). Agenci tylko czytają `sklep_zamowienia` (pola zamówienia, bez klientów) i `sklepy`. Sprawdzenie po wdrożeniu: `python -m src.main panel-check` |
| `CFO_STUDIO_API_KEY` | CFO Studio → **MCP / czat** → **Klucze API dla aplikacji** → Utwórz klucz (`cfk_…`, widoczny raz; zakres pełny albo „bez danych bankowych i SQL”). Opcjonalnie `CFO_STUDIO_URL` (domyślnie `https://mybed-cfo-studio.vercel.app`). Bez klucza karta i sekcja Finanse się nie pokazują |
| `RESEND_API_KEY`, `EMAIL_FROM` | to samo konto Resend co w MyBed OS (albo `SMTP_*`) |
| `PUBLIC_BASE_URL` | `https://agents.mybed.pl` — linki w mailach |
| `DASHBOARD_PASSWORD` | wymagane, nie ma już domyślnego `admin/admin` |

Usunięte: `ASANA_*`, `SHOPER_*`, `SHOPIFY_*`.

### Slack (raporty na Slacka + dzienne raporty zespołu w okresie przejściowym)

1. `SLACK_BOT_TOKEN` w `.env` — token bota „Daily Agent” (api.slack.com/apps → OAuth & Permissions → Bot User OAuth Token, `xoxb-…`).
2. Uprawnienia bota (Bot Token Scopes): `chat:write`, `channels:read`, `groups:read`, `channels:history`, `groups:history`, `users:read` (opcjonalnie `users:read.email`). Po dodaniu: „Reinstall to Workspace”. Panel **Źródła → Slack** pokazuje, czego brakuje.
3. Na każdym kanale z raportami dnia wpisz `/invite @Daily Agent` (kanały są prywatne). Kanał raportów (np. `#morning-briefing`) ustaw w **Studio raportów**; przycisk „Wyślij test” sprawdza wysyłkę.

## Komendy

```bash
python -m src.main report morning_briefing          # wygeneruj i wyślij teraz
python -m src.main report weekly_review --no-deliver
python -m src.main panel-check                      # dzisiejsza sprzedaż z API sklepów (nowy ERP) obok dash
python -m src.main job chat_digests                 # whatsapp_sync | gmail_sync | calendar_sync | slack_sync | team_updates | os_people_sync | topic_extraction | ideaerp_sync | whatsapp_watchdog | live_refresh
npm run css                                          # przebuduj CSS po zmianie szablonów (Docker robi to sam)
pytest                                               # testy jednostkowe
```

## Bezpieczeństwo

- Panel: podpisane ciasteczka sesji (30 dni), brak domyślnego hasła.
- MCP / OAuth: `/authorize` wymaga zalogowania i kliknięcia „Zezwól”. Tokeny są losowe, w bazie trzymane jako hash, refresh token jest rotowany. Odwołujesz je w **System → Połączenia MCP**. Statyczny `MCP_API_KEY` działa dalej dla skryptów.
- Bridge WhatsApp nie wystawia portu na zewnątrz i wymaga `BRIDGE_TOKEN`.
- Treść od AI renderowana w panelu i mailach jest escapowana, przechodzą tylko linki http(s)/mailto.
