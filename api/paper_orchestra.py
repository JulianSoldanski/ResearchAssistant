"""PaperOrchestra: a modular multi-agent manuscript drafting pipeline.

Architecture:
- `ManuscriptDocument` is a dataclass carrying all manuscript state through
  the pipeline. Each node reads from it and writes new fields into it.
- `PipelineNode` is the abstract interface every step implements. The
  `run(state, ctx) -> state` contract is what keeps swapping a dummy for a
  real agent (Step 2 plotting; Step 5 refinement) a one-line change.
- `Pipeline` runs nodes in a fixed order. Nodes are addressable by name so
  callers can substitute individual nodes (or insert new ones) without
  rewriting the orchestrator.

Execution order: outline -> plotting (filler) -> literature -> writing -> refinement (dummy).
"""
from __future__ import annotations

import re
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


PLOTTING_FILLER_LINE = "here a plot should be created"
PLOTTING_PLACEHOLDER = f"[PLACEHOLDER: {PLOTTING_FILLER_LINE}]"
REFINEMENT_PLACEHOLDER = (
    "[STATUS: Draft complete. Handing over to manual Refinement and Peer Review loop]"
)

FIGURES_DIR = Path(__file__).parent.parent / "uploads" / "figures"

_PER_PAPER_CHARS = 30_000


@dataclass
class BibEntry:
    """One row of the bibliography derived from the user's selected papers."""
    index: int             # 1-based bracketed citation index ([1], [2], …)
    paper_id: int
    title: str
    authors: str
    year: int | str


@dataclass
class FigureSpec:
    """One rendered (or attempted) figure from the Plotting Agent."""
    index: int             # 1-based; matches `Figure N` references in prose
    topic: str             # raw text after `[DIAGRAM REQUIRED: …]`
    description: str       # planner output, fed to the image-gen model
    image_path: str        # absolute path on disk; empty if rendering failed
    error: str = ""        # populated when image_path is empty


@dataclass
class ManuscriptDocument:
    """Shared state passed between agents. Each step appends/updates fields."""
    # Inputs
    user_ideas: str = ""
    provided_literature: str = ""   # rendered paper blocks for Step 3
    thesis_context: str = ""

    # Step outputs
    structured_outline: str = ""
    plot_placeholders: str = ""
    figures: list[FigureSpec] = field(default_factory=list)
    literature_review: str = ""
    bibliography: list[BibEntry] = field(default_factory=list)
    drafted_methodology: str = ""
    drafted_experiments: str = ""
    drafted_conclusion: str = ""
    refinement_feedback: str = ""

    # Telemetry
    step_log: list[str] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)

    def assembled_manuscript(self) -> str:
        """Render every populated field into a single markdown document."""
        parts: list[str] = []
        if self.structured_outline.strip():
            parts.append("## Structured Outline\n\n" + self.structured_outline.strip())
        if self.figures:
            fig_lines = []
            for f in self.figures:
                if f.image_path:
                    fig_lines.append(f"- Figure {f.index} — {f.topic} — `{f.image_path}`")
                else:
                    fig_lines.append(f"- Figure {f.index} — {f.topic} — FAILED ({f.error})")
            parts.append("## Figures\n\n" + "\n".join(fig_lines))
        elif self.plot_placeholders.strip():
            parts.append("## Plot & Diagram Slots\n\n" + self.plot_placeholders.strip())
        if self.literature_review.strip():
            parts.append("## Introduction & Related Work\n\n" + self.literature_review.strip())
        if self.drafted_methodology.strip():
            parts.append("## Methodology\n\n" + self.drafted_methodology.strip())
        if self.drafted_experiments.strip():
            parts.append("## Experiments\n\n" + self.drafted_experiments.strip())
        if self.drafted_conclusion.strip():
            parts.append("## Conclusion\n\n" + self.drafted_conclusion.strip())
        if self.bibliography:
            bib_lines = [
                f"[{b.index}] {b.authors} ({b.year}). {b.title}."
                for b in self.bibliography
            ]
            parts.append("## Bibliography\n\n" + "\n".join(bib_lines))
        if self.refinement_feedback.strip():
            parts.append("## Refinement Status\n\n" + self.refinement_feedback.strip())
        return "\n\n".join(parts)


