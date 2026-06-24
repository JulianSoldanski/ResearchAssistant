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


def _is_openai_model(model: str) -> bool:
    return model.startswith("gpt-") or model.startswith("o1") or model.startswith("o3")


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


def _call_openai(model: str, prompt: str) -> str:
    """Call OpenAI API and return response text."""
    from openai import OpenAI
    global _openai_client
    if _openai_client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set in .env")
        _openai_client = OpenAI(api_key=api_key)
    response = _openai_client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""


def _call_model(model: str, prompt: str) -> str:
    """Provider-agnostic generation: routes to OpenAI or Gemini based on model id."""
    if _is_openai_model(model):
        return _call_openai(model, prompt)
    client = _get_client()
    response = _generate_with_retry(client, model, prompt)
    return response.text or ""


# (display label, model id). Order = order in the UI dropdown; first entry is default.
AVAILABLE_MODELS: list[tuple[str, str]] = [
    ("Flash (fast)", "gemini-3-flash-preview"),
    ("Pro (deeper)", "gemini-3.1-pro-preview"),
]
DEFAULT_MODEL = AVAILABLE_MODELS[0][1]

# Models available in the cross-paper search dropdown (includes OpenAI).
SEARCH_MODELS: list[tuple[str, str]] = [
    ("GPT-5.5", "gpt-5.5-2026-04-23"),
    ("Flash (fast)", "gemini-3-flash-preview"),
    ("Pro (deeper)", "gemini-3.1-pro-preview"),
]
DEFAULT_SEARCH_MODEL = SEARCH_MODELS[0][1]


_client: genai.Client | None = None
_openai_client = None


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


_SECTION_GUIDES = {
    "Problem": (
        "Describe the gap or motivation in 1–2 paragraphs. Start from the "
        "broader context, narrow to the specific problem, and end with why "
        "solving it matters. Avoid generic platitudes."
    ),
    "Research Questions": (
        "Provide 1–3 specific, answerable research questions. Avoid yes/no "
        "framing. Each question should be precise enough that an answer would "
        "be defensible and falsifiable."
    ),
    "Methodology": (
        "Describe the research approach (design science, empirical study, "
        "literature review, etc.), the data sources, the key analysis steps, "
        "and the evaluation criteria. Be concrete about what you'll DO."
    ),
    "Rough Outline": (
        "Provide a numbered chapter/section outline appropriate for a "
        "Master's thesis (e.g. 1. Introduction, 1.1 Background, 2. Related "
        "Work, 3. Methodology, …). Keep it realistic in scope."
    ),
    "Data Management Plan": (
        "Cover: data types collected, collection method, storage and backup, "
        "sharing/access policy, retention and archival, and any ethics or "
        "data-protection considerations."
    ),
}


def generate_thesis_section(target: str, other_sections: dict[str, str],
                            existing: str = "",
                            style_profile: str = "") -> str:
    """Draft or refine a thesis section.

    `target` is the plain section name ("Problem", "Methodology", …).
    `other_sections` is a {label: text} map of the other sections that
    already have content — used as context so the new draft stays coherent.
    If `existing` is non-empty, the AI improves it instead of starting over.
    `style_profile` is the user's writing-style profile (from Settings).
    """
    if other_sections:
        ctx_blocks = [f"## {label}\n{text}" for label, text in other_sections.items()]
        other_ctx = "\n\n".join(ctx_blocks)
    else:
        other_ctx = "(no other sections written yet)"

    if existing.strip():
        task = (
            f"The user has a draft for the '{target}' section below. Improve "
            f"and expand it — keep their phrasing where it works, but polish "
            f"weak spots, fix structure, and strengthen the argument. Do not "
            f"radically change the meaning.\n\n"
            f"Existing draft:\n```\n{existing}\n```"
        )
    else:
        task = (
            f"The user has not yet written the '{target}' section. "
            f"Generate a strong initial draft based on the other sections "
            f"above. Be concrete and specific, not generic."
        )

    style_block = ""
    if style_profile.strip():
        style_block = (
            "\n\n## Writing Style Guide\n"
            "Write your output so it matches the style profile below. Do NOT "
            "mention or quote the profile — apply it naturally.\n\n"
            f"{style_profile}\n"
        )

    guide = _SECTION_GUIDES.get(target, "")
    prompt = (
        f"You are helping a researcher draft their Master's thesis.\n\n"
        f"## Other sections already written\n\n{other_ctx}\n\n"
        f"## Section guidance for '{target}'\n{guide}\n\n"
        f"## Your task\n{task}"
        f"{style_block}\n\n"
        f"OUTPUT REQUIREMENTS:\n"
        f"- Output ONLY the content of the '{target}' section.\n"
        f"- No section heading (the user knows what they asked for).\n"
        f"- No preamble like 'Here is your draft:'. Just the content.\n"
        f"- Plain text or light markdown only."
    )

    client = _get_client()
    response = _generate_with_retry(client, SEARCH_MODEL, prompt)
    return (response.text or "").strip()


