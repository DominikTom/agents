"""Where report data comes from — one registry for the "Skąd dane" page and the sources line in reports.

Keep this in sync with the ingestion jobs (src/jobs.py, src/scheduler.py) and the section builders
(src/reports/sections.py). Everything here is shown to the CEO verbatim, so it is written in Polish.
"""

from __future__ import annotations

# key -> description of one upstream system
SOURCES: dict[str, dict] = {
    "whatsapp": {
        "label": "WhatsApp",
        "icon": "message-circle",
        "what": "Wiadomości z Twojego WhatsAppa (czaty prywatne i grupy), czytane jako „połączone urządzenie”. "
                "Tylko odczyt — agenci nic nie wysyłają. Czaty wyłączone w Rozmowach są pomijane.",
        "how": "Bridge (Baileys) trzyma połączenie; co 5 min nowe wiadomości trafiają do bazy agentów. "
               "Głosówki i pliki są oznaczane (bez transkrypcji).",
        "cadence": "co 5 min",
        "processing": "Raz na godzinę (8–20) AI streszcza każdy aktywny czat z danego dnia: streszczenie, ustalenia, "
                      "otwarte kwestie, czy ktoś czeka na Twoją odpowiedź, sprawy (decyzja / do zrobienia / odpowiedź / "
                      "czekasz na). Co 15 min AI szuka dowodów załatwienia w dalszym ciągu rozmowy, w INNYCH czatach, "
                      "w mailach i w Twoich notatkach z czatu z Claude — i zamyka załatwione, nieaktualne i zdublowane.",
        "job": "whatsapp_sync",
        "health": "whatsapp",
    },
    "gmail": {
        "label": "Gmail",
        "icon": "mail",
        "what": "Skrzynka dominik.tomaszczyk@mybed.pl: nadawca, temat, treść. Newslettery i automaty oznaczane jako masowe.",
        "how": "Google API (konto serwisowe z delegacją), wątki z ostatnich godzin; po przerwie dociąga do 3 dni wstecz.",
        "cadence": "co 10 min",
        "processing": "Co 15 min (7–20) AI czyta każdy nowy wątek, w którym jesteś adresatem albo nadawcą: streszczenie, "
                      "czy ktoś czeka na Ciebie i sprawy (decyzje, zadania, odpowiedzi) — trafiają do planu dnia i "
                      "zobowiązań. Mail, na który odpowiedziałeś w wątku, innym mailem albo na WhatsAppie, nie wisi w "
                      "„Czeka na Twoją odpowiedź”.",
        "job": "gmail_sync",
        "health": "gmail",
    },
    "calendar": {
        "label": "Kalendarz Google",
        "icon": "calendar",
        "what": "Twoje wydarzenia: dziś i 10 dni do przodu (godziny, uczestnicy, Twoja odpowiedź na zaproszenie).",
        "how": "Google Calendar API; usunięte i przesunięte wydarzenia są aktualizowane.",
        "cadence": "co 30 min",
        "processing": "Sekcja Kalendarz łączy spotkania z wątkami z czatów, maili i OS (co przygotować).",
        "job": "calendar_sync",
        "health": "calendar",
    },
    "os": {
        "label": "MyBed Group OS",
        "icon": "layout-grid",
        "what": "Zadania, projekty, blokery, decyzje, ludzie oraz dzienne raporty zespołu z Daily Update "
                "(tabela os_entities w Supabase OS, kolekcja dailyUpdates).",
        "how": "Odczyt na żywo przy każdym raporcie (klucz serwisowy). Zapis tylko po Twojej akcji w panelu: "
               "prywatne zadanie ze zobowiązania albo propozycja AI.",
        "cadence": "na żywo przy raporcie; ludzie co 6 h",
        "processing": "Twoje zadania (zaległe, na dziś), obciążenie zespołu, blokery, projekty wymagające uwagi, decyzje. "
                      "Raporty dnia: analizę AI i decyzje o zmianach robi OS — agenci tylko je czytają, liczą braki "
                      "raportów (uczestnicy z ustawień Daily Update, dni robocze, od 10:00 następnego dnia) i eskalacje.",
        "job": "os_people_sync",
        "health": None,
    },
    "dash": {
        "label": "Hurtownia dash",
        "icon": "shopping-cart",
        "what": "Sprzedaż per sklep (fact_daily_revenue: mybed.pl, mybed.de, mojepostel.cz — w dash „shoper_unknown”, "
                "mittohome.pl, nomosleep.pl), wydatki Meta per konto reklamowe (fact_daily_adspend; konto → sklep, "
                "konto bez przypisania pokazujemy osobno), showroomy (sensmax_showroom_sales).",
        "how": "Supabase dash, odczyt przy raporcie. ETL po stronie dasha odświeża wczorajszy dzień ok. 6:00 — "
               "dlatego wszystkie okresy kończą się na wczoraj.",
        "cadence": "raz dziennie (ok. 6:00)",
        "processing": "Tabela „Liczby” i sekcje Sprzedaż / Marketing / Showroomy. Liczby liczy kod, nie AI. Marka = sklep "
                      "(MyBed: mybed.pl, mybed.de, mojepostel.cz; MittoHome; NOMO), nigdy spółka (All Good Things, "
                      "Delta Industries). Przychód = zamówienia złożone; kwota opłacona jest obok.",
        "job": None,
        "health": None,
    },
    "ga4": {
        "label": "GA4 → Google Ads",
        "icon": "chart-line",
        "what": "Koszt Google Ads per domena (fact_daily_traffic, wiersz __total__): mybed.pl, mybed.de, mittohome.pl, nomosleep.pl.",
        "how": "Z hurtowni dash. mybed.de raportuje w EUR — przeliczamy na PLN kursem z danego dnia.",
        "cadence": "raz dziennie (ok. 6:00)",
        "processing": "Kolumna „Google Ads” w tabeli wydatków per sklep.",
        "job": None,
        "health": None,
    },
    "panel": {
        "label": "Panel zamówień (nowy ERP)",
        "icon": "store",
        "what": "Zamówienia prosto z API sklepów: mybed.pl, mybed.de, mojepostel.cz (Shoper), mittohome.pl (Shopify), "
                "nomosleep.pl (Medusa) — tabela sklep_zamowienia w Supabase panel-zamowien. Tylko pola zamówienia "
                "(sklep, data, status, opłacone, kwota, waluta), bez danych klientów.",
        "how": "Panel pobiera sklepy co 10 min; agenci czytają przy raporcie, na stronie Dziś i w MCP. Waluta sklepu "
               "przeliczana kursem NBP (mojepostel.cz sprzedaje w CZK). Komplet dla wszystkich sklepów od 9.11.2026.",
        "cadence": "co 10 min",
        "processing": "„Dziś do tej pory” w podsumowaniu dnia, na stronie Dziś i w MCP; wczorajsza sprzedaż, gdy dash "
                      "jeszcze się nie załadował.",
        "job": None,
        "health": None,
    },
    "cfo_studio": {
        "label": "CFO Studio (finanse Delty)",
        "icon": "chart-line",
        "what": "Finanse spółki Delta Industries (MyBed): wynik realny zamkniętych miesięcy vs księgi Incro, marża I, prognoza "
                "bieżącego miesiąca, przychód MTD, koszt zamówień w produkcji, alerty CFO; w MCP także zestawy danych, "
                "zamówienia, faktury z Fakturowni i zapytania SQL. Bez MittoHome i NOMO (All Good Things).",
        "how": "Serwer MCP CFO Studio (mybed-cfo-studio.vercel.app/api/mcp) z kluczem API aplikacji (CFO_STUDIO_API_KEY w .env). "
               "Logika i uprawnienia zostają w CFO Studio — klucz „bez danych bankowych” nie widzi wyciągów ani SQL.",
        "cadence": "przy otwarciu Pulpitu (pamięć 10 min), przy raporcie, w MCP i w poniedziałki o 7:00 (audyt)",
        "processing": "Karta „Finanse” na Pulpicie, sekcja „Finanse (CFO Studio)” w raportach (włączasz w Studio), "
                      "narzędzia cfo_* w MCP Agentów. Wynik realny = księgi Incro z kosztem produkcji wg cennika zamiast "
                      "faktur producentów i transportem z kalkulatora panelu. W poniedziałki audyt dokumentów Delty i AGT "
                      "(audit_runs, audit_report, audit_findings) → mail do księgowości z plikami CSV i raport CFO "
                      "(Studio raportów → Audyt CFO); w raporcie CFO też most od wyniku do gotówki i podatki do zapłaty "
                      "(cash_bridge_monthly, tax_reserve, tax_payments — szacunek).",
        "job": "weekly_audit",
        "health": None,
    },
    "ideaerp": {
        "label": "IdeaERP",
        "icon": "store",
        "what": "Zapasowe źródło dzisiejszych zamówień, gdy panel zamówień nie jest skonfigurowany.",
        "how": "API IdeaERP; sklep rozpoznajemy po numerze zamówienia (Shoper…-1/-2/-3, Shopify…, NOMO-…), nie po spółce w ERP.",
        "cadence": "co godzinę (:05)",
        "processing": "Podsumowanie dnia i tygodnia pokazują „dziś do tej pory”.",
        "job": "ideaerp_sync",
        "health": None,
    },
    "slack": {
        "label": "Slack",
        "icon": "hash",
        "what": "Kanały, na których jest bot Daily Agent i które zaznaczysz w Źródła → Slack — przede wszystkim "
                "dzienne raporty zespołu (#dev-daily-updates, #kamila-daily-update itd.).",
        "how": "Slack API (bot). Nowe wiadomości co 10 min; kanały prywatne wymagają zaproszenia bota.",
        "cadence": "co 10 min",
        "processing": "Okres przejściowy: AI czyta raport ze Slacka tylko osób, które danego dnia nie napisały raportu "
                      "w MyBed OS — co zrobiły, w toku, plan, blokery, dopasowanie do zadań w OS. Od dnia przełączenia "
                      "(panel → Zespół) raporty dnia pochodzą wyłącznie z OS.",
        "job": "slack_sync",
        "health": "slack",
    },
}