@dataclass
class PipelineContext:
    """Side-channel inputs that aren't part of the manuscript itself.

    Kept separate from ManuscriptDocument so the state stays serializable and
    so swapped-in real agents (replacing the dummies) can read whatever
    runtime values they need without polluting the document.
    """
    conn: Optional[sqlite3.Connection] = None
    model: Optional[str] = None
    paper_ids: list[int] = field(default_factory=list)
    on_progress: Optional[Callable[[str], None]] = None


# =====================================================================
# Node interface
# =====================================================================

class PipelineNode(ABC):
    """Each pipeline step is a node. Subclasses set `name` and `is_dummy`."""
    name: str = "node"
    is_dummy: bool = False

    @abstractmethod
    def run(self, state: ManuscriptDocument,
            ctx: PipelineContext) -> ManuscriptDocument:
        ...


# =====================================================================
# Step 1 — Outline Agent (ACTIVE)
# =====================================================================

OUTLINE_SYSTEM_PROMPT = (
    "You are an expert academic strategist and outline generator. Your task "
    "is to analyze the user's raw research notes, experiment logs, and ideas, "
    "and formulate a highly structured, comprehensive outline for a "
    "scientific paper. Determine the necessary sections, the logical flow of "
    "the argument, and explicitly list where diagrams, plots, or literature "
    "citations will be required. Output a clear, hierarchical plan that "
    "subsequent agents can use as a strict blueprint."
)


class OutlineNode(PipelineNode):
    name = "outline"
    is_dummy = False

    def run(self, state, ctx):
        from api import gemini_client as _gc

        if not state.user_ideas.strip():
            raise ValueError("OutlineNode: user_ideas is empty — nothing to outline.")

        ctx_block = ""
        if state.thesis_context.strip():
            ctx_block = (
                f"## Thesis context\n\n{state.thesis_context.strip()}\n\n"
            )

        prompt = (
            f"{OUTLINE_SYSTEM_PROMPT}\n\n"
            f"{ctx_block}"
            f"## User's raw research notes and ideas\n\n"
            f"{state.user_ideas.strip()}\n\n"
            f"---\n\n"
            f"Output the hierarchical outline. Use numbered sections "
            f"(1., 1.1, 1.2, …). For every section, add a brief one-line "
            f"description of its purpose. Where a diagram or plot is "
            f"required, insert a line `[DIAGRAM REQUIRED: <topic>]`. Where "
            f"citations from the provided literature will be needed, insert "
            f"`[CITATIONS REQUIRED: <topic>]`. No preamble, no closing "
            f"remarks."
        )

        client = _gc._get_client()
        resp = _gc._generate_with_retry(
            client, ctx.model or _gc.SEARCH_MODEL, prompt
        )
        state.structured_outline = (resp.text or "").strip()
        return state


# =====================================================================
# Step 2 — Plotting Agent (FILLER PLACEHOLDER)
# =====================================================================

_DIAGRAM_MARKER_RE = re.compile(r"\[DIAGRAM REQUIRED:\s*([^\]]+)\]", re.IGNORECASE)


