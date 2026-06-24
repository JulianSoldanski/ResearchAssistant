import os
import re
import sqlite3
from html import escape
from pathlib import Path
from dotenv import load_dotenv
from pyzotero import zotero

from db import database as db


# Tag we put on Zotero child notes we manage. Used to find our note among any
# other notes the user may have attached to a paper.
MANAGED_NOTE_TAG = "research-assistant"

# Section markers inside the managed note. <h1> headings make the note read
# nicely inside Zotero's editor and give us stable anchors to parse against.
_WHY_HEADING = "Why is this important"
_NOTES_HEADING = "Notes"

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


def _build_note_html(why: str, notes_text: str) -> str:
    """Render the two-section managed note as Zotero-flavored HTML."""
    why_html = escape(why or "").replace("\n", "<br>")
    notes_html = escape(notes_text or "").replace("\n", "<br>")
    return (
        f"<h1>{_WHY_HEADING}</h1>"
        f"<p>{why_html}</p>"
        f"<h1>{_NOTES_HEADING}</h1>"
        f"<p>{notes_html}</p>"
    )


_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _strip_html(fragment: str) -> str:
    """Best-effort HTML → plain-text converter for the small subset Zotero emits."""
    fragment = _BR_RE.sub("\n", fragment)
    fragment = re.sub(r"</p\s*>", "\n", fragment, flags=re.IGNORECASE)
    fragment = _TAG_RE.sub("", fragment)
    fragment = fragment.replace("&nbsp;", " ")
    fragment = fragment.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return fragment.strip("\n")


def _parse_note_html(html: str) -> tuple[str, str]:
    """Pull the (why, notes) sections out of a managed note's HTML.

    Robust to either heading appearing first, missing sections, and the user
    editing the body in Zotero's WYSIWYG (which may swap `<h1>` for other
    tags). Falls back to dumping the whole stripped text into `notes` when no
    heading is recognized.
    """
    if not html:
        return "", ""
    # Split on any heading tag whose text matches one of our markers
    pattern = re.compile(
        r"<h[1-6][^>]*>\s*(" + re.escape(_WHY_HEADING) + r"|"
        + re.escape(_NOTES_HEADING) + r")\s*</h[1-6]>",
        re.IGNORECASE,
    )
    parts = pattern.split(html)
    # parts: [pre, marker1, body1, marker2, body2, ...]
    sections: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        marker = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections[marker] = _strip_html(body).strip()
    if not sections:
        return "", _strip_html(html).strip()
    return sections.get(_WHY_HEADING, ""), sections.get(_NOTES_HEADING, "")


def _find_managed_note(zot: zotero.Zotero, parent_key: str) -> dict | None:
    """Return the managed child note for a paper, or None."""
    try:
        children = zot.children(parent_key)
    except Exception:
        return None
    for child in children:
        data = child.get("data", {})
        if data.get("itemType") != "note":
            continue
        tags = [t.get("tag", "") for t in data.get("tags", []) or []]
        if MANAGED_NOTE_TAG in tags:
            return child
    return None


def _upsert_managed_note(zot: zotero.Zotero, parent_key: str,
                         why: str, notes_text: str) -> bool:
    """Create or update the managed child note for a paper.

    Returns True on success (including no-op), False on API failure.
    Skips entirely when both fields are empty AND no note exists yet.
    """
    new_html = _build_note_html(why, notes_text)
    existing = _find_managed_note(zot, parent_key)

    if existing is None:
        if not (why or "").strip() and not (notes_text or "").strip():
            return True  # nothing to push, nothing to create
        template = zot.item_template("note")
        template["note"] = new_html
        template["tags"] = [{"tag": MANAGED_NOTE_TAG}]
        template["parentItem"] = parent_key
        try:
            zot.create_items([template])
            return True
        except Exception:
            return False

    if existing["data"].get("note", "") == new_html:
        return True  # already in sync
    existing["data"]["note"] = new_html
    try:
        zot.update_item(existing)
        return True
    except Exception:
        return False


