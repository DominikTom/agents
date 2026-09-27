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
                      "otwarte kwestie, czy ktoś czeka na Twoją odpowiedź, zobowiązania.",
        "job": "whatsapp_sync",
        "health": "whatsapp",
    },
    "gmail": {
        "label": "Gmail",
        "icon": "mail",
        "what": "Skrzynka dominik.tomaszczyk@mybed.pl: nadawca, temat, treść. Newslettery i automaty oznaczane jako masowe.",
        "how": "Google API (konto serwisowe z delegacją), wątki z ostatnich godzin; po przerwie dociąga do 3 dni wstecz.",
        "cadence": "co 10 min",
        "processing": "Maile, na które nie odpowiedziałeś, trafiają do „Czeka na Twoją odpowiedź”; ważne maile — do sekcji Poczta.",
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
        "what": "Zadania, projekty, blokery, decyzje i ludzie (tabela os_entities w Supabase OS).",
        "how": "Odczyt na żywo przy każdym raporcie (klucz serwisowy). Zapis tylko po Twojej akcji w panelu: "
               "prywatne zadanie ze zobowiązania albo propozycja AI.",
        "cadence": "na żywo przy raporcie; ludzie co 6 h",
        "processing": "Twoje zadania (zaległe, na dziś), obciążenie zespołu, blokery, projekty wymagające uwagi, decyzje.",
        "job": "os_people_sync",
        "health": None,
    },
    "dash": {
        "label": "Hurtownia dash",
        "icon": "shopping-cart",
        "what": "Sprzedaż per sklep (fact_daily_revenue), wydatki Meta per konto reklamowe (fact_daily_adspend), "
                "showroomy (sensmax_showroom_sales).",
        "how": "Supabase dash, odczyt przy raporcie. ETL po stronie dasha odświeża wczorajszy dzień ok. 6:00 — "
               "dlatego wszystkie okresy kończą się na wczoraj.",
        "cadence": "raz dziennie (ok. 6:00)",
        "processing": "Tabela „Liczby” i sekcje Sprzedaż / Marketing / Showroomy. Liczby liczy kod, nie AI.",
        "job": None,
        "health": None,
    },
    "ga4": {
        "label": "GA4 → Google Ads",
        "icon": "chart-line",
        "what": "Koszt Google Ads per domena (fact_daily_traffic, wiersz __total__): mybed.pl, mybed.de, mittohome.pl.",
        "how": "Z hurtowni dash. mybed.de raportuje w EUR — przeliczamy na PLN kursem z danego dnia.",
        "cadence": "raz dziennie (ok. 6:00)",
        "processing": "Kolumna „Google Ads” w tabeli wydatków per sklep.",
        "job": None,
        "health": None,
    },
    "ideaerp": {
        "label": "IdeaERP",
        "icon": "store",
        "what": "Dzisiejsze zamówienia i przychód (do tej pory) — jedyne źródło danych z bieżącego dnia.",
        "how": "API IdeaERP, metryki per sklep.",
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
        "processing": "AI czyta raport każdej osoby: co zrobiła, w toku, plan, blokery; dopasowuje to do jej zadań w OS "
                      "i oznacza brak raportu albo brak planu.",
        "job": "slack_sync",
        "health": "slack",
    },
}

# report section -> sources it reads ("ai" = synthesis over everything above)
SECTION_SOURCES: dict[str, list[str]] = {
    "top": ["ai"],
    "sales": ["dash", "ideaerp"],
    "marketing": ["dash", "ga4"],
    "showrooms": ["dash"],
    "calendar": ["calendar"],
    "awaiting": ["whatsapp", "gmail"],
    "commitments": ["whatsapp"],
    "chats": ["whatsapp"],
    "email": ["gmail"],
    "os_me": ["os"],
    "team": ["os"],
    "team_updates": ["slack", "os"],
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
