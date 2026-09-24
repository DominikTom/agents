"""Report profiles — what each report contains, when it runs and where it goes.

Stored in the `settings` table (key "reports") and edited in the panel
(Studio raportów). Defaults below are merged in, so new sections appear
automatically after an upgrade without overwriting user choices.
"""

from __future__ import annotations

import copy

from src.ai.client import REPORT_MODEL

# key -> label, description (shown in Studio), default writing instruction, reports where it makes sense
SECTION_CATALOG: dict[str, dict] = {
    "top": {
        "label": "Najważniejsze teraz",
        "description": "3–5 rzeczy, którymi warto się zająć najpierw, z krótkim uzasadnieniem.",
        "instruction": "3–5 numerowanych punktów: co zrobić i dlaczego teraz. Wybieraj na podstawie wpływu na biznes, pilności i tego, kto czeka na Dominika. Każdy punkt ma konkret (kto, co, do kiedy).",
        "data": False,
    },
    "sales": {
        "label": "Sprzedaż",
        "description": "Zamówienia i przychód per sklep z porównaniami (dash), a po południu też wynik bieżącego dnia.",
        "instruction": "Tabela lub krótka lista per sklep: zamówienia, przychód, zmiana vs dzień wcześniej i vs ten sam dzień tydzień temu. Potem 1–2 zdania interpretacji (co odstaje i możliwy powód). Wyróżnij spadki powyżej 25%.",
        "data": True,
    },
    "marketing": {
        "label": "Marketing i reklamy",
        "description": "Wydatki reklamowe per sklep (mybed.pl, mybed.de, MittoHome), osobno Meta i Google Ads.",
        "instruction": "Tabela: wiersz = sklep (mybed.pl, mybed.de, mittohome.pl), kolumny: Meta, Google Ads, Suma — za wczoraj i za 7 dni (w zł). Pod tabelą wiersz „Razem”. Nie licz MER ani ROAS. Potem 1–2 zdania tylko o tym, co odstaje (np. wydatki sklepu rosną, a sprzedaż spada; platforma z zerem, choć zwykle wydaje).",
        "data": True,
    },
    "showrooms": {
        "label": "Showroomy",
        "description": "Sprzedaż showroomów z ostatnich 7 dni.",
        "instruction": "Krótko: które showroomy ciągną, które odstają (7 dni).",
        "data": True,
    },
    "calendar": {
        "label": "Kalendarz",
        "description": "Spotkania (dziś / jutro / w przyszłym tygodniu) i co przygotować.",
        "instruction": "Spotkania z godzinami. Przy ważnych dopisz, co przygotować, łącząc to z wątkami z czatów, maili i OS (np. otwarte kwestie z tą osobą).",
        "data": True,
    },
    "awaiting": {
        "label": "Czeka na Twoją odpowiedź",
        "description": "WhatsApp i maile, w których ktoś czeka na Ciebie — z tym, czego oczekuje.",
        "instruction": "Lista od najdłużej czekających / najważniejszych: kto, w jakiej sprawie, czego oczekuje, od ilu godzin czeka. Pomiń sprawy błahe. Jeśli nic ważnego nie czeka — napisz to jednym zdaniem.",
        "data": True,
    },
    "commitments": {
        "label": "Zobowiązania",
        "description": "Co obiecałeś, o co Cię proszono i co inni obiecali Tobie.",
        "instruction": "Dwie krótkie grupy: „Twoje” (obiecane przez Dominika + prośby do niego) i „Czekasz na” (obietnice innych). Najpierw zaległe i z terminem na dziś.",
        "data": True,
    },
    "chats": {
        "label": "Z rozmów (WhatsApp)",
        "description": "Najważniejsze sprawy z czatów — streszczenia, decyzje, otwarte kwestie.",
        "instruction": "Pogrupuj po sprawach, nie po czatach. Dla każdej sprawy: co się dzieje, ustalenia, co jest otwarte. Pomiń rozmowy o niskim znaczeniu.",
        "data": True,
    },
    "email": {
        "label": "Poczta",
        "description": "Ważne maile z okresu raportu (bez newsletterów i automatów).",
        "instruction": "Tylko maile, które niosą informację lub wymagają działania: od kogo, o czym, co z tego wynika. Pomiń te już opisane w innych sekcjach.",
        "data": True,
    },
    "os_me": {
        "label": "Moje zadania (OS)",
        "description": "Twoje zadania z MyBed OS: zaległe, na dziś i najbliższe.",
        "instruction": "Zaległe i na dziś z terminami. Zasugeruj, co można przesunąć albo delegować.",
        "data": True,
    },
    "team": {
        "label": "Zespół (OS)",
        "description": "Obciążenie i zaległości zespołu, blokery, decyzje czekające na Ciebie.",
        "instruction": "Kto ma najwięcej zaległości (z przykładami), aktywne blokery z następnym krokiem, decyzje do podjęcia. Wskaż, gdzie Dominik powinien zainterweniować.",
        "data": True,
    },
    "projects": {
        "label": "Projekty (OS)",
        "description": "Projekty wymagające uwagi: czerwone/żółte, po terminie, bez aktualizacji.",
        "instruction": "Tylko projekty z problemem: nazwa, właściciel, co jest nie tak, proponowany ruch.",
        "data": True,
    },
    "topics": {
        "label": "Wątki przekrojowe",
        "description": "Sprawy, które pojawiają się jednocześnie w czatach, mailach i OS.",
        "instruction": "Połącz informacje z różnych źródeł w jeden obraz sprawy. Tylko sprawy aktywne i istotne.",
        "data": True,
    },
    "slack": {
        "label": "Slack",
        "description": "Wiadomości z obserwowanych kanałów Slack.",
        "instruction": "Tylko to, co ważne dla CEO.",
        "data": True,
    },
    "risks": {
        "label": "Sygnały i ryzyka",
        "description": "Rzeczy, które AI zauważyło: trendy, powtarzające się problemy, szanse.",
        "instruction": "2–5 obserwacji, których nie widać na pierwszy rzut oka: powtarzające się problemy, rosnące opóźnienia, rozjazd między tym, co mówią ludzie, a stanem w OS. Każda z sugestią działania.",
        "data": False,
    },
    "plan": {
        "label": "Plan",
        "description": "Proponowany plan (dnia / jutra / przyszłego tygodnia) w kolejności priorytetów.",
        "instruction": "Numerowana lista 3–7 kroków w kolejności wykonania, każdy z uzasadnieniem z danych.",
        "data": False,
    },
}

