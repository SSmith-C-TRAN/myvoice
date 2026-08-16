"""Match an incoming caller ID to a known contact name.

The contact list is a CSV committed to the repo and loaded into memory at
startup. DigitalOcean App Platform has an ephemeral filesystem, so a database
written at runtime wouldn't survive a deploy — but a CSV baked into the image
(and kept in git) does. Updating contacts is just: edit the CSV, git push.

Lookups are a plain dict keyed by a normalized phone number, so they're instant.
This is deliberately the smallest thing that works; it can grow into a real
database later if live uploads or bigger lists are ever needed.
"""

import csv
import logging
from pathlib import Path

from app.config import settings

logger = logging.getLogger("contacts")

# Normalized phone -> name. None until load() runs; lookup() lazy-loads so any
# path that runs before startup still works.
_index: dict[str, str] | None = None


def normalize(phone: str | None) -> str | None:
    """Reduce any phone format to a canonical key: the US 10-digit number.

    Twilio caller ID arrives as E.164 ("+15035550134"); a CSV might hold
    "(503) 555-0134", "503-555-0134", or "15035550134". We keep the digits,
    drop a leading US "1", and use the last 10 so every format collapses to one
    key. Returns None when there aren't enough digits to be a real number.
    (US-centric on purpose — fine for a personal line; revisit for i18n.)
    """
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) < 10:
        return None
    return digits[-10:]


def load(path: str | None = None) -> int:
    """Read the contacts CSV into the in-memory index. Returns rows loaded.

    Expects 'name' and 'phone' columns (case-insensitive); any extra columns
    are ignored, leaving room to grow the file. A missing file or bad header
    logs and yields an empty index so the app still boots.
    """
    global _index
    path = path or settings.contacts_file
    index: dict[str, str] = {}
    file = Path(path)

    if not file.exists():
        logger.warning("contacts file not found: %s — no names will match", path)
        _index = index
        return 0

    with file.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = {name.lower(): name for name in (reader.fieldnames or [])}
        name_col, phone_col = columns.get("name"), columns.get("phone")
        if not (name_col and phone_col):
            logger.error(
                "contacts file %s needs 'name' and 'phone' columns; got %s",
                path,
                reader.fieldnames,
            )
            _index = index
            return 0
        for row in reader:
            key = normalize(row.get(phone_col))
            name = (row.get(name_col) or "").strip()
            if key and name:
                index[key] = name

    _index = index
    logger.info("loaded %d contacts from %s", len(index), path)
    return len(index)


def lookup(phone: str | None) -> str | None:
    """Return the contact name for a phone number, or None if unknown."""
    if _index is None:
        load()
    key = normalize(phone)
    if key is None:
        return None
    return _index.get(key) if _index else None
