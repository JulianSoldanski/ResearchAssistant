import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "research_assistant.db"


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Required for ON DELETE CASCADE (e.g. thesis_chapters subchapter cleanup)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS papers (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            zotero_id        TEXT UNIQUE NOT NULL,
            title            TEXT NOT NULL,
            authors          TEXT,
            year             INTEGER,
            local_file_path  TEXT,
            priority_level   INTEGER DEFAULT 1,
            notes            TEXT,
            created_at       TEXT DEFAULT (datetime('now')),
            updated_at       TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS ai_cache (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id           INTEGER REFERENCES papers(id),
            prompt_hash        TEXT NOT NULL,
            prompt_text        TEXT NOT NULL,
            generated_response TEXT NOT NULL,
            model_used         TEXT,
            created_at         TEXT DEFAULT (datetime('now')),
            UNIQUE(paper_id, prompt_hash)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS comments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id     INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            comment_text TEXT NOT NULL,
            created_at   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS searches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_query       TEXT,
            optimized_query TEXT NOT NULL,
            paper_ids       TEXT NOT NULL,   -- JSON array of paper IDs
            response        TEXT NOT NULL,
            created_at      TEXT DEFAULT (datetime('now')),
            agent_workers   TEXT,            -- JSON list[WorkerOut] (NULL = single-shot)
            agent_draft     TEXT             -- synthesizer pre-critic draft
        );

        CREATE TABLE IF NOT EXISTS chapters (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            title         TEXT NOT NULL,
            instructions  TEXT,
            paper_ids     TEXT NOT NULL,    -- JSON array of paper IDs
            content       TEXT NOT NULL,
            model_used    TEXT,
            created_at    TEXT DEFAULT (datetime('now')),
            agent_workers TEXT,             -- JSON list[WorkerOut] (NULL = single-shot)
            agent_draft   TEXT              -- synthesizer pre-critic draft
        );

        CREATE TABLE IF NOT EXISTS thesis_chapters (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id   INTEGER REFERENCES thesis_chapters(id) ON DELETE CASCADE,
            title       TEXT NOT NULL,
            content     TEXT NOT NULL DEFAULT '',
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        );
    """)
    # Idempotent migrations for older databases. Each ALTER is wrapped in
    # try/except so re-running on a current schema is a no-op.
    for stmt in (
        "ALTER TABLE papers ADD COLUMN notes TEXT",
        "ALTER TABLE searches ADD COLUMN agent_workers TEXT",
        "ALTER TABLE searches ADD COLUMN agent_draft TEXT",
        "ALTER TABLE chapters ADD COLUMN agent_workers TEXT",
        "ALTER TABLE chapters ADD COLUMN agent_draft TEXT",
    ):
        try:
            conn.execute(stmt)
        except Exception:
            pass
    conn.commit()


# --- Papers ---

def upsert_paper(conn: sqlite3.Connection, zotero_id: str, title: str,
                 authors: str, year: int | None, local_file_path: str | None,
                 initial_priority: int = 1) -> None:
    """Insert paper, or update its metadata if it already exists.

    `initial_priority` is only used on INSERT — existing priority_level is
    preserved on UPDATE so a re-sync does not stomp on local moves.
    """
    conn.execute("""
        INSERT INTO papers (zotero_id, title, authors, year, local_file_path, priority_level)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(zotero_id) DO UPDATE SET
            title           = excluded.title,
            authors         = excluded.authors,
            year            = excluded.year,

            local_file_path = COALESCE(excluded.local_file_path, papers.local_file_path),
            updated_at      = datetime('now')
    """, (zotero_id, title, authors, year, local_file_path, initial_priority))
    conn.commit()


def set_paper_pdf_path(conn: sqlite3.Connection, paper_id: int, pdf_path: str) -> None:
    conn.execute(
        "UPDATE papers SET local_file_path = ?, updated_at = datetime('now') WHERE id = ?",
        (pdf_path, paper_id)
    )
    conn.commit()


def reset_database(conn: sqlite3.Connection) -> None:
    """Clear papers, comments, AI cache, saved searches, and chapter drafts.
    Settings (thesis description, writing style) preserved. The user's thesis
    chapter structure is also preserved.
    """
    conn.executescript("""
        DELETE FROM comments;
        DELETE FROM searches;
        DELETE FROM chapters;
        DELETE FROM ai_cache;
        DELETE FROM papers;
        DELETE FROM sqlite_sequence
            WHERE name IN ('papers', 'comments', 'ai_cache', 'searches', 'chapters');
    """)
    conn.commit()


def get_papers_by_priority(conn: sqlite3.Connection, priority_level: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM papers WHERE priority_level = ? ORDER BY title",
        (priority_level,)
    ).fetchall()


def get_paper_by_id(conn: sqlite3.Connection, paper_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()


def set_priority(conn: sqlite3.Connection, paper_id: int, priority_level: int) -> None:
    priority_level = max(0, min(4, priority_level))
    conn.execute(
        "UPDATE papers SET priority_level = ?, updated_at = datetime('now') WHERE id = ?",
        (priority_level, paper_id)
    )
    conn.commit()


def update_paper_notes(conn: sqlite3.Connection, paper_id: int, notes: str) -> None:
    conn.execute(
        "UPDATE papers SET notes = ?, updated_at = datetime('now') WHERE id = ?",
        (notes, paper_id)
    )
    conn.commit()


# --- Comments ---

def get_comments(conn: sqlite3.Connection, paper_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM comments WHERE paper_id = ? ORDER BY created_at DESC",
        (paper_id,)
    ).fetchall()


def add_comment(conn: sqlite3.Connection, paper_id: int, text: str) -> None:
    conn.execute(
        "INSERT INTO comments (paper_id, comment_text) VALUES (?, ?)",
        (paper_id, text)
    )
    conn.commit()


def delete_comment(conn: sqlite3.Connection, comment_id: int) -> None:
    conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    conn.commit()


# --- Searches (cross-paper search history) ---

def save_search(conn: sqlite3.Connection, raw_query: str, optimized_query: str,
                paper_ids: list[int], response: str,
                agent_workers: str | None = None,
                agent_draft: str | None = None) -> int:
    cur = conn.execute("""
        INSERT INTO searches (raw_query, optimized_query, paper_ids, response,
                              agent_workers, agent_draft)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (raw_query, optimized_query, json.dumps(paper_ids), response,
          agent_workers, agent_draft))
    conn.commit()
    return cur.lastrowid


