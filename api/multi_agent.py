"""Multi-agent pipeline: workers (map) → synthesizer (reduce) → critic (refine).

Used by both cross-paper search and chapter writing when the user opts in to
multi-agent mode. Single-shot calls go directly through gemini_client and
bypass this module.
"""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

from db import database as db
from utils.pdf_processor import extract_text


# Internal API. Imported by api.gemini_client lazily to avoid circular imports.

_MAX_PARALLEL_WORKERS = 4  # bound concurrent API calls so we don't fan out 50 in parallel


@dataclass
class WorkerOut:
    batch_index: int               # 1-based
    paper_indices: list[int]       # global indices (1-based) covered by this worker
    output: str                    # the worker's findings


@dataclass
class PipelineResult:
    final: str
    workers: list[WorkerOut] = field(default_factory=list)
    synthesizer_draft: str = ""
    critic_revision: Optional[str] = None

    @classmethod
    def single_shot(cls, text: str) -> "PipelineResult":
        """Wrap a single-pass response so callers can stay uniform."""
        return cls(final=text)


def _build_paper_blocks(paper_ids: list[int], conn: sqlite3.Connection,
                        per_paper_chars: int) -> list[tuple[int, str, str]]:
    """Resolve and pre-render every paper as a (global_idx, title, block_text) tuple.

    Papers without a local PDF or that fail to extract are silently skipped —
    they DO consume their global index (so citations stay stable even if a PDF
    is missing — the index just won't appear in any worker's slice).
    """
    blocks = []
    for global_idx, pid in enumerate(paper_ids, start=1):
        paper = db.get_paper_by_id(conn, pid)
        if not paper or not paper["local_file_path"]:
            continue
        try:
            text = extract_text(paper["local_file_path"])
        except Exception:
            continue
        title = paper["title"] or "Untitled"
        body = (
            f"### Paper [{global_idx}]: {title}\n"
            f"Authors: {paper['authors'] or 'Unknown'}\n"
            f"Year: {paper['year'] or '?'}\n\n"
            f"{text[:per_paper_chars]}"
        )
        blocks.append((global_idx, title, body))
    return blocks


def _chunk(lst: list, size: int) -> list[list]:
    return [lst[i:i + size] for i in range(0, len(lst), size)]


def _task_description(task_kind: str, task_prompt: str) -> str:
    if task_kind == "chapter":
        return f"Extract evidence relevant to writing the thesis section: \"{task_prompt}\""
    # default: search
    return f"Extract findings relevant to the query: \"{task_prompt}\""


def _worker_prompt(batch_idx: int, n_workers: int, task_kind: str,
                   task_prompt: str, blocks: list[tuple[int, str, str]]) -> str:
    paper_block_text = "\n\n---\n\n".join(block for _, _, block in blocks)
    indices = ", ".join(f"[{i}]" for i, _, _ in blocks)
    task_desc = _task_description(task_kind, task_prompt)
    return (
        f"You are worker #{batch_idx} of {n_workers} researchers analyzing a "
        f"corpus of academic papers. Your batch covers papers {indices}. The "
        f"paper indices are GLOBAL — use the same bracketed numbers for "
        f"citations so they stay consistent across all workers.\n\n"
        f"PAPERS:\n\n{paper_block_text}\n\n"
        f"---\n\n"
        f"TASK: {task_desc}\n\n"
        f"INSTRUCTIONS:\n"
        f"- Output bullet points of findings, claims, or evidence from YOUR papers only.\n"
        f"- Cite each bullet by its bracketed paper index, e.g. [3].\n"
        f"- Be concise but specific. Quote numbers and concrete facts where present.\n"
        f"- Do NOT try to write a complete answer — you're one of several workers.\n"
        f"- Do NOT speculate beyond what the papers state.\n"
        f"- If a paper has no relevant content, write a single line: "
        f"\"Paper [N]: nothing relevant.\"\n\n"
        f"OUTPUT the bullet list. No preamble, no closing remark."
    )


def _synthesizer_prompt(task_kind: str, task_prompt: str,
                        thesis_context: str, workers: list[WorkerOut]) -> str:
    task_desc = _task_description(task_kind, task_prompt)
    reports = []
    for w in workers:
        idx_str = ", ".join(f"[{i}]" for i in w.paper_indices)
        reports.append(
            f"=== Worker {w.batch_index} (papers {idx_str}) ===\n{w.output}"
        )
    reports_block = "\n\n".join(reports)

    ctx_block = ""
    if thesis_context.strip():
        ctx_block = (
            f"## Thesis context (for tone, framing, scope — do not mention directly)\n\n"
            f"{thesis_context}\n\n"
        )

    output_hint = (
        "Write a unified, well-organized answer to the user's query. Group "
        "findings by theme."
        if task_kind == "search"
        else
        "Draft the chapter section. Use academic prose with light markdown "
        "(subheadings where helpful, paragraphs, bullet lists for enumerations). "
        "Do not include a top-level heading repeating the section title."
    )

    return (
        f"You are synthesizing reports from {len(workers)} research workers, "
        f"each of whom analyzed a batch of papers.\n\n"
        f"ORIGINAL TASK: {task_desc}\n\n"
        f"{ctx_block}"
        f"## Worker reports\n\n{reports_block}\n\n"
        f"---\n\n"
        f"INSTRUCTIONS:\n"
        f"- {output_hint}\n"
        f"- Preserve ALL paper-index citations from the worker reports — "
        f"cross-batch citations are fine (e.g. [3] from worker 1 + [12] from worker 3).\n"
        f"- Do NOT invent claims that aren't in the worker reports.\n"
        f"- Note disagreements or gaps if relevant.\n"
        f"- If a worker said \"nothing relevant\" for a paper, you don't need to mention it.\n\n"
        f"OUTPUT the answer/draft. No preamble."
    )