class PlottingNode(PipelineNode):
    """Filler-only plotting step. No image generation.

    For each `[DIAGRAM REQUIRED: <topic>]` marker in the outline, emits a
    line of filler text downstream. The writing node reads
    `state.plot_placeholders` and references each slot as `[FIGURE: <topic>]`
    in the prose.
    """
    name = "plotting"
    is_dummy = True

    def run(self, state, ctx):
        topics = [m.group(1).strip()
                  for m in _DIAGRAM_MARKER_RE.finditer(state.structured_outline or "")]

        if not topics:
            state.plot_placeholders = PLOTTING_PLACEHOLDER
            state.figures = []
            return state

        lines = [
            f"Figure {idx} ({topic}): {PLOTTING_FILLER_LINE}"
            for idx, topic in enumerate(topics, start=1)
        ]
        state.plot_placeholders = "\n".join(lines)
        state.figures = []
        return state


# =====================================================================
# Step 3 — Literature Synthesis Agent (ACTIVE, no web access)
# =====================================================================

LITERATURE_SYSTEM_PROMPT = (
    "You are an expert academic author. You are provided with a fixed list "
    "of pre-researched scientific sources (Provided Literature) and an "
    "outline. Your sole task is to analyze these specific papers, "
    "synthesize their core points, and write a cohesive 'Introduction' and "
    "a 'Related Work' section. Do not invent your own sources and do not "
    "access the internet. Critically compare the provided literature, "
    "highlight research gaps, and seamlessly integrate the given citations "
    "into the text."
)


class LiteratureSynthesisNode(PipelineNode):
    name = "literature"
    is_dummy = False

    def run(self, state, ctx):
        from api import gemini_client as _gc

        if not state.provided_literature.strip():
            raise ValueError(
                "LiteratureSynthesisNode: no provided literature — select "
                "papers with PDFs before running the pipeline."
            )
        if not state.structured_outline.strip():
            raise ValueError(
                "LiteratureSynthesisNode: outline missing — Step 1 must run first."
            )

        prompt = (
            f"{LITERATURE_SYSTEM_PROMPT}\n\n"
            f"## Outline (your blueprint)\n\n{state.structured_outline}\n\n"
            f"## Provided Literature\n\n{state.provided_literature}\n\n"
            f"---\n\n"
            f"Write the 'Introduction' and 'Related Work' sections back to "
            f"back. Cite supporting claims by their bracketed paper index "
            f"(e.g. [1], [3]). Do not introduce sources outside the "
            f"provided list. End with a short paragraph that explicitly "
            f"names the research gap your manuscript fills.\n\n"
            f"OUTPUT STRUCTURE:\n"
            f"## Introduction\n<text>\n\n## Related Work\n<text>\n\n"
            f"## Research Gap\n<text>"
        )

        client = _gc._get_client()
        resp = _gc._generate_with_retry(
            client, ctx.model or _gc.SEARCH_MODEL, prompt
        )
        state.literature_review = (resp.text or "").strip()
        return state


# =====================================================================
# Step 4 — Section Writing Agent (ACTIVE)
# =====================================================================

WRITING_SYSTEM_PROMPT = (
    "You are an expert academic author. Your task is to write the core "
    "sections of a research manuscript, specifically the Methodology, "
    "Experiments, and Conclusion. You will be provided with a strict "
    "outline, a completed Literature Review, and placeholders for plots. "
    "Write clearly, concisely, and with high academic rigor. Ensure smooth "
    "transitions between the sections provided in the outline and embed "
    "the plot placeholders seamlessly into the narrative flow."
)

# Sentinel markers the writer agent uses; the parser uses these to split.
_SECTION_MARKERS = ("## Methodology", "## Experiments", "## Conclusion")


