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
        "description": "Zamówienia i przychód per sklep (mybed.pl, mybed.de, mojepostel.cz, MittoHome, NOMO) z porównaniami (dash), a po południu też wynik bieżącego dnia.",
        "instruction": "1–3 zdania: co odstaje w sprzedaży per sklep (zmiana vs dzień wcześniej i vs ten sam dzień tydzień temu, spadki powyżej 25%) i możliwy powód. Tabelę dawaj tylko, gdy nie ma tabeli „Liczby”. Sklepy nazywaj jak w danych (mybed.pl, mybed.de, mojepostel.cz, MittoHome, NOMO) — nigdy nazwą spółki (All Good Things, Delta Industries).",
        "data": True,
    },
    "marketing": {
        "label": "Marketing i reklamy",
        "description": "Wydatki reklamowe per sklep i marka (MyBed: mybed.pl, mybed.de, mojepostel.cz; MittoHome; NOMO), osobno Meta i Google Ads.",
        "instruction": "Tabela: wiersz = sklep (mybed.pl, mybed.de, MittoHome, NOMO; mojepostel.cz tylko gdy wydaje), kolumny: Meta, Google Ads, Suma — za wczoraj i za 7 dni (w zł). Pod tabelą wiersz „Razem”. Nie licz MER ani ROAS. Potem 1–2 zdania tylko o tym, co odstaje. Jeśli w danych są „nieprzypisane_konta”, napisz wprost, które konto reklamowe wydaje i nie jest przypisane do sklepu — nie doliczaj go do żadnej marki.",
        "data": True,
    },
    "finance": {
        "label": "Finanse (CFO Studio)",
        "description": "Wynik realny ostatniego zamkniętego miesiąca vs księgi Incro i 3M, marża I, prognoza bieżącego miesiąca, koszt zamówień w produkcji i alerty — z CFO Studio (tylko Delta Industries / MyBed).",
        "instruction": "2–4 zdania: wynik realny ostatniego zamkniętego miesiąca vs Incro i krocząco 3M, marża I realna 3M vs księgi, prognoza bieżącego miesiąca (wynik i przychód, który to dzień miesiąca), koszt zamówień w produkcji; alerty CFO jednym zdaniem. Liczby bierz dokładnie z danych, nie przeliczaj. Nie oceniaj pojedynczego miesiąca bez 3M. To finanse spółki Delta Industries (MyBed: mybed.pl, mybed.de) — bez MittoHome i NOMO.",
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
    "team_updates": {
        "label": "Raporty dnia zespołu",
        "description": "Dzienne raporty ludzi z MyBed OS (Daily Update; w okresie przejściowym też ze Slacka): co dowieźli, plan, blokery, eskalacje, kto nie wysłał raportu.",
        "instruction": "Najpierw alarmy: eskalacje (prośby bez reakcji, zadania, które długo czekają, seria dni bez raportu, stare blokery), kto nie wysłał raportu, kto nie ma planu na kolejny dzień, realne blokady (szczególnie zależne od Dominika). Potem po jednej linii na osobę: co dowiozła i na czym się skupia. Na końcu: co zespół sam zmienił w OS po raporcie (zatwierdzone zmiany) oraz — dla raportów ze Slacka — rozjazd z OS i praca, której nie ma w OS. Nieobecność podawaj bez powodu. Nie przepisuj całych raportów i nie cytuj ich.",
        "data": True,
        "auto_enable": True,
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
    "short": "Bardzo krótko: cały tekst (bez tabeli „Liczby”) najwyżej ok. 250 słów — do przeczytania w minutę. "
             "Najwyżej 3 punkty na sekcję, każdy w jednej linii. Pustą sekcję zamknij jednym zdaniem.",
    "standard": "Zwięźle: cały tekst najwyżej ok. 500 słów, do przeczytania w 2–3 minuty. Najwyżej 5 punktów na sekcję.",
    "detailed": "Szczegółowo: pełny kontekst i wnioski, najwyżej ok. 1000 słów.",
}

DAY_NAMES = ["Pn", "Wt", "Śr", "Cz", "Pt", "Sb", "Nd"]

# KPI table at the top of a report — computed from dash, never written by the AI
KPI_METRICS = {
    "revenue": "Przychód",
    "orders": "Zamówienia",
    "aov": "Średni koszyk",
    "spend_total": "Reklamy razem",
    "spend_meta": "Meta",
    "spend_google": "Google Ads",
}
KPI_PERIODS = {"1": "Ostatni dzień", "3": "Ostatnie 3 dni", "7": "Ostatnie 7 dni", "30": "Ostatnie 30 dni"}


def _kpi(periods: list[str], metrics: list[str], per_shop: bool = True) -> dict:
    return {"enabled": True, "periods": periods, "metrics": metrics, "per_shop": per_shop}


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
        "wait_until": "09:00",
        "days": [0, 1, 2, 3, 4],
        "email": True,
        "slack_channel": "",
        "model": REPORT_MODEL,
        "length": "short",
        "instructions": "",
        "kpi": _kpi(["1", "7", "30"], ["revenue", "orders", "aov", "spend_meta", "spend_google", "spend_total"]),
        "sections": _sections(
            ["top", "calendar", "awaiting", "commitments", "sales", "marketing", "team_updates"],
            ["chats", "os_me", "team", "email", "projects", "showrooms", "topics", "risks", "plan", "slack", "finance"],
        ),
    },
    "daily_wrap": {
        "name": "Podsumowanie dnia",
        "kind": "wrap",
        "enabled": True,
        "time": "16:00",
        "wait_until": "",
        "days": [0, 1, 2, 3, 4],
        "email": True,
        "slack_channel": "",
        "model": REPORT_MODEL,
        "length": "short",
        "instructions": "",
        "kpi": _kpi(["1", "7"], ["revenue", "orders", "spend_total"], per_shop=False),
        "sections": _sections(
            ["top", "awaiting", "commitments", "calendar", "sales"],
            ["topics", "chats", "risks", "plan", "email", "marketing", "os_me", "team", "team_updates", "projects",
             "showrooms", "slack", "finance"],
        ),
    },
    "weekly_review": {
        "name": "Podsumowanie tygodnia",
        "kind": "weekly",
        "enabled": True,
        "time": "15:00",
        "wait_until": "",
        "days": [4],
        "email": True,
        "slack_channel": "",
        "model": REPORT_MODEL,
        "length": "standard",
        "instructions": "",
        "kpi": _kpi(["7", "30"], ["revenue", "orders", "aov", "spend_meta", "spend_google", "spend_total"]),
        "sections": _sections(
            ["top", "sales", "marketing", "finance", "showrooms", "projects", "team", "team_updates", "topics", "chats", "commitments", "risks", "calendar", "plan"],
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
            prev_key = None
            for s in default["sections"]:
                if s["key"] not in seen:
                    # sections added after the user saved stay off — unless the feature is meant to show up on its
                    # own; those go right after their neighbour from the default order
                    on = s["enabled"] and SECTION_CATALOG[s["key"]].get("auto_enable", False)
                    item = {**s, "enabled": on}
                    keys = [x["key"] for x in sections]
                    if on and prev_key in keys:
                        sections.insert(keys.index(prev_key) + 1, item)
                    else:
                        sections.append(item)
                    seen.add(s["key"])
                prev_key = s["key"]
            p["sections"] = sections
        kpi = {**default["kpi"], **(p.get("kpi") or {})}
        kpi["periods"] = [x for x in KPI_PERIODS if x in {str(v) for v in kpi.get("periods") or []}]
        kpi["metrics"] = [m for m in KPI_METRICS if m in set(kpi.get("metrics") or [])]
        p["kpi"] = kpi
        p["key"] = key
        out[key] = p
    return out


def merge_general(stored: dict | None) -> dict:
    g = copy.deepcopy(DEFAULT_GENERAL)
    g.update(stored or {})
    return g


# One-time changes of saved profiles, applied once per database (settings "reports_migrations").
# compact_2026_11: Dominik asked for shorter reports — saved profiles move to the new compact length and section
# set (instructions, times, recipients, notes of sections that stay, KPI choices are kept).
MIGRATIONS = ["compact_2026_11"]


def apply_compact_2026_11(stored: dict) -> dict:
    out = copy.deepcopy(stored)
    for key, p in out.items():
        if key not in DEFAULT_PROFILES or not isinstance(p, dict):
            continue
        default = DEFAULT_PROFILES[key]
        p["length"] = default["length"]
        notes = {s.get("key"): s.get("note", "") for s in p.get("sections") or []}
        p["sections"] = [{**s, "note": notes.get(s["key"], "")} for s in copy.deepcopy(default["sections"])]
    return out


async def migrate_profiles(db) -> list[str]:
    """Run pending one-time profile migrations; returns the names applied."""
    done = await db.get_setting("reports_migrations", []) or []
    pending = [m for m in MIGRATIONS if m not in done]
    if not pending:
        return []
    stored = await db.get_setting("reports", {}) or {}
    if stored and "compact_2026_11" in pending:
        await db.set_setting("reports", apply_compact_2026_11(stored))
    await db.set_setting("reports_migrations", done + pending)
    return pending


async def load_profiles(db) -> dict[str, dict]:
    try:
        await migrate_profiles(db)
    except Exception:  # a migration must never block reading the profiles
        pass
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
    wait = f" (czeka na dane do {p['wait_until']})" if p.get("wait_until") else ""
    return f"{d}, {p.get('time', '')}{wait}"
