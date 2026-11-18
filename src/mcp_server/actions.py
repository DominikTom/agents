"""MCP tools that change things — Dominik's chat with Claude feeds the agents' knowledge base.

"Dodaj", "zamknij", "to jest ogarnięte", "zmień raport", "przypisz konto reklamowe do NOMO" — every change made
here is used right away by the day view, the panel and the next report. Read tools for the same data live next
to them (today, list_items, get_marketing…). Registered on the FastMCP instance from server.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

from src.common.timeutil import fmt_short, now, today

logger = logging.getLogger(__name__)

_background: set[asyncio.Task] = set()

KIND_LABEL = {"decyzja": "decyzja", "zadanie": "do zrobienia", "odpowiedz": "odpowiedź", "czekam": "czekasz na"}
SOURCE_LABEL = {"whatsapp": "WhatsApp", "gmail": "mail", "manual": "dodane"}


def item_line(c: dict) -> str:
    due = c["due_date"].strftime("%d.%m") if c.get("due_date") else (c.get("due_hint") or "")
    status = "" if c.get("status") == "open" else f" [{c.get('status')}]"
    snooze = f" · odłożone do {c['snoozed_until']:%d.%m}" if c.get("snoozed_until") and c["snoozed_until"] > today() else ""
    who = f" — {c['counterpart']}" if c.get("counterpart") else ""
    src = SOURCE_LABEL.get(c.get("source"), c.get("source"))
    where = f": {c['chat_name'][:60]}" if c.get("chat_name") and c.get("source") != "manual" else ""
    kind = KIND_LABEL.get(c.get("kind") or ("czekam" if c.get("direction") == "theirs" else "zadanie"), "do zrobienia")
    return (f"#{c['id']} [{kind}] {c['title']}{who}{' · termin ' + due if due else ''}{snooze} "
            f"({src}{where}, {fmt_short(c.get('source_at'))}){status}")


def register(mcp, get_db) -> None:
    """Attach the tools to the FastMCP server."""

    @mcp.tool()
    async def today_view() -> str:
        """Plan dnia Dominika NA ŻYWO: kalendarz, decyzje, odpowiedzi, zadania do zrobienia, na co czeka, zadania z
        MyBed OS i co już dziś załatwione. Każda sprawa ma #id (do close_item / update_item), a rozmowa czekająca
        na odpowiedź ma [kanał:ref] (do mark_handled). Użyj, gdy Dominik pyta „co mam dziś / co na mnie czeka”."""
        from src.processing.day_view import build_day, day_text

        return day_text(await build_day(get_db()))

    @mcp.tool()
    async def list_items(status: str = "open", search: str = "", kind: str = "", person: str = "", limit: int = 40) -> str:
        """Lista spraw (zobowiązania, prośby, decyzje, na co Dominik czeka) ze wszystkich źródeł.
        status: open | done | dismissed | all. search: słowa z tytułu/osoby (np. „umowa Eli”).
        kind: decyzja | zadanie | odpowiedz | czekam. person: imię/nazwisko drugiej osoby."""
        db = get_db()
        st = None if status == "all" else status
        if search:
            rows = await db.search_commitments(search, status=st, limit=min(limit, 50))
        else:
            rows = await db.list_commitments(status=st, limit=min(limit, 200))
        if kind:
            rows = [r for r in rows if (r.get("kind") or ("czekam" if r["direction"] == "theirs" else "zadanie")) == kind]
        if person:
            p = person.lower()
            rows = [r for r in rows if p in (r.get("counterpart") or "").lower()]
        if not rows:
            return "Brak spraw spełniających kryteria."
        return "\n".join(item_line(r) for r in rows[:limit])

    @mcp.tool()
    async def add_item(title: str, kind: str = "zadanie", due: str = "", person: str = "", note: str = "") -> str:
        """Dodaj sprawę do listy Dominika (pokaże się w planie dnia i raportach).
        title: w trybie rozkazującym („Zadzwoń do Comfy w sprawie płatności”). kind: zadanie | decyzja | odpowiedz |
        czekam (gdy Dominik czeka na kogoś). due: RRRR-MM-DD, DD.MM, „dziś” albo „jutro”. person: z kim."""
        from src.processing.items import add_manual_item

        try:
            c = await add_manual_item(get_db(), title, kind=kind, due=due or None, who=person or None, note=note or None)
        except ValueError as e:
            return f"Nie dodano: {e}"
        return "Dodano: " + item_line(c)

    @mcp.tool()
    async def close_item(item_id: int = 0, search: str = "", status: str = "done", comment: str = "") -> str:
        """Zamknij sprawę: status done (zrobione) albo dismissed (nieaktualne); status open = przywróć.
        Podaj item_id (#id z today_view / list_items) ALBO search (słowa z tytułu). Gdy search pasuje do kilku spraw,
        nic nie jest zamykane — dostaniesz kandydatów do wyboru. comment: krótko jak/gdzie załatwione."""
        from src.processing.items import find_items, set_status

        db = get_db()
        if status not in ("done", "dismissed", "open"):
            return "status: done | dismissed | open"
        if not item_id:
            if not search:
                return "Podaj item_id albo search."
            found = await find_items(db, search, status="open" if status != "open" else None)
            if not found:
                return f"Nie znalazłem otwartej sprawy dla „{search}”. Sprawdź list_items."
            top = found[0]["hits"] if found else 0
            best = [f for f in found if f["hits"] == top]
            if len(best) > 1:
                return ("Pasuje kilka spraw — nic nie zamknąłem. Wybierz id:\n" + "\n".join(item_line(f) for f in best[:8]))
            item_id = best[0]["id"]
        c = await set_status(db, item_id, status, comment or None)
        if not c:
            return f"Nie ma sprawy #{item_id}."
        verb = {"done": "Zamknięte jako zrobione", "dismissed": "Zamknięte jako nieaktualne", "open": "Przywrócone (zostaje otwarte na stałe)"}[status]
        return f"{verb}: {item_line(c)}"

    @mcp.tool()
    async def update_item(item_id: int, title: str = "", due: str = "", kind: str = "", person: str = "",
                          note: str = "", snooze_until: str = "") -> str:
        """Zmień sprawę #id: title, due (RRRR-MM-DD / DD.MM / „jutro”; „brak” usuwa termin), kind (zadanie | decyzja |
        odpowiedz | czekam), person, note (dopisek widoczny w planie dnia i raportach), snooze_until (odłóż: data
        albo „jutro” — do tego dnia sprawa znika z planu dnia i raportów)."""
        from src.processing.items import update_item as upd

        fields: dict = {}
        if title:
            fields["title"] = title
        if due:
            fields["due"] = "" if due.strip().lower() in ("brak", "usuń", "none") else due
        if kind:
            fields["kind"] = kind
        if person:
            fields["who"] = person
        if note:
            fields["note"] = note
        if snooze_until:
            fields["snooze"] = snooze_until
        try:
            c = await upd(get_db(), item_id, **fields)
        except ValueError as e:
            return f"Nie zmieniono: {e}"
        return ("Zmienione: " + item_line(c)) if c else f"Nie ma sprawy #{item_id}."

    @mcp.tool()
    async def add_note(text: str, about: str = "") -> str:
        """Zapisz to, co Dominik mówi o stanie spraw — fakt albo decyzję, np. „umowa Eli Janas podpisana”, „Comfy:
        płacimy w 3 ratach od 1.12”, „PayU dla NOMO ogarnięte”. Notatka: (1) od razu zamyka pasujące otwarte sprawy
        (zwraca, które), (2) raporty traktują ją jako rozstrzygającą i nie pokażą tematu jako otwartego.
        about: opcjonalnie czego dotyczy (osoba / temat)."""
        from src.processing.items import add_note as save

        try:
            res = await save(get_db(), text, about or None)
        except ValueError as e:
            return f"Nie zapisano: {e}"
        out = f"Zapisano notatkę #{res['note']['id']}."
        if res["closed"]:
            out += "\nZamknięte na jej podstawie:\n" + "\n".join(
                f"  #{c['id']} {c['title']} — {c['reason']}" for c in res["closed"])
        else:
            out += " Żadna otwarta sprawa nie została przez nią zamknięta (jeśli powinna — użyj close_item)."
        return out

    @mcp.tool()
    async def list_notes(days: int = 14) -> str:
        """Notatki Dominika z ostatnich dni (fakty i decyzje, które raporty traktują jako rozstrzygające)."""
        rows = await get_db().list_notes(since=now() - timedelta(days=days), limit=100)
        if not rows:
            return "Brak notatek."
        return "\n".join(f"n{r['id']} · {fmt_short(r['created_at'])} · {r['text']}" + (f" (dotyczy: {r['about']})" if r.get("about") else "")
                         for r in rows)

    @mcp.tool()
    async def delete_note(note_id: int) -> str:
        """Wyłącz notatkę (np. pomyłka) — przestaje trafiać do raportów. Sprawy zamknięte przez nią przywróć close_item(status=open)."""
        ok = await get_db().deactivate_note(note_id)
        return "Notatka wyłączona." if ok else f"Nie ma notatki n{note_id}."

    @mcp.tool()
    async def mark_handled(channel: str = "", ref: str = "", person: str = "", comment: str = "") -> str:
        """Rozmowa WhatsApp albo wątek mailowy nie wymaga już odpowiedzi Dominika (odpisał gdzie indziej,
        zadzwonił, nic do odpowiedzi). channel: whatsapp | gmail i ref z today_view ([whatsapp:…] / [gmail:…]),
        ALBO person — imię/nazwa czatu/nadawcy z listy czekających. Nowa wiadomość od tej osoby znów pokaże rozmowę."""
        db = get_db()
        targets: list[tuple[str, str, str]] = []
        if channel and ref:
            if channel not in ("whatsapp", "gmail"):
                return "channel: whatsapp | gmail"
            targets.append((channel, ref, ref))
        elif person:
            p = person.lower()
            for w in await db.get_whatsapp_awaiting(days=7, limit=100, hide_answered=False):
                if p in (w.get("chat_name") or "").lower() or p in (w.get("last_sender") or "").lower():
                    targets.append(("whatsapp", w["jid"], w.get("chat_name") or w["jid"]))
            for m in await db.get_email_awaiting(days=7, limit=100, hide_answered=False):
                meta = m["metadata"]
                names = " ".join(filter(None, [m.get("sender_display"), meta.get("sender_name"), meta.get("from")])).lower()
                if p in names and meta.get("thread_id"):
                    targets.append(("gmail", meta["thread_id"], f"mail „{m.get('title')}”"))
            if not targets:
                return f"Nic od „{person}” nie czeka na odpowiedź."
        else:
            return "Podaj channel + ref albo person."
        for kind, r, _ in targets:
            await db.mark_handled(kind, r, await db.last_message_at(kind, r) or now(), comment or None, by="mcp")
        return "Oznaczone jako obsłużone: " + ", ".join(label for _, _, label in targets)

    @mcp.tool()
    async def get_marketing(brand: str = "") -> str:
        """Wydatki reklamowe (Meta + Google Ads) za wczoraj i 7 dni, per sklep, marka i platforma, z hurtowni dash.
        Marki: MyBed (mybed.pl, mybed.de, mojepostel.cz), MittoHome (mittohome.pl), NOMO (nomosleep.pl).
        All Good Things / Delta Industries to spółki, nie sklepy. Pokazuje też konta reklamowe bez przypisanego sklepu."""
        from src.connectors.dash import DashClient

        client = await DashClient.from_settings(get_db())
        if not client.configured:
            return "Dash nie jest skonfigurowany (DASH_SUPABASE_SERVICE_KEY)."
        o = await client.overview()
        m = o["marketing"]
        rows = m["per_sklep"]
        if brand:
            b = brand.lower()
            rows = [r for r in rows if b in (r.get("marka") or "").lower() or b in r["shop"].lower()]
        out = {"dzien": o["date"], "per_sklep": rows, "per_marka": m.get("per_marka"), "per_platforma": m["per_platforma"],
               "razem_dzien": m["spend_day"], "razem_7_dni": m["spend_7d"], "zmiana_7_dni_pct": m["spend_7d_vs_prev_pct"],
               "brak_danych": m.get("brak_danych"), "zrodla": m.get("zrodla")}
        if m.get("nieprzypisane_konta"):
            out["nieprzypisane_konta"] = m["nieprzypisane_konta"]
            out["jak_przypisac"] = "set_ad_account(account_id, shop)"
        return json.dumps(out, ensure_ascii=False, indent=1)

    @mcp.tool()
    async def set_ad_account(account_id: str, shop: str) -> str:
        """Przypisz konto reklamowe Meta (act_…) do sklepu: mybed.pl | mybed.de | mojepostel.cz | mittohome.pl |
        nomosleep.pl. shop „-” usuwa własne przypisanie (wraca domyślne). Raporty i pulpit liczą od razu po nowemu."""
        from src.connectors.dash import MAIN_SHOPS
        from src.dashboard import data as panel_data

        db = get_db()
        account_id = account_id.strip()
        if not account_id.startswith("act_"):
            account_id = "act_" + account_id
        accounts = await db.get_setting("ad_accounts", {}) or {}
        if shop.strip() == "-":
            accounts.pop(account_id, None)
        elif shop not in MAIN_SHOPS:
            return "shop musi być jednym z: " + ", ".join(MAIN_SHOPS)
        else:
            accounts[account_id] = shop
        await db.set_setting("ad_accounts", accounts)
        panel_data.invalidate("dash")
        return f"Zapisane. Własne przypisania kont: {json.dumps(accounts, ensure_ascii=False)}"

    @mcp.tool()
    async def set_revenue_basis(basis: str) -> str:
        """Jak liczyć przychód w raportach i KPI: gross = wszystkie zamówienia złożone w ERP (domyślnie, jak w dash;
        z nieopłaconymi ofertami i anulowanymi), paid = tylko opłacone (ostatnie dni rosną, gdy schodzą przelewy)."""
        from src.dashboard import data as panel_data

        if basis not in ("gross", "paid"):
            return "basis: gross | paid"
        db = get_db()
        general = await db.get_setting("general", {}) or {}
        general["revenue_basis"] = basis
        await db.set_setting("general", general)
        panel_data.invalidate("dash")
        return f"Przychód liczony teraz jako: {'opłacone' if basis == 'paid' else 'złożone (wszystkie)'}."

    @mcp.tool()
    async def get_data_health() -> str:
        """Czy dane są świeże: kiedy ostatnio przyszły wiadomości z WhatsAppa, maile, kalendarz; ostrzeżenia
        (np. WhatsApp rozłączony — wtedy odpowiedzi Dominika z WhatsAppa nie są widoczne) i ostatnie przebiegi zadań."""
        from src.reports.health import data_health

        db = get_db()
        h = await data_health(db)
        runs = await db.get_last_run_any(["whatsapp_sync", "gmail_sync", "calendar_sync", "chat_digests",
                                          "live_refresh", "ideaerp_sync"])
        h["ostatnie_przebiegi"] = {k: {"status": v.get("status"), "kiedy": fmt_short(v.get("ran_at")),
                                       "blad": (v.get("error_message") or "")[:200]} for k, v in runs.items()}
        return json.dumps(h, ensure_ascii=False, indent=1, default=str)

    @mcp.tool()
    async def get_report_settings(report: str = "") -> str:
        """Ustawienia raportów (poranny briefing, podsumowanie dnia, tygodnia): godzina, dni, długość, sekcje
        (klucz → włączona), instrukcje. report: morning_briefing | daily_wrap | weekly_review (puste = wszystkie)."""
        from src.reports.profiles import SECTION_CATALOG, load_profiles, schedule_label

        profiles = await load_profiles(get_db())
        keys = [report] if report else list(profiles)
        out = {}
        for k in keys:
            p = profiles.get(k)
            if not p:
                return f"Nieznany raport: {k}"
            out[k] = {
                "nazwa": p.get("name"), "wlaczony": p.get("enabled"), "harmonogram": schedule_label(p),
                "dlugosc": p.get("length"), "model": p.get("model"), "email": p.get("email"),
                "slack": p.get("slack_channel") or "",
                "sekcje": [{"klucz": s["key"], "nazwa": SECTION_CATALOG[s["key"]]["label"], "wlaczona": s["enabled"],
                            **({"uwaga": s["note"]} if s.get("note") else {})} for s in p["sections"]],
                "instrukcje": p.get("instructions") or "",
            }
        return json.dumps(out, ensure_ascii=False, indent=1)

    @mcp.tool()
    async def update_report_settings(report: str, length: str = "", time: str = "", enabled: str = "",
                                     sections_on: str = "", sections_off: str = "", section_note: str = "",
                                     instructions: str = "", append_instructions: str = "") -> str:
        """Zmień raport (zapis od razu, jak w Studio raportów). report: morning_briefing | daily_wrap | weekly_review.
        length: short | standard | detailed. time: GG:MM. enabled: tak | nie.
        sections_on / sections_off: klucze albo nazwy sekcji po przecinku (np. „marketing, Raporty dnia zespołu”).
        section_note: „klucz: uwaga” — dodatkowa instrukcja dla jednej sekcji.
        instructions: zastępuje własne instrukcje; append_instructions: dopisuje zdanie do istniejących."""
        from src.reports.profiles import SECTION_CATALOG, load_profiles

        db = get_db()
        profiles = await load_profiles(db)
        if report not in profiles:
            return "report: " + " | ".join(profiles)
        p = profiles[report]
        changed = []

        def key_of(token: str) -> str | None:
            t = token.strip().lower()
            if t in SECTION_CATALOG:
                return t
            return next((k for k, v in SECTION_CATALOG.items() if v["label"].lower() == t or t in v["label"].lower()), None)

        if length:
            if length not in ("short", "standard", "detailed"):
                return "length: short | standard | detailed"
            p["length"] = length
            changed.append(f"długość {length}")
        if time:
            t = time.strip()
            if len(t) != 5 or t[2] != ":" or not t.replace(":", "").isdigit():
                return "time: GG:MM"
            p["time"] = t
            changed.append(f"godzina {t}")
        if enabled:
            p["enabled"] = enabled.strip().lower() in ("tak", "true", "1", "yes", "on")
            changed.append("włączony" if p["enabled"] else "wyłączony")
        for raw, on in ((sections_on, True), (sections_off, False)):
            for token in filter(None, (x.strip() for x in raw.split(","))):
                k = key_of(token)
                if not k:
                    return f"Nie znam sekcji „{token}”. Klucze: " + ", ".join(SECTION_CATALOG)
                for s in p["sections"]:
                    if s["key"] == k:
                        s["enabled"] = on
                changed.append(f"{'+' if on else '−'}{k}")
        if section_note:
            k_raw, _, note = section_note.partition(":")
            k = key_of(k_raw)
            if not k:
                return f"Nie znam sekcji „{k_raw}”."
            for s in p["sections"]:
                if s["key"] == k:
                    s["note"] = note.strip()[:500]
            changed.append(f"uwaga do {k}")
        if instructions:
            p["instructions"] = instructions.strip()[:4000]
            changed.append("instrukcje")
        if append_instructions:
            p["instructions"] = ((p.get("instructions") or "").rstrip() + "\n" + append_instructions.strip())[:4000].strip()
            changed.append("dopisana instrukcja")
        if not changed:
            return "Nic do zmiany."
        profiles[report] = p
        await db.set_setting("reports", profiles)
        return f"Zapisane ({report}): " + ", ".join(changed) + ". Działa od następnego raportu."

    @mcp.tool()
    async def run_report(report: str = "daily_wrap", send: bool = False) -> str:
        """Wygeneruj raport teraz (w tle, 1–3 min). send=True wysyła go też mailem/Slackiem jak zwykle.
        Wynik odczytasz potem get_latest_report(report)."""
        from src.reports.engine import ReportEngine
        from src.reports.profiles import load_profiles

        db = get_db()
        if report not in await load_profiles(db):
            return "report: morning_briefing | daily_wrap | weekly_review"

        async def go():
            try:
                await ReportEngine(db).run(report, deliver=send)
            except Exception as e:
                logger.warning(f"MCP run_report {report} failed: {e}")

        task = asyncio.create_task(go())
        _background.add(task)
        task.add_done_callback(_background.discard)
        return f"Generuję {report} ({'z wysyłką' if send else 'bez wysyłki'}). Za 1–3 minuty: get_latest_report(\"{report}\")."
