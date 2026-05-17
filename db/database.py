import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "research_assistant.db"


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
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
            created_at      TEXT DEFAULT (datetime('now'))
        );
    """)
    # Migrate existing databases that predate the notes column
    try:
        conn.execute("ALTER TABLE papers ADD COLUMN notes TEXT")
        conn.commit()
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
    """Clear all papers, comments, AI cache, and saved searches. Settings preserved."""
    conn.executescript("""
        DELETE FROM comments;
        DELETE FROM searches;
        DELETE FROM ai_cache;
        DELETE FROM papers;
        DELETE FROM sqlite_sequence WHERE name IN ('papers', 'comments', 'ai_cache', 'searches');
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
                paper_ids: list[int], response: str) -> int:
    cur = conn.execute("""
        INSERT INTO searches (raw_query, optimized_query, paper_ids, response)
        VALUES (?, ?, ?, ?)
    """, (raw_query, optimized_query, json.dumps(paper_ids), response))
    conn.commit()
    return cur.lastrowid


def get_all_searches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM searches ORDER BY created_at DESC"
    ).fetchall()


def delete_search(conn: sqlite3.Connection, search_id: int) -> None:
    conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))
    conn.commit()


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

    style = get_setting(conn, "writing_style", "").strip()
    if style:
        parts.append(
            "## Writing Style\n"
            "Match the tone, vocabulary, and sentence structure of the "
            "following sample(s) in your response. Do NOT echo the sample "
            "back — only imitate the style.\n\n"
            f"```\n{style}\n```"
        )

    return "\n\n".join(parts)