class SectionWritingNode(PipelineNode):
    name = "writing"
    is_dummy = False

    def run(self, state, ctx):
        from api import gemini_client as _gc

        if not state.structured_outline.strip():
            raise ValueError("SectionWritingNode: outline missing.")
        if not state.literature_review.strip():
            raise ValueError("SectionWritingNode: literature review missing.")

        # If real figures were rendered, tell the writer to reference them
        # by index; otherwise fall back to the marker-passthrough wording.
        if state.figures:
            fig_lines = [
                f"- Figure {f.index}: {f.topic}"
                for f in state.figures if f.image_path
            ]
            figure_block = (
                "## Available figures\n\n"
                + "\n".join(fig_lines)
                + "\n\nReference these in prose as 'Figure N' (e.g. 'as "
                "illustrated in Figure 2, …'). Every available figure should "
                "be cited at least once.\n\n"
            )
            figure_instruction = (
                "Reference rendered figures by their numeric index ('Figure N')."
            )
        else:
            figure_block = (
                f"## Plot/Diagram placeholder status\n\n{state.plot_placeholders}\n\n"
            )
            figure_instruction = (
                "Where the outline marks `[DIAGRAM REQUIRED: <topic>]`, embed "
                "`[FIGURE: <topic>]` inline in the prose so it reads as a "
                "natural reference (e.g. 'as shown in [FIGURE: pipeline "
                "overview], …')."
            )

        prompt = (
            f"{WRITING_SYSTEM_PROMPT}\n\n"
            f"## Outline (strict blueprint)\n\n{state.structured_outline}\n\n"
            f"## Completed Literature Review\n\n{state.literature_review}\n\n"
            f"{figure_block}"
            f"---\n\n"
            f"Write the Methodology, Experiments, and Conclusion sections. "
            f"{figure_instruction} Carry over the bracketed citation indices "
            f"used by the literature review where appropriate.\n\n"
            f"OUTPUT STRUCTURE (use these EXACT headings, in this order):\n"
            f"## Methodology\n<text>\n\n## Experiments\n<text>\n\n"
            f"## Conclusion\n<text>\n\n"
            f"No preamble. No closing remark."
        )

        client = _gc._get_client()
        resp = _gc._generate_with_retry(
            client, ctx.model or _gc.SEARCH_MODEL, prompt
        )
        raw = (resp.text or "").strip()
        methodology, experiments, conclusion = _split_writing_output(raw)
        state.drafted_methodology = methodology
        state.drafted_experiments = experiments
        state.drafted_conclusion = conclusion
        return state


def _split_writing_output(raw: str) -> tuple[str, str, str]:
    """Parse the writer's heading-delimited output into three sections.

    Falls back to dumping everything into Methodology if the model didn't
    follow the heading contract — the operator still sees something useful.
    """
    pos = {marker: raw.find(marker) for marker in _SECTION_MARKERS}
    if any(p < 0 for p in pos.values()):
        return raw, "", ""

    m_start = pos["## Methodology"] + len("## Methodology")
    e_start = pos["## Experiments"] + len("## Experiments")
    c_start = pos["## Conclusion"] + len("## Conclusion")

    methodology = raw[m_start:pos["## Experiments"]].strip()
    experiments = raw[e_start:pos["## Conclusion"]].strip()
    conclusion = raw[c_start:].strip()
    return methodology, experiments, conclusion


# =====================================================================
# Step 5 — Content Refinement Agent (DUMMY / MANUAL PLACEHOLDER)
# =====================================================================

class RefinementNode(PipelineNode):
    """Pass-through placeholder. Halts automated pipeline; awaits human review.

    Future replacement options (the interface stays the same):
    1. Multi-agent critique framework (worker reviewers + a chief editor).
    2. Human-in-the-loop pause — the orchestrator detects the placeholder
       output, surfaces the draft to the user, then loops back to Step 4
       with manual feedback merged into `state.refinement_feedback`.
    """
    name = "refinement"
    is_dummy = True

    def run(self, state, ctx):
        state.refinement_feedback = REFINEMENT_PLACEHOLDER
        return state


# =====================================================================
# Pipeline orchestrator
# =====================================================================

