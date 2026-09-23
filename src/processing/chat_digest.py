"""Chat understanding — one AI digest per chat per day + commitments.

Instead of dumping hundreds of raw WhatsApp lines into every report, each
active chat is read once as a whole conversation (with reply context, voice
note markers, who is who in the company) and condensed into: a summary,
decisions, open questions, whether Dominik owes a reply, and concrete
commitments. Reports then work from these digests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta

from src.ai.client import AIClient
from src.common.timeutil import WARSAW, day_range, fmt_date_pl, to_local, today
from src.storage.database import Database

logger = logging.getLogger(__name__)

MAX_MESSAGES = 400
CONTEXT_BEFORE = 12

DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "importance": {"type": "string", "enum": ["high", "normal", "low"]},
        "needs_reply": {"type": "boolean"},
        "reply_hint": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "topics": {"type": "array", "items": {"type": "string"}},
        "commitments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["mine", "ask", "theirs"]},
                    "counterpart": {"type": "string"},
                    "what": {"type": "string"},
                    "due_date": {"type": "string"},
                    "due_hint": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["direction", "counterpart", "what", "due_date", "due_hint", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "summary", "importance", "needs_reply", "reply_hint",
        "decisions", "open_questions", "topics", "commitments",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Jesteś asystentem Dominika Tomaszczyka — CEO i założyciela MyBed Group (marki: MyBed — łóżka tapicerowane, MittoHome — meble i dodatki, NOMO / NomoSleep — materace i sen, LepszySen.pl; sklepy mybed.pl, mybed.de, mittohome.pl; showroomy w kilku miastach). Czytasz jedną rozmowę z WhatsAppa z jednego dnia i przygotowujesz jej rzetelne streszczenie dla Dominika.

Jak czytać zapis:
- „Ja (Dominik)” to wiadomości Dominika; pozostałe osoby są podpisane nazwą, a gdy znamy ich rolę w firmie — rolą w nawiasie.
- „↩ na: …” pokazuje, na którą wiadomość ktoś odpowiada — używaj tego, żeby zrozumieć, kto co z kim ustala.
- [głosówka 0:42], [zdjęcie], [dokument: plik.pdf] to treści, których nie widzisz. Nie zgaduj ich zawartości; jeśli są istotne (np. długa głosówka od kluczowej osoby), napisz, że Dominik powinien ją odsłuchać/obejrzeć.
- Wiadomości „(kontekst z wcześniej)” są tylko tłem — streszczaj dzień, którego dotyczy prośba.

Co zwracasz:
- summary: 2–4 konkretne zdania po polsku. Kto, co, ustalenia, liczby, terminy. Bez ogólników typu „rozmowa dotyczyła spraw firmowych”. Dla rozmów czysto prywatnych/towarzyskich: jedno krótkie zdanie.
- importance: high — decyzje, pieniądze, problemy klienta/produkcji/dostaw, ryzyko, coś pilnego od kluczowej osoby; low — small talk, powiadomienia, logistyka bez znaczenia; w pozostałych przypadkach normal.
- needs_reply: true tylko wtedy, gdy ktoś czeka na odpowiedź, decyzję lub działanie Dominika i Dominik jeszcze nie odpowiedział w tym wątku. reply_hint: czego dokładnie oczekują (1 zdanie) albo pusty tekst.
- decisions: ustalenia/decyzje podjęte w rozmowie.
- open_questions: nierozstrzygnięte kwestie, które mogą wymagać uwagi Dominika.
- topics: 1–4 krótkie nazwy spraw (np. „Dostawa tkanin od Komfort”, „Kampania Meta MittoHome”) — takie, pod którymi ta sama sprawa może pojawić się w innych czatach i mailach.
- commitments: tylko konkretne, wykonalne zobowiązania:
  • mine — Dominik obiecał coś zrobić („wyślę”, „zadzwonię”, „dam znać do piątku”),
  • ask — ktoś wprost prosi Dominika o działanie/decyzję,
  • theirs — ktoś obiecał coś Dominikowi.
  what = zwięzłe zadanie w trybie rozkazującym, zrozumiałe bez czytania czatu (np. „Wyślij Sandrze cennik B2B dla hotelu Arłamów”). counterpart = druga osoba. due_date = RRRR-MM-DD, jeśli termin wynika z rozmowy (licz względem daty rozmowy), inaczej pusty tekst; due_hint = oryginalne sformułowanie terminu albo pusty tekst. evidence = krótki cytat. Pomiń rzeczy już zrobione w tej samej rozmowie oraz grzecznościowe „odezwę się”.

Pisz po polsku. Nie wymyślaj faktów spoza zapisu."""