def get_all_searches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM searches ORDER BY created_at DESC"
    ).fetchall()


def delete_search(conn: sqlite3.Connection, search_id: int) -> None:
    conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))
    conn.commit()


# --- Chapters (saved AI-drafted thesis chapters) ---

def save_chapter(conn: sqlite3.Connection, title: str, instructions: str,
                 paper_ids: list[int], content: str, model_used: str,
                 agent_workers: str | None = None,
                 agent_draft: str | None = None) -> int:
    cur = conn.execute("""
        INSERT INTO chapters (title, instructions, paper_ids, content,
                              model_used, agent_workers, agent_draft)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (title, instructions, json.dumps(paper_ids), content, model_used,
          agent_workers, agent_draft))
    conn.commit()
    return cur.lastrowid


def get_all_chapters(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM chapters ORDER BY created_at DESC"
    ).fetchall()


def delete_chapter(conn: sqlite3.Connection, chapter_id: int) -> None:
    conn.execute("DELETE FROM chapters WHERE id = ?", (chapter_id,))
    conn.commit()


# --- Thesis chapter tree (user-defined outline + content) ---

def get_thesis_chapters(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All thesis chapter rows, sorted by (parent_id NULLS FIRST, sort_order)."""
    return conn.execute(
        "SELECT * FROM thesis_chapters "
        "ORDER BY (parent_id IS NOT NULL), parent_id, sort_order, id"
    ).fetchall()


def add_thesis_chapter(conn: sqlite3.Connection, parent_id: int | None,
                       title: str = "Untitled") -> int:
    # New chapter goes at the end of its siblings.
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next "
        "FROM thesis_chapters WHERE parent_id IS ?",
        (parent_id,)
    ).fetchone()
    sort_order = row["next"] if row else 0
    cur = conn.execute(
        "INSERT INTO thesis_chapters (parent_id, title, sort_order) "
        "VALUES (?, ?, ?)",
        (parent_id, title, sort_order)
    )
    conn.commit()
    return cur.lastrowid


