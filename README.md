# MyBed Agents

AI chief of staff dla CEO MyBed Group. Czyta WhatsApp, Gmail, kalendarz, MyBed Group OS i hurtownię sprzedaży (dash). Z tego robi:

- **Poranny briefing, podsumowanie dnia i tygodnia** (piątek). Wysyła je e-mailem i zapisuje w panelu. Sekcje, kolejność, długość, godziny, model AI i własne instrukcje ustawiasz w **Studio raportów**.
- **Rozumienie czatów**: raz dziennie AI streszcza każdy aktywny czat (kontekst odpowiedzi, znaczniki głosówek i plików, role osób w firmie). Wychodzą z tego ustalenia, otwarte kwestie, informacja „czeka na Twoją odpowiedź” i zobowiązania.
- **Zobowiązania**: co obiecałeś, o co Cię proszono i co inni obiecali Tobie. Po zatwierdzeniu trafiają do MyBed OS jako prywatne zadanie.
- **Pulpit**: KPI sprzedaży, rozmowy i maile czekające na odpowiedź, kalendarz, zadania z OS.
- **Serwer MCP** dla Claude.ai, czyli pytania do całej bazy wiedzy z czatu. Połączenie wymaga logowania i zgody w panelu.

Wygląd: design system MyBed Group OS (`pipeline/docs/DESIGN.md`) — Geist, indygo + cyjan, gęsty panel, jasny/ciemny motyw, mobile.

## Architektura

```
whatsapp-bridge (Node, Baileys) ──► scheduler (Python) ──► Postgres (events, chat_digests, commitments, reports, settings)
Gmail / Calendar (Google API) ───►      │                        ▲
MyBed OS (Supabase os_entities) ◄──────┤                        │
dash (Supabase fact_daily_*) ──────────┘            agents (FastAPI: panel + MCP) ◄── Caddy (HTTPS)
```

| Kontener | Co robi |
|---|---|
| `agents` | panel WWW, API, MCP (`/mcp/mcp`), OAuth |
| `scheduler` | synchronizacje (WhatsApp co 5 min, Gmail co 10 min, kalendarz co 30 min), streszczenia czatów co godzinę 8–20, raporty wg Studio |
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
| `RESEND_API_KEY`, `EMAIL_FROM` | to samo konto Resend co w MyBed OS (albo `SMTP_*`) |
| `PUBLIC_BASE_URL` | `https://agents.mybed.pl` — linki w mailach |
| `DASHBOARD_PASSWORD` | wymagane, nie ma już domyślnego `admin/admin` |

Usunięte: `ASANA_*`, `SHOPER_*`, `SHOPIFY_*`.

## Komendy

```bash
python -m src.main report morning_briefing          # wygeneruj i wyślij teraz
python -m src.main report weekly_review --no-deliver
python -m src.main job chat_digests                 # whatsapp_sync | gmail_sync | calendar_sync | os_people_sync | topic_extraction | ideaerp_sync
npm run css                                          # przebuduj CSS po zmianie szablonów (Docker robi to sam)
pytest                                               # testy jednostkowe
```

## Bezpieczeństwo

- Panel: podpisane ciasteczka sesji (30 dni), brak domyślnego hasła.
- MCP / OAuth: `/authorize` wymaga zalogowania i kliknięcia „Zezwól”. Tokeny są losowe, w bazie trzymane jako hash, refresh token jest rotowany. Odwołujesz je w **System → Połączenia MCP**. Statyczny `MCP_API_KEY` działa dalej dla skryptów.
- Bridge WhatsApp nie wystawia portu na zewnątrz i wymaga `BRIDGE_TOKEN`.
- Treść od AI renderowana w panelu i mailach jest escapowana, przechodzą tylko linki http(s)/mailto.