def _critic_prompt(task_kind: str, task_prompt: str, draft: str,
                   workers: list[WorkerOut]) -> str:
    task_desc = _task_description(task_kind, task_prompt)
    reports = []
    for w in workers:
        idx_str = ", ".join(f"[{i}]" for i in w.paper_indices)
        reports.append(
            f"=== Worker {w.batch_index} (papers {idx_str}) ===\n{w.output}"
        )
    reports_block = "\n\n".join(reports)

    return (
        f"You are a senior researcher reviewing a draft synthesis.\n\n"
        f"ORIGINAL TASK: {task_desc}\n\n"
        f"## Draft to review\n\n{draft}\n\n"
        f"## Source worker reports (the ground truth — the draft is derived from these)\n\n"
        f"{reports_block}\n\n"
        f"---\n\n"
        f"YOUR JOB: Find and fix issues in the draft. Specifically:\n"
        f"1. **Missing claims**: things in the worker reports that are important "
        f"but absent from the draft.\n"
        f"2. **Unsupported claims**: anything in the draft not backed by a worker "
        f"report, or carrying a wrong citation.\n"
        f"3. **Contradictions**: where workers disagree but the draft glosses over it.\n"
        f"4. **Vague hedging**: where the workers were specific but the draft is generic.\n\n"
        f"OUTPUT a revised version of the draft fixing all issues. Maintain the "
        f"same structure where it works. Do NOT introduce information not in the "
        f"worker reports. Same citation style ([N] references)."
    )


def run_pipeline(
    paper_ids: list[int],
    conn: sqlite3.Connection,
    task_prompt: str,
    task_kind: str = "search",                # "search" | "chapter"
    thesis_context: str = "",
    papers_per_worker: int = 5,
    enable_critic: bool = True,
    worker_model: Optional[str] = None,
    synthesizer_model: Optional[str] = None,
    critic_model: Optional[str] = None,
    per_paper_chars: int = 30_000,
    on_progress: Optional[Callable[[str], None]] = None,
) -> PipelineResult:
    """Map-reduce-refine pipeline. See module docstring."""
    # Import locally to avoid circular imports (gemini_client imports us back).
    from api import gemini_client as _gc

    if not paper_ids:
        raise ValueError("No papers selected.")
    if not task_prompt.strip():
        raise ValueError("Task prompt is empty.")

    wm = worker_model or _gc.SEARCH_MODEL
    sm = synthesizer_model or _gc.SEARCH_MODEL
    cm = critic_model or _gc.SEARCH_MODEL

    def progress(msg: str):
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    # --- Resolve PDFs once, then chunk ---
    progress("Extracting paper text…")
    all_blocks = _build_paper_blocks(paper_ids, conn, per_paper_chars)
    if not all_blocks:
        raise ValueError("No papers with extractable PDFs.")

    chunks = _chunk(all_blocks, papers_per_worker)
    n_workers = len(chunks)
    progress(f"Dispatching {n_workers} workers…")

    # --- Map: run workers in parallel ---
    workers: list[WorkerOut] = [None] * n_workers  # preserve order
    completed = 0

    def run_worker(batch_idx: int, blocks: list) -> WorkerOut:
        prompt = _worker_prompt(batch_idx, n_workers, task_kind, task_prompt, blocks)
        return WorkerOut(
            batch_index=batch_idx,
            paper_indices=[i for i, _, _ in blocks],
            output=_gc._call_model(wm, prompt).strip(),
        )

    with ThreadPoolExecutor(max_workers=_MAX_PARALLEL_WORKERS) as ex:
        futures = {
            ex.submit(run_worker, i + 1, chunk): i
            for i, chunk in enumerate(chunks)
        }
        for fut in as_completed(futures):
            i = futures[fut]
            workers[i] = fut.result()  # propagates exceptions
            completed += 1
            progress(f"Worker {completed}/{n_workers} done")

    # --- Reduce: synthesize ---
    progress("Synthesizing…")
    syn_prompt = _synthesizer_prompt(task_kind, task_prompt, thesis_context, workers)
    draft = _gc._call_model(sm, syn_prompt).strip()

    if not enable_critic:
        return PipelineResult(final=draft, workers=workers, synthesizer_draft=draft,
                              critic_revision=None)

    # --- Refine: critic ---
    progress("Critic reviewing…")
    crit_prompt = _critic_prompt(task_kind, task_prompt, draft, workers)
    revision = _gc._call_model(cm, crit_prompt).strip()

    progress("Done.")
    return PipelineResult(final=revision, workers=workers,
                          synthesizer_draft=draft, critic_revision=revision)