def _build_priority_collection_map(zot: zotero.Zotero) -> dict[str, int]:
    """Returns {collection_key: priority_level} for our 5 named sub-collections."""
    mapping: dict[str, int] = {}
    for c in zot.collections():
        name = c.get("data", {}).get("name", "")
        if name in COLLECTION_NAMES:
            mapping[c["key"]] = COLLECTION_NAMES[name]
    return mapping


def _ensure_priority_collections(zot: zotero.Zotero) -> dict[str, str]:
    """Make sure the 5 priority sub-collections exist in Zotero, creating any
    that are missing. Returns {collection_name: key} for all 5.
    """
    name_to_key: dict[str, str] = {}
    for c in zot.collections():
        name = c.get("data", {}).get("name", "")
        if name in COLLECTION_NAMES:
            name_to_key[name] = c["key"]

    missing = [n for n in COLLECTION_NAMES if n not in name_to_key]
    if not missing:
        return name_to_key

    # Create at the root level of the library/group.
    payload = [{"name": n} for n in missing]
    try:
        zot.create_collections(payload)
    except Exception as exc:
        raise ValueError(
            f"Couldn't auto-create missing Zotero sub-collections "
            f"({', '.join(missing)}): {exc}. Check that the API key has "
            f"write access to this library."
        ) from exc

    # Refetch to pick up the freshly-created keys
    for c in zot.collections():
        name = c.get("data", {}).get("name", "")
        if name in COLLECTION_NAMES and name not in name_to_key:
            name_to_key[name] = c["key"]

    still_missing = [n for n in COLLECTION_NAMES if n not in name_to_key]
    if still_missing:
        raise ValueError(
            f"Created collections but Zotero did not return keys for: "
            f"{', '.join(still_missing)}"
        )
    return name_to_key


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

        existed_before = conn.execute(
            "SELECT id, notes, notes_text FROM papers WHERE zotero_id = ?",
            (zotero_id,),
        ).fetchone()

        db.upsert_paper(conn, zotero_id, title, authors, year, local_path, priority)
        updated += 1

        # Pull the managed note from Zotero only when local notes are empty —
        # "local wins" means we never overwrite something the user typed here.
        local_why = (existed_before["notes"] if existed_before else "") or ""
        local_notes = (existed_before["notes_text"] if existed_before else "") or ""
        if not local_why.strip() and not local_notes.strip():
            note = _find_managed_note(zot, zotero_id)
            if note is not None:
                remote_why, remote_notes = _parse_note_html(
                    note["data"].get("note", "")
                )
                if remote_why or remote_notes:
                    paper_row = conn.execute(
                        "SELECT id FROM papers WHERE zotero_id = ?",
                        (zotero_id,),
                    ).fetchone()
                    if paper_row is not None:
                        pid = paper_row["id"]
                        if remote_why:
                            db.update_paper_notes(conn, pid, remote_why)
                        if remote_notes:
                            db.update_paper_notes_text(conn, pid, remote_notes)

    return updated, skipped


def push_to_zotero(conn: sqlite3.Connection) -> tuple[int, int]:
    """Push local priority_level changes back to Zotero collections.

    For each paper, ensures it's in the Zotero sub-collection matching its
    local priority_level, and removes it from other priority sub-collections.
    Items already in the correct state are not touched. Missing priority
    sub-collections are auto-created in Zotero.

    Returns (pushed, failed). Raises ValueError if auto-creation fails
    (typically a write-permission issue with the API key).
    """
    zot = _get_client()
    name_to_key = _ensure_priority_collections(zot)
    priority_to_key = {COLLECTION_NAMES[name]: key for name, key in name_to_key.items()}
    our_keys = set(name_to_key.values())

    papers = conn.execute(
        "SELECT zotero_id, priority_level, notes, notes_text FROM papers"
    ).fetchall()
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

        collections_changed = set(new_collections) != set(current)
        if collections_changed:
            item["data"]["collections"] = new_collections
            try:
                zot.update_item(item)
                pushed += 1
            except Exception:
                failed += 1
                continue  # don't try to push the note if the item update failed

        # Push the managed note (local wins, so we always overwrite Zotero)
        ok = _upsert_managed_note(
            zot, zotero_id,
            paper["notes"] or "", paper["notes_text"] or "",
        )
        if not ok:
            failed += 1

    return pushed, failed