# report section -> sources it reads ("ai" = synthesis over everything above)
SECTION_SOURCES: dict[str, list[str]] = {
    "top": ["ai"],
    "sales": ["dash", "panel", "ideaerp"],
    "marketing": ["dash", "ga4"],
    "showrooms": ["dash"],
    "finance": ["cfo_studio"],
    "calendar": ["calendar"],
    "awaiting": ["whatsapp", "gmail"],
    "commitments": ["whatsapp", "gmail"],
    "chats": ["whatsapp"],
    "email": ["gmail"],
    "os_me": ["os"],
    "team": ["os"],
    "team_updates": ["os", "slack"],
    "projects": ["os"],
    "topics": ["whatsapp", "gmail", "os"],
    "slack": ["slack"],
    "risks": ["ai"],
    "plan": ["ai"],
}

AI_STEPS = [
    ("Zbieranie", "Synchronizacje zapisują wiadomości, maile i wydarzenia w bazie agentów (Postgres na serwerze). "
                  "OS i dash są czytane na żywo."),
    ("Streszczanie", "Claude Sonnet streszcza czaty (raz na godzinę), dzienne raporty zespołu i wątki przekrojowe. "
                     "Każde streszczenie ma źródło — czat, dzień, osobę."),
    ("Liczby", "Tabela „Liczby” jest liczona kodem wprost z hurtowni — AI jej nie pisze i nie może zmienić."),
    ("Raport", "Claude Opus dostaje tylko dane włączonych sekcji (Studio raportów) i pisze syntezę: co ważne, dlaczego, "
               "co zrobić. Zasada: nie wymyślaj faktów — jeśli czegoś nie ma w danych, nie pisze tego."),
    ("Rekomendacje", "Priorytety wynikają z wpływu na biznes, pilności i tego, kto czeka na Ciebie. Twoje instrukcje "
                     "z Studio („O mnie”, VIP, wskazówki do sekcji) mają pierwszeństwo."),
]


def sources_for_sections(sections: list[str]) -> list[str]:
    seen: list[str] = []
    for s in sections:
        for src in SECTION_SOURCES.get(s, []):
            if src != "ai" and src not in seen:
                seen.append(src)
    return seen


def sources_line(sections: list[str], kpi_as_of: str | None = None) -> str:
    labels = [SOURCES[s]["label"] for s in sources_for_sections(sections)]
    if kpi_as_of and "Hurtownia dash" not in labels:
        labels.append("Hurtownia dash")
    line = ", ".join(labels)
    if kpi_as_of:
        from datetime import date

        line += f" · liczby do {date.fromisoformat(kpi_as_of).strftime('%d.%m')}"
    return line
