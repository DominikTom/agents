"""Entity resolution - maps names/IDs from different sources to canonical entities.

Seeded from config/people.yaml. For a team of ~10 people, simple exact + fuzzy
first-name matching covers 95%+ of cases.
"""

from __future__ import annotations

import logging

from src.storage.database import Database

logger = logging.getLogger(__name__)


class EntityResolver:
    """Resolves names across sources to canonical entity IDs."""

    def __init__(self, db: Database):
        self.db = db
        # In-memory cache: (source, name) -> entity_id
        self._alias_cache: dict[tuple[str, str], int] = {}
        # Display name cache: name_lower -> entity_id
        self._name_cache: dict[str, int] = {}

    async def load_cache(self) -> None:
        """Load all aliases into memory for fast lookup."""
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch("SELECT entity_id, source, alias_name FROM entity_aliases")
            for row in rows:
                self._alias_cache[(row["source"], row["alias_name"])] = row["entity_id"]

            entities = await conn.fetch("SELECT id, display_name, canonical_name FROM entities")
            for ent in entities:
                self._name_cache[ent["display_name"].lower()] = ent["id"]
                self._name_cache[ent["canonical_name"].lower()] = ent["id"]
                # Also cache first name
                first_name = ent["display_name"].split()[0].lower() if ent["display_name"] else ""
                if first_name:
                    self._name_cache[first_name] = ent["id"]

        logger.info(
            f"EntityResolver: loaded {len(self._alias_cache)} aliases, "
            f"{len(self._name_cache)} name mappings"
        )

    def resolve(self, source: str, name: str) -> int | None:
        """Resolve a name from a source to an entity ID.

        Resolution order:
        1. Exact match on (source, name) in alias cache
        2. Fuzzy match on display_name / canonical_name / first_name
        """
        if not name:
            return None

        # 1. Exact alias match
        entity_id = self._alias_cache.get((source, name))
        if entity_id:
            return entity_id

        # 2. Name-based fuzzy match
        name_lower = name.lower().strip()
        entity_id = self._name_cache.get(name_lower)
        if entity_id:
            return entity_id

        # 3. Try first name only
        first_name = name_lower.split()[0] if name_lower else ""
        if first_name:
            entity_id = self._name_cache.get(first_name)
            if entity_id:
                return entity_id

        return None

    async def seed_from_config(self, people_config: dict) -> None:
        """Populate entities and aliases from people.yaml config.

        Expected format:
        people:
          karolina:
            display_name: Karolina
            role: operations
            asana_name: "Karolina Kowalska"
            slack_name: "Karolina Kowalska"
            whatsapp_name: "Karolina"
            email_patterns: ["karolina@mybed.pl"]
        """
        people = people_config.get("people", {})
        if not people:
            logger.info("EntityResolver: no people in config, skipping seed")
            return

        count = 0
        for key, person in people.items():
            if not isinstance(person, dict):
                continue

            display_name = person.get("display_name", key.title())
            entity_type = person.get("entity_type", "person")
            canonical_name = person.get("canonical_name", display_name)
            role = person.get("role", "")

            entity_id = await self.db.upsert_entity(
                entity_type=entity_type,
                canonical_name=canonical_name,
                display_name=display_name,
                metadata={"role": role, "key": key},
            )

            # Create aliases for each source
            source_mappings = {
                "asana": person.get("asana_name"),
                "slack": person.get("slack_name"),
                "whatsapp": person.get("whatsapp_name"),
                "gmail": person.get("gmail_name"),
            }

            for source, alias in source_mappings.items():
                if alias:
                    # Support list of aliases (e.g. whatsapp_name: ["Maciej", "Maciek Żydziak"])
                    if isinstance(alias, list):
                        for a in alias:
                            await self.db.upsert_alias(entity_id, source, a)
                    else:
                        await self.db.upsert_alias(entity_id, source, alias)

            # Email patterns as gmail aliases
            for pattern in person.get("email_patterns", []):
                await self.db.upsert_alias(entity_id, "gmail", pattern)

            # External IDs
            for source_key in ["asana_id", "slack_id"]:
                ext_id = person.get(source_key)
                if ext_id:
                    source = source_key.replace("_id", "")
                    alias = source_mappings.get(source, display_name)
                    await self.db.upsert_alias(entity_id, source, alias or display_name, ext_id)

            count += 1

        await self.load_cache()
        logger.info(f"EntityResolver: seeded {count} entities from config")
