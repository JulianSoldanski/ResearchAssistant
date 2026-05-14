import os
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from pyzotero import zotero

from db import database as db

load_dotenv()

SUPPORTED_TYPES = {"journalArticle", "book", "conferencePaper", "bookSection",
                   "thesis", "report", "preprint"}

# Zotero stores imported attachments under <data_dir>/storage/<attachment_key>/<filename>.
# Default data dir on macOS is ~/Zotero. Override with ZOTERO_DATA_DIR in .env.
def _zotero_storage_dir() -> Path:
    custom = os.getenv("ZOTERO_DATA_DIR", "").strip()
    base = Path(custom).expanduser() if custom else Path.home() / "Zotero"
    return base / "storage"

# Zotero sub-collection names that map to Kanban priority levels.
# Create these as sub-collections in your Zotero group to organize papers.
COLLECTION_NAMES = {
    "Trash": 0,
    "Inbox": 1,
    "Interesting": 2,
    "Relevant": 3,
    "Crucial": 4,
}


def _get_client() -> zotero.Zotero:
    api_key = os.getenv("ZOTERO_API_KEY", "")
    library_id = os.getenv("ZOTERO_LIBRARY_ID", "")
    library_type = os.getenv("ZOTERO_LIBRARY_TYPE", "user")

    if not api_key or api_key == "your_zotero_api_key_here":
        raise ValueError("ZOTERO_API_KEY not configured in .env")
    if not library_id or library_id == "your_library_id_here":
        raise ValueError("ZOTERO_LIBRARY_ID not configured in .env")

    return zotero.Zotero(library_id, library_type, api_key)


def _extract_year(date_str: str | None) -> int | None:
    if not date_str:
        return None
    for part in str(date_str).split("-"):
        if len(part) == 4 and part.isdigit():
            return int(part)
    if len(date_str) >= 4 and date_str[:4].isdigit():
        return int(date_str[:4])
    return None


def _format_authors(creators: list) -> str:
    names = []
    for c in creators:
        if c.get("creatorType") == "author":
            last = c.get("lastName", "")
            first = c.get("firstName", "")
            names.append(f"{last}, {first}".strip(", "))
    return "; ".join(names) if names else "Unknown"


def _resolve_pdf_path(zot: zotero.Zotero, parent_key: str) -> str | None:
    """Find the local filesystem path of a PDF attachment for a Zotero item.

    Handles both `imported_file` (in Zotero/storage/<KEY>/<filename>) and
    `linked_file` (absolute filesystem path). Returns None if no readable
    PDF attachment exists.
    """
    try:
        children = zot.children(parent_key)
    except Exception:
        return None

    storage_root = _zotero_storage_dir()

    for child in children:
        child_data = child.get("data", {})
        if child_data.get("itemType") != "attachment":
            continue

        content_type = (child_data.get("contentType") or "").lower()
        link_mode = child_data.get("linkMode") or ""
        raw_path = (child_data.get("path") or "")
        filename = child_data.get("filename") or ""
        child_key = child.get("key") or child_data.get("key") or ""

        is_pdf = "pdf" in content_type or raw_path.lower().endswith(".pdf") \
            or filename.lower().endswith(".pdf")
        if not is_pdf:
            continue

        if link_mode in ("imported_file", "imported_url"):
            fname = filename
            if not fname:
                if raw_path.startswith("storage:"):
                    fname = raw_path[len("storage:"):]
                elif raw_path.startswith("attachments:"):
                    fname = raw_path[len("attachments:"):]
                else:
                    fname = raw_path
            if not fname or not child_key:
                continue
            candidate = storage_root / child_key / fname
            if candidate.exists():
                return str(candidate)

        elif link_mode == "linked_file":
            if raw_path.startswith("/") and Path(raw_path).exists():
                return raw_path
            if raw_path.startswith("attachments:"):
                rel = raw_path[len("attachments:"):]
                base = os.getenv("ZOTERO_LINKED_BASE_DIR", "").strip()
                if base:
                    candidate = Path(base).expanduser() / rel
                    if candidate.exists():
                        return str(candidate)

    return None


def _build_priority_collection_map(zot: zotero.Zotero) -> dict[str, int]:
    """Returns {collection_key: priority_level} for our 5 named sub-collections."""
    mapping: dict[str, int] = {}
    for c in zot.collections():
        name = c.get("data", {}).get("name", "")
        if name in COLLECTION_NAMES:
            mapping[c["key"]] = COLLECTION_NAMES[name]
    return mapping


def sync_papers(conn: sqlite3.Connection) -> tuple[int, int]:
    """Sync Zotero library to local DB. Returns (added_or_updated, skipped).

    Papers are assigned a priority_level based on which of the 5 named
    sub-collections (Trash/Inbox/Interesting/Relevant/Crucial) they belong to.
    Papers not in any of these go to Inbox. priority_level is only set on
    INSERT — existing papers keep their local priority.
    """
    zot = _get_client()

    collection_key_to_priority = _build_priority_collection_map(zot)

    items = zot.everything(zot.top())

    updated = 0
    skipped = 0

    for item in items:
        data = item.get("data", {})
        item_type = data.get("itemType", "")

        if item_type not in SUPPORTED_TYPES:
            skipped += 1
            continue

        zotero_id = data.get("key", "")
        title = data.get("title", "Untitled")
        authors = _format_authors(data.get("creators", []))
        year = _extract_year(data.get("date"))

        # Pick priority from the first matching priority sub-collection
        priority = 1  # default Inbox
        for ck in data.get("collections", []) or []:
            if ck in collection_key_to_priority:
                priority = collection_key_to_priority[ck]
                break

        local_path = _resolve_pdf_path(zot, zotero_id)

        db.upsert_paper(conn, zotero_id, title, authors, year, local_path, priority)
        updated += 1

    return updated, skipped


def push_to_zotero(conn: sqlite3.Connection) -> tuple[int, int]:
    """Push local priority_level changes back to Zotero collections.

    For each paper, ensures it's in the Zotero sub-collection matching its
    local priority_level, and removes it from other priority sub-collections.
    Items already in the correct state are not touched.

    Returns (pushed, failed). Raises ValueError if expected sub-collections
    don't exist in Zotero.
    """
    zot = _get_client()

    name_to_key: dict[str, str] = {}
    for c in zot.collections():
        name = c.get("data", {}).get("name", "")
        if name in COLLECTION_NAMES:
            name_to_key[name] = c["key"]

    missing = [n for n in COLLECTION_NAMES if n not in name_to_key]
    if missing:
        raise ValueError(
            f"Missing sub-collections in Zotero group: {', '.join(missing)}. "
            "Create them in Zotero, then retry."
        )

    priority_to_key = {COLLECTION_NAMES[name]: key for name, key in name_to_key.items()}
    our_keys = set(name_to_key.values())

    papers = conn.execute("SELECT zotero_id, priority_level FROM papers").fetchall()
    pushed = 0
    failed = 0

    for paper in papers:
        zotero_id = paper["zotero_id"]
        priority = paper["priority_level"]
        target_key = priority_to_key.get(priority)
        if not target_key:
            continue

        try:
            item = zot.item(zotero_id)
        except Exception:
            failed += 1
            continue

        current = list(item.get("data", {}).get("collections", []) or [])
        # Strip our 5 priority collections, then add target
        new_collections = [c for c in current if c not in our_keys]
        new_collections.append(target_key)

        if set(new_collections) == set(current):
            continue

        item["data"]["collections"] = new_collections
        try:
            zot.update_item(item)
            pushed += 1
        except Exception:
            failed += 1

    return pushed, failed