def update_thesis_chapter_title(conn: sqlite3.Connection, chapter_id: int,
                                title: str) -> None:
    conn.execute(
        "UPDATE thesis_chapters SET title = ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (title, chapter_id)
    )
    conn.commit()


def update_thesis_chapter_content(conn: sqlite3.Connection, chapter_id: int,
                                  content: str) -> None:
    conn.execute(
        "UPDATE thesis_chapters SET content = ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (content, chapter_id)
    )
    conn.commit()


def delete_thesis_chapter(conn: sqlite3.Connection, chapter_id: int) -> None:
    """Delete a chapter (children cascade via FK)."""
    conn.execute("DELETE FROM thesis_chapters WHERE id = ?", (chapter_id,))
    conn.commit()


def get_thesis_chapter_by_id(conn: sqlite3.Connection,
                             chapter_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM thesis_chapters WHERE id = ?", (chapter_id,)
    ).fetchone()


# --- AI Cache ---

def get_cached_response(conn: sqlite3.Connection, paper_id: int, prompt_hash: str) -> str | None:
    row = conn.execute(
        "SELECT generated_response FROM ai_cache WHERE paper_id = ? AND prompt_hash = ?",
        (paper_id, prompt_hash)
    ).fetchone()
    return row["generated_response"] if row else None


def save_cached_response(conn: sqlite3.Connection, paper_id: int, prompt_hash: str,
                         prompt_text: str, response: str, model_used: str) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO ai_cache
            (paper_id, prompt_hash, prompt_text, generated_response, model_used)
        VALUES (?, ?, ?, ?, ?)
    """, (paper_id, prompt_hash, prompt_text, response, model_used))
    conn.commit()


def get_analyses_for_paper(conn: sqlite3.Connection, paper_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, prompt_text, generated_response, model_used, created_at "
        "FROM ai_cache WHERE paper_id = ? ORDER BY created_at DESC",
        (paper_id,)
    ).fetchall()


def delete_analysis(conn: sqlite3.Connection, analysis_id: int) -> None:
    conn.execute("DELETE FROM ai_cache WHERE id = ?", (analysis_id,))
    conn.commit()


# --- Settings ---

def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, value)
    )
    conn.commit()


# Mirrors the SECTIONS list in ui/thesis.py (label + setting key).
# Used to build the AI system-context block from the user's thesis description.
_THESIS_SECTIONS = [
    ("Problem", "thesis_problem"),
    ("Research Questions", "thesis_research_questions"),
    ("Methodology", "thesis_methodology"),
    ("Rough Outline", "thesis_outline"),
    ("Data Management Plan", "thesis_data_management"),
]


def get_thesis_context(conn: sqlite3.Connection) -> str:
    """Assemble all non-empty thesis sections plus the optional writing-style
    sample into a single context block suitable for prepending to AI prompts.
    Empty sections are omitted.
    """
    parts = []
    for label, key in _THESIS_SECTIONS:
        val = get_setting(conn, key, "").strip()
        if val:
            parts.append(f"## {label}\n{val}")

    profile = get_setting(conn, "writing_style_analysis", "").strip()
    if profile:
        parts.append(
            "## Writing Style Guide\n"
            "Write your response so it matches the following style profile "
            "(this describes the user's writing — apply the patterns "
            "naturally; do NOT mention the profile or quote from it):\n\n"
            f"{profile}"
        )

    return "\n\n".join(parts)