LENGTHS = {
    "short": "Bardzo zwięźle: cały raport ma dać się przeczytać w 2 minuty. Maksymalnie 3 punkty na sekcję.",
    "standard": "Zwięźle, ale z kontekstem: raport do przeczytania w 4–5 minut.",
    "detailed": "Szczegółowo: pełny kontekst i wnioski, raport może mieć do 10 minut czytania.",
}

DAY_NAMES = ["Pn", "Wt", "Śr", "Cz", "Pt", "Sb", "Nd"]


def _sections(keys_on: list[str], keys_off: list[str]) -> list[dict]:
    return [{"key": k, "enabled": True, "note": ""} for k in keys_on] + [
        {"key": k, "enabled": False, "note": ""} for k in keys_off
    ]


DEFAULT_PROFILES: dict[str, dict] = {
    "morning_briefing": {
        "name": "Poranny briefing",
        "kind": "morning",
        "enabled": True,
        "time": "07:00",
        "days": [0, 1, 2, 3, 4],
        "email": True,
        "slack_channel": "",
        "model": REPORT_MODEL,
        "length": "standard",
        "instructions": "",
        "sections": _sections(
            ["top", "awaiting", "calendar", "sales", "marketing", "chats", "commitments", "os_me", "team", "email"],
            ["projects", "showrooms", "topics", "risks", "plan", "slack"],
        ),
    },
    "daily_wrap": {
        "name": "Podsumowanie dnia",
        "kind": "wrap",
        "enabled": True,
        "time": "16:00",
        "days": [0, 1, 2, 3, 4],
        "email": True,
        "slack_channel": "",
        "model": REPORT_MODEL,
        "length": "standard",
        "instructions": "",
        "sections": _sections(
            ["top", "topics", "chats", "awaiting", "commitments", "sales", "calendar", "risks", "plan"],
            ["email", "marketing", "os_me", "team", "projects", "showrooms", "slack"],
        ),
    },
    "weekly_review": {
        "name": "Podsumowanie tygodnia",
        "kind": "weekly",
        "enabled": True,
        "time": "15:00",
        "days": [4],
        "email": True,
        "slack_channel": "",
        "model": REPORT_MODEL,
        "length": "detailed",
        "instructions": "",
        "sections": _sections(
            ["top", "sales", "marketing", "showrooms", "projects", "team", "topics", "chats", "commitments", "risks", "calendar", "plan"],
            ["awaiting", "email", "os_me", "slack"],
        ),
    },
}

DEFAULT_GENERAL = {
    "owner_name": "Dominik Tomaszczyk",
    "recipients": ["dominik.tomaszczyk@mybed.pl"],
    "about": (
        "Jestem CEO i założycielem MyBed Group (MyBed, MittoHome, NOMO/NomoSleep, LepszySen.pl). "
        "Zarządzam zespołem ok. 30 osób: sprzedaż i B2B, marketing i kreacja, IT/e-commerce, "
        "operacje i logistyka, finanse. Zadania i projekty prowadzimy w MyBed Group OS."
    ),
    "vip": [],
    "push_to_os_suggestions": False,
}


def merge_profiles(stored: dict | None) -> dict[str, dict]:
    """Stored profiles on top of defaults; unknown sections dropped, new ones appended (off)."""
    out: dict[str, dict] = {}
    stored = stored or {}
    for key, default in DEFAULT_PROFILES.items():
        p = copy.deepcopy(default)
        p.update({k: v for k, v in (stored.get(key) or {}).items() if k != "sections"})
        if stored.get(key, {}).get("sections"):
            seen = set()
            sections = []
            for s in stored[key]["sections"]:
                if s.get("key") in SECTION_CATALOG and s["key"] not in seen:
                    seen.add(s["key"])
                    sections.append({"key": s["key"], "enabled": bool(s.get("enabled")), "note": s.get("note", "")})
            for s in default["sections"]:
                if s["key"] not in seen:
                    sections.append({**s, "enabled": False})
            p["sections"] = sections
        p["key"] = key
        out[key] = p
    return out


def merge_general(stored: dict | None) -> dict:
    g = copy.deepcopy(DEFAULT_GENERAL)
    g.update(stored or {})
    return g


async def load_profiles(db) -> dict[str, dict]:
    return merge_profiles(await db.get_setting("reports", {}))


async def load_general(db) -> dict:
    return merge_general(await db.get_setting("general", {}))


def schedule_label(p: dict) -> str:
    days = p.get("days") or []
    if days == [0, 1, 2, 3, 4]:
        d = "Pn–Pt"
    elif days == list(range(7)):
        d = "codziennie"
    else:
        d = ", ".join(DAY_NAMES[i] for i in days)
    return f"{d}, {p.get('time', '')}"
