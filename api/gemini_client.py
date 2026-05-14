import os
import sqlite3
from dotenv import load_dotenv
from google import genai

from db import database as db
from utils.pdf_processor import extract_text
from utils.cache import make_hash

load_dotenv()

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
          conn: sqlite3.Connection, thesis_goals: str = "",
          model: str = DEFAULT_MODEL) -> tuple[str, bool]:
    """Return (response_text, from_cache). Cache is keyed by (pdf, prompt, model)."""
    pdf_text = extract_text(pdf_path)
    prompt_hash = make_hash(pdf_text, prompt_text, model)

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
    response = client.models.generate_content(model=model, contents=full_prompt)
    response_text = response.text

    db.save_cached_response(conn, paper_id, prompt_hash, prompt_text, response_text, model)
    return response_text, False