class ChatDigester:
    def __init__(self, db: Database, ai: AIClient, model: str | None = None, concurrency: int = 4):
        self.db = db
        self.ai = ai
        self.model = model
        self.sem = asyncio.Semaphore(concurrency)
        self._roles: dict[int, str] | None = None

    async def run(self, day: date | None = None, force: bool = False) -> dict:
        """Digest every chat with new messages on `day` (default: today)."""
        day = day or today()
        start, end = day_range(day)
        chats = await self.db.get_active_chats(start, end)
        marks = {} if force else await self.db.get_digest_marks(day)
        todo = [c for c in chats if c["jid"] and (c["last_event_id"] or 0) > marks.get(c["jid"], 0)]
        stats = {"day": day.isoformat(), "chats": len(chats), "digested": 0, "commitments": 0, "errors": 0}
        if not todo:
            return stats
        await self._load_roles()
        results = await asyncio.gather(*(self._digest_chat(c, day, start, end) for c in todo))
        for ok, n in results:
            stats["digested"] += int(ok)
            stats["errors"] += int(not ok)
            stats["commitments"] += n
        logger.info(f"Chat digests {day}: {stats}")
        return stats

    async def _load_roles(self) -> None:
        rows = await self.db.get_all_entities()
        self._roles = {}
        for r in rows:
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            role = meta.get("role") or ""
            if role:
                self._roles[r["id"]] = role.replace("_", " ")

    async def _digest_chat(self, chat: dict, day: date, start: datetime, end: datetime) -> tuple[bool, int]:
        async with self.sem:
            try:
                messages = await self.db.get_chat_messages(chat["jid"], since=start, until=end, limit=MAX_MESSAGES)
                if not messages:
                    return True, 0
                before = await self.db.get_chat_messages(
                    chat["jid"], since=start - timedelta(days=3), until=start, limit=CONTEXT_BEFORE
                )
                transcript = self._transcript(before, context=True) + self._transcript(messages)
                name = chat.get("chat_name") or chat["jid"]
                kind = "grupa" if chat.get("is_group") else "rozmowa prywatna"
                content = (
                    f"Czat: {name} ({kind})\nData: {fmt_date_pl(day)} ({day.isoformat()})\n"
                    f"Liczba wiadomości tego dnia: {len(messages)}\n\n" + "\n".join(transcript)
                )
                result = await self.ai.extract(
                    system=SYSTEM_PROMPT, content=content, schema=DIGEST_SCHEMA,
                    model=self.model, max_tokens=6000, effort="medium", purpose="chat_digest",
                )
                await self.db.upsert_chat_digest({
                    "chat_jid": chat["jid"],
                    "chat_name": name,
                    "is_group": bool(chat.get("is_group")),
                    "day": day,
                    "summary": result.get("summary", "").strip(),
                    "importance": result.get("importance", "normal"),
                    "needs_reply": bool(result.get("needs_reply")),
                    "reply_hint": result.get("reply_hint", "").strip(),
                    "decisions": result.get("decisions", []),
                    "open_questions": result.get("open_questions", []),
                    "topics": result.get("topics", []),
                    "message_count": len(messages),
                    "last_message_at": messages[-1]["timestamp"],
                    "last_event_id": chat["last_event_id"],
                })
                n = await self._store_commitments(result.get("commitments", []), chat, name, messages[-1]["timestamp"])
                return True, n
            except Exception as e:
                logger.warning(f"Digest failed for {chat.get('chat_name')}: {e}")
                return False, 0

    def _transcript(self, messages: list[dict], context: bool = False) -> list[str]:
        lines = []
        for m in messages:
            meta = m.get("metadata") or {}
            ts = to_local(m["timestamp"]).strftime("%H:%M" if not context else "%d.%m %H:%M")
            if meta.get("from_me"):
                who = "Ja (Dominik)"
            else:
                who = m.get("sender_display") or meta.get("sender_name") or "?"
                role = (self._roles or {}).get(m.get("sender_entity_id"))
                if role:
                    who = f"{who} ({role})"
            body = (m.get("body") or "").replace("\n", " ⏎ ")
            if len(body) > 1200:
                body = body[:1200] + "…"
            extra = ""
            if meta.get("quoted_text"):
                extra = f" ↩ na: „{meta['quoted_text'][:160]}”"
            if meta.get("forwarded"):
                extra += " (przekazana dalej)"
            prefix = "(kontekst z wcześniej) " if context else ""
            lines.append(f"{prefix}[{ts}] {who}:{extra} {body}")
        return lines

    async def _store_commitments(self, items: list[dict], chat: dict, chat_name: str, at: datetime) -> int:
        n = 0
        for c in items:
            what = (c.get("what") or "").strip()
            direction = c.get("direction")
            if not what or direction not in {"mine", "ask", "theirs"}:
                continue
            due = _parse_date(c.get("due_date"))
            fp = fingerprint("whatsapp", chat["jid"], direction, what)
            await self.db.upsert_commitment({
                "fingerprint": fp,
                "direction": direction,
                "title": what[:300],
                "context": (c.get("evidence") or "")[:500],
                "counterpart": (c.get("counterpart") or "")[:120],
                "source": "whatsapp",
                "chat_jid": chat["jid"],
                "chat_name": chat_name,
                "source_at": at,
                "due_date": due,
                "due_hint": (c.get("due_hint") or "")[:120],
            })
            n += 1
        return n


def fingerprint(source: str, scope: str, direction: str, text: str) -> str:
    norm = re.sub(r"[^\w]+", " ", text.lower(), flags=re.UNICODE).strip()
    norm = " ".join(sorted(set(norm.split())))[:200]
    return hashlib.sha1(f"{source}|{scope}|{direction}|{norm}".encode()).hexdigest()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
