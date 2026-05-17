import os
import sqlite3
import time
from dotenv import load_dotenv
from google import genai

from db import database as db
from utils.pdf_processor import extract_text
from utils.cache import make_hash

load_dotenv()

# Transient HTTP errors worth retrying. Pro models in particular hit 503s
# under heavy demand; they typically recover within a minute.
_RETRY_TOKENS = ("503", "429", "500", "502", "504",
                 "UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED")
_RETRY_DELAYS = (8, 20, 45)  # seconds; total backoff up to ~73s


def _generate_with_retry(client, model: str, contents: str):
    """Call Gemini with auto-retry on transient errors (503/429/etc)."""
    last_exc = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            return client.models.generate_content(model=model, contents=contents)
        except Exception as exc:
            err = str(exc)
            transient = any(t in err for t in _RETRY_TOKENS)
            if not transient or attempt >= len(_RETRY_DELAYS):
                if transient:
                    raise RuntimeError(
                        f"{model} is overloaded right now ({err.splitlines()[0]}). "
                        f"Retried {len(_RETRY_DELAYS)} times. Try again in a minute."
                    ) from exc
                raise
            last_exc = exc
            time.sleep(_RETRY_DELAYS[attempt])
    raise last_exc

# (display label, model id). Order = order in the UI dropdown; first entry is default.
AVAILABLE_MODELS: list[tuple[str, str]] = [
    ("Flash (fast)", "gemini-3-flash-preview"),
    ("Pro (deeper)", "gemini-3.1-pro-preview"),
]
DEFAULT_MODEL = AVAILABLE_MODELS[0][1]

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("gemini_api_key") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("gemini_api_key not set in .env")
        _client = genai.Client(api_key=api_key)
    return _client


def query(paper_id: int, pdf_path: str, prompt_text: str,
          conn: sqlite3.Connection, thesis_context: str = "",
          model: str = DEFAULT_MODEL) -> tuple[str, bool]:
    """Return (response_text, from_cache). Cache is keyed by (pdf, prompt, model)."""
    pdf_text = extract_text(pdf_path)
    prompt_hash = make_hash(pdf_text, prompt_text, model)

    cached = db.get_cached_response(conn, paper_id, prompt_hash)
    if cached:
        return cached, True

    system_context = ""
    if thesis_context.strip():
        system_context = (
            f"My thesis context (problem, research questions, methodology, "
            f"outline, data management plan):\n\n{thesis_context}\n\n"
        )

    full_prompt = (
        f"{system_context}"
        f"The following is the full text of an academic paper:\n\n"
        f"{pdf_text}\n\n"
        f"---\n\n"
        f"{prompt_text}"
    )

    client = _get_client()
    response = _generate_with_retry(client, model, full_prompt)
    response_text = response.text

    db.save_cached_response(conn, paper_id, prompt_hash, prompt_text, response_text, model)
    return response_text, False


# --- Cross-paper search ---

# Flash for both query optimization and cross-paper search (fast + 1M token window)
SEARCH_MODEL = "gemini-3-flash-preview"

# Per-paper character limit when concatenating into the search prompt.
# 30k chars ~ 7.5k tokens. 30 papers ~ 225k tokens, well within Flash's 1M context.
_PER_PAPER_CHARS = 30_000


def optimize_search_query(raw_query: str) -> str:
    """Rewrite a rough user query into a precise prompt for an LLM cross-paper search."""
    meta_prompt = (
        "You are a search-query expert. Transform the user's rough query into a "
        "precise, well-structured prompt for searching across a corpus of academic "
        "papers using an LLM.\n\n"
        "Apply these best practices:\n"
        "1. State the search intent clearly (what is the user actually looking for?)\n"
        "2. Specify what counts as relevant — concepts, methods, results, claims\n"
        "3. Require explicit per-paper citations in the answer (by bracketed index)\n"
        "4. Ask for structured output (themes, bullet points, or a comparison table)\n"
        "5. Restrict the answer to information present in the provided papers\n"
        "6. Keep it under 200 words but specific\n\n"
        f'User query: """{raw_query}"""\n\n'
        "Output ONLY the optimized prompt as a single block of plain text. "
        "Do not include explanations, examples, or markdown headings."
    )
    client = _get_client()
    response = _generate_with_retry(client, SEARCH_MODEL, meta_prompt)
    return (response.text or "").strip()


def cross_paper_search(paper_ids: list[int], conn: sqlite3.Connection,
                       optimized_query: str, thesis_context: str = "",
                       model: str = SEARCH_MODEL) -> str:
    """Run an LLM search across multiple papers. Returns markdown answer.

    Each paper gets a bracketed index ([1], [2]…) for citation. Papers without
    a resolvable PDF path are silently skipped; raises if none remain.
    """
    if not paper_ids:
        raise ValueError("No papers selected.")

    paper_blocks = []
    skipped = 0
    for idx, pid in enumerate(paper_ids, start=1):
        paper = db.get_paper_by_id(conn, pid)
        if not paper or not paper["local_file_path"]:
            skipped += 1
            continue
        try:
            text = extract_text(paper["local_file_path"])
        except Exception:
            skipped += 1
            continue
        header = (
            f"### Paper [{idx}]: {paper['title']}\n"
            f"Authors: {paper['authors'] or 'Unknown'}\n"
            f"Year: {paper['year'] or '?'}\n\n"
        )
        paper_blocks.append(header + text[:_PER_PAPER_CHARS])

    if not paper_blocks:
        raise ValueError(f"No papers with extractable PDFs ({skipped} skipped).")

    system_context = ""
    if thesis_context.strip():
        system_context = f"My thesis context:\n\n{thesis_context}\n\n"

    full_prompt = (
        f"{system_context}"
        f"You have access to the following {len(paper_blocks)} academic papers. "
        f"Answer the user's query using ONLY information from these papers. "
        f"When making a claim, cite the supporting paper(s) by their bracketed "
        f"index, e.g. [1], [3]. If multiple papers support the same point, cite "
        f"all of them.\n\n"
        + "\n\n---\n\n".join(paper_blocks)
        + f"\n\n---\n\nUser query: {optimized_query}"
    )

    client = _get_client()
    response = _generate_with_retry(client, model, full_prompt)
    return response.text or ""
