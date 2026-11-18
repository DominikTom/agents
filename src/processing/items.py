"""Items Dominik manages himself — from the panel and from chat with Claude (MCP).

One place for the rules, so the panel and the MCP tools behave the same:
- add an item ("dodaj: zadzwoń do Comfy jutro"),
- close / reopen / snooze / edit an item by id, or find it by words ("umowa Eli"),
- notes: facts and decisions he tells the agents ("umowa Eli podpisana") — reports treat them as authoritative
  and the resolver closes matching open items right away.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime

from src.common.timeutil import next_workday, now, today

KINDS = {"decyzja", "zadanie", "odpowiedz", "czekam"}
STATUSES = {"done", "dismissed", "open"}


def parse_due(value) -> date | None:
    """YYYY-MM-DD, DD.MM(.YYYY), "dziś", "jutro" — or None."""
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    v = str(value).strip().lower()
    if v in ("dziś", "dzis", "today"):
        return today()
    if v in ("jutro", "tomorrow"):
        return next_workday(today())
    if v.count(".") == 1:  # DD.MM → this year
        v = f"{v}.{today().year}"
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Nie rozumiem daty „{value}” (użyj RRRR-MM-DD albo DD.MM)")


async def add_manual_item(db, title: str, kind: str = "zadanie", due=None, who: str | None = None,
                          note: str | None = None) -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("Pusty tytuł")
    kind = kind if kind in KINDS else "zadanie"
    direction = "theirs" if kind == "czekam" else "mine"
    fp = "manual:" + hashlib.sha1(f"{title}|{now().isoformat()}".encode()).hexdigest()
    cid = await db.upsert_commitment({
        "fingerprint": fp, "direction": direction, "kind": kind, "title": title[:300],
        "counterpart": (who or "")[:120] or None, "source": "manual", "chat_jid": None,
        "chat_name": "dodane przez Dominika", "source_at": now(), "due_date": parse_due(due), "due_hint": "",
        "note": (note or "")[:500] or None,
    })
    # added by hand → never closed by the automatic passes
    await db._execute("UPDATE commitments SET keep_open = TRUE WHERE id = $1", cid)
    return await db.get_commitment(cid)


async def find_items(db, query: str, status: str | None = "open", limit: int = 8) -> list[dict]:
    return await db.search_commitments(query, status=status, limit=limit)


async def set_status(db, cid: int, status: str, note: str | None = None) -> dict | None:
    if status not in STATUSES:
        raise ValueError("status: done | dismissed | open")
    c = await db.get_commitment(cid)
    if not c:
        return None
    await db.set_commitment_status(cid, status, (note or "").strip()[:300] or None)
    return await db.get_commitment(cid)


async def update_item(db, cid: int, **fields) -> dict | None:
    patch = {}
    if fields.get("title"):
        patch["title"] = str(fields["title"]).strip()[:300]
    if "due" in fields and fields["due"] is not None:
        patch["due_date"] = parse_due(fields["due"]) if fields["due"] != "" else None
    if fields.get("kind") in KINDS:
        patch["kind"] = fields["kind"]
        if fields["kind"] == "czekam":
            patch["direction"] = "theirs"
    if fields.get("who") is not None:
        patch["counterpart"] = str(fields["who"])[:120] or None
    if fields.get("note") is not None:
        patch["note"] = str(fields["note"])[:500] or None
    if "snooze" in fields and fields["snooze"] is not None:
        patch["snoozed_until"] = (next_workday(today()) if fields["snooze"] in (True, "jutro", "")
                                  else parse_due(fields["snooze"]))
    return await db.update_commitment(cid, patch)


async def add_note(db, text: str, about: str | None = None, source: str = "mcp", resolve: bool = True,
                   ai=None) -> dict:
    """Save Dominik's note and (resolve=True) immediately close open items it settles. Returns
    {"note": row, "closed": [...]}."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Pusta notatka")
    note = await db.add_note(text[:2000], (about or "").strip()[:200] or None, source)
    closed: list[dict] = []
    if resolve:
        from src.processing.resolution import ItemResolver, note_matches

        candidates = await db.get_crosscheck_candidates(limit=200)
        ids = [c["id"] for c in candidates if note_matches(note, c)]
        if ids:
            stats = await ItemResolver(db, ai).run(only_ids=ids, force=True)
            closed = stats.get("closed", [])
    return {"note": note, "closed": closed}
