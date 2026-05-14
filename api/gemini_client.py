import os
import sqlite3
from dotenv import load_dotenv
from google import genai

from db import database as db
from utils.pdf_processor import extract_text
from utils.cache import make_hash

load_dotenv()

MODEL_NAME = "gemini-2.0-flash"

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
          conn: sqlite3.Connection, thesis_goals: str = "") -> tuple[str, bool]:
    """Return (response_text, from_cache)."""
    pdf_text = extract_text(pdf_path)
    prompt_hash = make_hash(pdf_text, prompt_text)

    cached = db.get_cached_response(conn, paper_id, prompt_hash)
    if cached:
        return cached, True

    system_context = ""
    if thesis_goals.strip():
        system_context = f"My core thesis goals are:\n{thesis_goals}\n\n"

    full_prompt = (
        f"{system_context}"
        f"The following is the full text of an academic paper:\n\n"
        f"{pdf_text}\n\n"
        f"---\n\n"
        f"{prompt_text}"
    )

    client = _get_client()
    response = client.models.generate_content(model=MODEL_NAME, contents=full_prompt)
    response_text = response.text

    db.save_cached_response(conn, paper_id, prompt_hash, prompt_text, response_text, MODEL_NAME)
    return response_text, False


def query_code(code_snippet: str) -> str:
    """Analyze a code snippet without caching."""
    system_prompt = (
        "You are an expert software architect and code reviewer. "
        "Analyze the provided code and produce a structured review covering:\n\n"
        "## Architecture & Design\n"
        "Evaluate the overall structure, separation of concerns, and design patterns used.\n\n"
        "## Edge Cases & Bugs\n"
        "Identify potential edge cases, off-by-one errors, null/None handling, and actual bugs.\n\n"
        "## Security Issues\n"
        "Flag any security vulnerabilities such as injection risks, improper validation, or "
        "exposure of sensitive data.\n\n"
        "## Improvements\n"
        "Suggest concrete, prioritized improvements with brief code examples where helpful.\n\n"
        "## Summary\n"
        "A one-paragraph summary verdict on the code quality."
    )

    full_prompt = f"{system_prompt}\n\n---\n\nCode to review:\n\n```\n{code_snippet}\n```"

    client = _get_client()
    response = client.models.generate_content(model=MODEL_NAME, contents=full_prompt)
    return response.text