class Pipeline:
    """Runs nodes in a fixed order. Supports node swapping for future upgrades."""

    DEFAULT_ORDER: tuple[str, ...] = (
        "outline", "plotting", "literature", "writing", "refinement"
    )

    def __init__(self, nodes: Optional[list[PipelineNode]] = None):
        if nodes is None:
            nodes = [
                OutlineNode(),
                PlottingNode(),
                LiteratureSynthesisNode(),
                SectionWritingNode(),
                RefinementNode(),
            ]
        self._nodes: dict[str, PipelineNode] = {n.name: n for n in nodes}
        # Validate that every step in DEFAULT_ORDER is present.
        missing = [n for n in self.DEFAULT_ORDER if n not in self._nodes]
        if missing:
            raise ValueError(f"Pipeline missing nodes: {missing}")

    def replace_node(self, name: str, new_node: PipelineNode) -> None:
        """Swap a node by name. Used to replace dummies with real agents."""
        if name not in self._nodes:
            raise KeyError(f"Unknown node '{name}'.")
        self._nodes[name] = new_node

    def nodes(self) -> list[PipelineNode]:
        return [self._nodes[name] for name in self.DEFAULT_ORDER]

    def run(self, state: ManuscriptDocument,
            ctx: PipelineContext) -> ManuscriptDocument:
        for step_name in self.DEFAULT_ORDER:
            node = self._nodes[step_name]
            kind = "dummy" if node.is_dummy else "active"
            _progress(ctx, f"Step '{step_name}' ({kind}) — running…")
            state = node.run(state, ctx)
            state.completed_steps.append(step_name)
            state.step_log.append(f"{step_name}: {kind}")
            _progress(ctx, f"Step '{step_name}' — done.")
        return state


def emit_progress(ctx: PipelineContext, msg: str) -> None:
    """Public helper for nodes (incl. swapped-in real agents) to report status."""
    if ctx.on_progress:
        try:
            ctx.on_progress(msg)
        except Exception:
            pass


# Backwards-compat alias used by nodes inside this module.
_progress = emit_progress


# =====================================================================
# Helpers — building the inputs from the existing app state
# =====================================================================

def build_provided_literature(
    paper_ids: list[int], conn: sqlite3.Connection,
    per_paper_chars: int = _PER_PAPER_CHARS,
) -> tuple[str, list[BibEntry]]:
    """Render the user's selected papers as a citation-numbered text block.

    Returns (block_text, bibliography). Papers without a resolvable PDF are
    silently skipped — they don't get a citation index.
    """
    from db import database as db
    from utils.pdf_processor import extract_text

    blocks: list[str] = []
    bib: list[BibEntry] = []
    cite_idx = 0
    for pid in paper_ids:
        paper = db.get_paper_by_id(conn, pid)
        if not paper or not paper["local_file_path"]:
            continue
        try:
            text = extract_text(paper["local_file_path"])
        except Exception:
            continue
        cite_idx += 1
        title = paper["title"] or "Untitled"
        authors = paper["authors"] or "Unknown"
        year = paper["year"] or "?"
        blocks.append(
            f"### Paper [{cite_idx}]: {title}\n"
            f"Authors: {authors}\n"
            f"Year: {year}\n\n"
            f"{text[:per_paper_chars]}"
        )
        bib.append(BibEntry(
            index=cite_idx, paper_id=pid, title=title,
            authors=authors, year=year,
        ))
    return "\n\n---\n\n".join(blocks), bib


def run_default_pipeline(
    paper_ids: list[int], conn: sqlite3.Connection,
    user_ideas: str, thesis_context: str = "",
    model: Optional[str] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> ManuscriptDocument:
    """One-call helper: build inputs, run the default 5-step pipeline, return state."""
    literature_block, bib = build_provided_literature(paper_ids, conn)
    state = ManuscriptDocument(
        user_ideas=user_ideas,
        provided_literature=literature_block,
        thesis_context=thesis_context,
        bibliography=bib,
    )
    ctx = PipelineContext(
        conn=conn, model=model, paper_ids=paper_ids, on_progress=on_progress,
    )
    pipeline = Pipeline()
    return pipeline.run(state, ctx)
