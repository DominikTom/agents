"""Sync people from MyBed OS into the entities table (OS is the source of truth)."""

from __future__ import annotations

import logging

from src.connectors.os_mybed import OSClient
from src.storage.database import Database

logger = logging.getLogger(__name__)


async def sync_os_people(db: Database, client: OSClient | None = None) -> dict:
    client = client or OSClient()
    if not client.configured:
        return {"skipped": "OS not configured"}
    people = await client.collection("people")
    count = 0
    for p in people:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        entity_id = await db.upsert_entity(
            entity_type="person",
            canonical_name=name,
            display_name=name,
            metadata={
                "os_id": p.get("id"),
                "role": p.get("title") or p.get("role") or "",
                "team": p.get("team") or "",
                "department_id": p.get("departmentId"),
                "email": p.get("email") or "",
                "responsibilities": p.get("responsibilities") or "",
                "ask_me_about": p.get("askMeAbout") or [],
                "access_role": p.get("accessRole") or "",
            },
        )
        for email in [p.get("email"), *(p.get("altEmails") or [])]:
            if email:
                await db.upsert_alias(entity_id, "gmail", email.lower())
        await db.upsert_alias(entity_id, "os", name, p.get("id"))
        count += 1
    logger.info(f"OS people sync: {count} people")
    return {"people": count}