def analyze_writing_style(sample: str) -> str:
    """Extract a structured style profile from a writing sample.

    Returns a markdown profile describing tone, sentence structure, vocabulary,
    rhetorical devices, paragraph structure, quirks, and anti-patterns —
    intended to be used as a style guide for other LLM calls (not as
    content to be echoed back).
    """
    prompt = (
        "You are a stylometric analyst. Analyze the writing style of the "
        "sample below. DO NOT summarize the content — focus only on HOW the "
        "text is written. Produce a structured profile another LLM can use "
        "to match this style.\n\n"
        "Use exactly these markdown headings, with concise but specific "
        "observations under each (cite 1–2 short examples from the sample "
        "where it helps):\n"
        "## Tone & Register\n"
        "## Sentence Structure\n"
        "## Vocabulary\n"
        "## Rhetorical Devices & Transitions\n"
        "## Paragraph Structure\n"
        "## Signature Quirks\n"
        "## Things to Avoid (anti-patterns)\n\n"
        "Be concrete, not generic. \"Formal academic\" is weak; \"hedges "
        "claims with 'arguably' and 'tends to'; uses past-tense passive for "
        "methodology\" is strong.\n\n"
        f"SAMPLE:\n```\n{sample}\n```\n\n"
        "Output only the markdown profile. No preamble, no closing remark."
    )
    client = _get_client()
    response = _generate_with_retry(client, SEARCH_MODEL, prompt)
    return (response.text or "").strip()


def optimize_search_query(raw_query: str, model: str = SEARCH_MODEL) -> str:
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
    return _call_model(model, meta_prompt).strip()


def cross_paper_search(paper_ids: list[int], conn: sqlite3.Connection,
                       optimized_query: str, thesis_context: str = "",
                       model: str = DEFAULT_SEARCH_MODEL,
                       multi_agent: bool = False,
                       enable_critic: bool = True,
                       on_progress=None) -> "PipelineResult":
    """Run an LLM search across multiple papers.

    Always returns a `PipelineResult` (single-shot wraps the text via
    `PipelineResult.single_shot(...)`). When `multi_agent=True` it routes
    through `api.multi_agent.run_pipeline`.

    Each paper gets a bracketed index ([1], [2]…) for citation. Papers without
    a resolvable PDF path are silently skipped; raises if none remain.
    """
    from api.multi_agent import PipelineResult, run_pipeline

    if multi_agent:
        return run_pipeline(
            paper_ids=paper_ids,
            conn=conn,
            task_prompt=optimized_query,
            task_kind="search",
            thesis_context=thesis_context,
            enable_critic=enable_critic,
            worker_model=model,
            synthesizer_model=model,
            critic_model=model,
            per_paper_chars=_PER_PAPER_CHARS,
            on_progress=on_progress,
        )

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
        f"index, e.g. [1, name], [3, name]. If multiple papers support the same point, cite "
        f"all of them.\n\n"
        + "\n\n---\n\n".join(paper_blocks)
        + f"\n\n---\n\nUser query: {optimized_query}"
    )

    return PipelineResult.single_shot(_call_model(model, full_prompt))
