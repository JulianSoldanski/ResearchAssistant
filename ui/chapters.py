"""Chapter Writer — multi-agent manuscript pipeline (PaperOrchestra-style).

Drives `api.paper_orchestra`: the user picks papers + types raw ideas, then
the pipeline runs five steps end-to-end. Steps 1/3/4 call Gemini; Step 2
(Plotting) and Step 5 (Refinement) are filler placeholders kept in the
pipeline so a real agent can be swapped in later. Each step has its own
card showing live status and the text it contributed.
"""
import threading

import flet as ft
import sqlite3

from db import database as db
from api import paper_orchestra as orchestra


LANE_DEFS = [
    (0, "Trash"),
    (1, "Inbox"),
    (2, "Interesting"),
    (3, "Relevant"),
    (4, "Crucial"),
]

STEP_DEFS = [
    ("outline",    "1. Outline Agent",              "active"),
    ("plotting",   "2. Plotting Agent",             "dummy"),
    ("literature", "3. Literature Synthesis Agent", "active"),
    ("writing",    "4. Section Writing Agent",      "active"),
    ("refinement", "5. Refinement Agent",           "dummy"),
]


def build(conn: sqlite3.Connection, page: ft.Page) -> ft.Control:
    # =================================================================
    # Paper picker (same pattern as cross_search.py)
    # =================================================================
    selected_lanes: set[int] = {level for level, _ in LANE_DEFS}
    deselected_paper_ids: set[int] = set()

    paper_count_text = ft.Text("", size=11, color=ft.Colors.BLUE_GREY_400, italic=True)
    paper_list_column = ft.Column([], spacing=2, scroll=ft.ScrollMode.AUTO, expand=True)

    def _papers_in_selected_lanes() -> list[sqlite3.Row]:
        rows = []
        for level in sorted(selected_lanes):
            rows.extend(db.get_papers_by_priority(conn, level))
        return rows

    def _selected_paper_ids() -> list[int]:
        return [
            p["id"] for p in _papers_in_selected_lanes()
            if p["id"] not in deselected_paper_ids
        ]

    def _update_count():
        total_rows = _papers_in_selected_lanes()
        selected = [p for p in total_rows if p["id"] not in deselected_paper_ids]
        with_pdf = [p for p in selected if p["local_file_path"]]
        paper_count_text.value = (
            f"{len(selected)} of {len(total_rows)} selected · "
            f"{len(with_pdf)} have a PDF"
        )

    def _on_paper_toggle(pid: int, checked: bool):
        if checked:
            deselected_paper_ids.discard(pid)
        else:
            deselected_paper_ids.add(pid)
        _update_count()
        page.update()

    def _refresh_paper_list():
        paper_list_column.controls.clear()
        for level in sorted(selected_lanes):
            papers = db.get_papers_by_priority(conn, level)
            if not papers:
                continue
            lane_label = next(label for lv, label in LANE_DEFS if lv == level)
            paper_list_column.controls.append(
                ft.Text(f"— {lane_label} ({len(papers)})", size=11,
                        color=ft.Colors.BLUE_GREY_400, weight=ft.FontWeight.W_500)
            )
            for p in papers:
                pid = p["id"]
                title = p["title"] or "Untitled"
                has_pdf = bool(p["local_file_path"])
                paper_list_column.controls.append(
                    ft.Row(
                        [
                            ft.Checkbox(
                                value=(pid not in deselected_paper_ids),
                                on_change=lambda e, x=pid: _on_paper_toggle(x, str(e.data).lower() == "true"),
                            ),
                            ft.Text(
                                title + ("" if has_pdf else "  (no PDF)"),
                                size=12,
                                color=ft.Colors.WHITE if has_pdf else ft.Colors.BLUE_GREY_500,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                                expand=True,
                            ),
                        ],
                        spacing=2,
                    )
                )
        _update_count()
        page.update()

    def _on_lane_toggle(level: int, checked: bool):
        if checked:
            selected_lanes.add(level)
        else:
            selected_lanes.discard(level)
        _refresh_paper_list()

    lane_checkboxes = [
        ft.Checkbox(
            label=label,
            value=True,
            on_change=lambda e, lv=level: _on_lane_toggle(lv, str(e.data).lower() == "true"),
        )
        for level, label in LANE_DEFS
    ]

    # =================================================================
    # User ideas textarea — raw research notes feeding Step 1
    # =================================================================
    user_ideas_field = ft.TextField(
        label="Raw research notes / ideas (Step 1 input)",
        value=db.get_setting(conn, "orchestra_user_ideas", ""),
        hint_text=(
            "Dump your raw notes here: experiment logs, hypotheses, half-"
            "formed arguments, what you want the paper to say. The Outline "
            "Agent turns this into a structured plan."
        ),
        multiline=True,
        min_lines=8,
        max_lines=20,
        text_size=13,
        on_change=lambda e: db.set_setting(conn, "orchestra_user_ideas", e.control.value),
    )

    # Model selector
    from api import gemini_client as _gc
    model_dropdown = ft.Dropdown(
        label="Model",
        options=[ft.DropdownOption(key=mid, text=label)
                 for label, mid in _gc.AVAILABLE_MODELS],
        value=_gc.DEFAULT_MODEL,
        text_size=13,
        width=200,
    )

    # =================================================================
    # Step status cards — one per pipeline node
    # =================================================================
    step_cards: dict[str, dict] = {}  # name -> {"icon", "status", "body"}

    def _build_step_card(step_name: str, label: str, kind: str) -> ft.Control:
        # Status icon (pending → ⏳, running → spinner, done → ✓, dummy → 📎)
        status_icon = ft.Icon(
            ft.Icons.RADIO_BUTTON_UNCHECKED,
            size=18, color=ft.Colors.BLUE_GREY_400,
        )
        status_text = ft.Text(
            "Pending" + (" (dummy)" if kind == "dummy" else ""),
            size=11, italic=True, color=ft.Colors.BLUE_GREY_400,
        )
        body_field = ft.TextField(
            value="",
            hint_text=(
                "Placeholder text will appear here when the pipeline runs."
                if kind == "dummy" else
                "Agent output will appear here."
            ),
            multiline=True,
            min_lines=3,
            max_lines=12,
            read_only=False,   # let the user tweak between runs if they want
            text_size=12,
            border=ft.InputBorder.OUTLINE,
        )
        kind_badge_label = ft.Text(kind.upper(), size=10,
                                   color=ft.Colors.WHITE,
                                   weight=ft.FontWeight.W_600)
        kind_badge = ft.Container(
            bgcolor=(ft.Colors.AMBER_700 if kind == "dummy"
                     else ft.Colors.BLUE_700),
            border_radius=4,
            padding=ft.Padding(left=6, right=6, top=2, bottom=2),
            content=kind_badge_label,
        )
        header = ft.Row(
            [
                status_icon,
                ft.Text(label, size=13, weight=ft.FontWeight.W_600,
                        color=ft.Colors.WHITE, expand=True),
                kind_badge,
                status_text,
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        card = ft.Container(
            bgcolor=ft.Colors.BLUE_GREY_800,
            border_radius=6,
            padding=ft.Padding(left=10, right=10, top=8, bottom=8),
            content=ft.Column([header, body_field],
                              spacing=6, tight=True),
        )
        step_cards[step_name] = {
            "icon": status_icon, "status": status_text,
            "body": body_field,
            "badge": kind_badge, "badge_label": kind_badge_label,
        }
        return card

    step_cards_column = ft.Column(
        [_build_step_card(name, label, kind) for name, label, kind in STEP_DEFS],
        spacing=8,
    )

    # Run-pipeline controls
    run_status = ft.Text("", size=11, italic=True, color=ft.Colors.BLUE_GREY_400)
    run_busy = ft.ProgressRing(width=14, height=14, visible=False)
    run_btn = ft.ElevatedButton(
        content="Run Pipeline",
        icon=ft.Icons.PLAY_ARROW,
        style=ft.ButtonStyle(bgcolor=ft.Colors.BLUE_700, color=ft.Colors.WHITE),
    )

    def _reset_step_cards():
        for name, label, kind in STEP_DEFS:
            c = step_cards[name]
            c["icon"].name = ft.Icons.RADIO_BUTTON_UNCHECKED
            c["icon"].color = ft.Colors.BLUE_GREY_400
            c["status"].value = (
                "Pending" + (" (dummy)" if kind == "dummy" else "")
            )
            c["status"].color = ft.Colors.BLUE_GREY_400
            c["body"].value = ""

    def _mark_running(step_name: str):
        c = step_cards[step_name]
        c["icon"].name = ft.Icons.AUTORENEW
        c["icon"].color = ft.Colors.BLUE_300
        c["status"].value = "Running…"
        c["status"].color = ft.Colors.BLUE_300

    def _mark_done(step_name: str):
        c = step_cards[step_name]
        c["icon"].name = ft.Icons.CHECK_CIRCLE
        c["icon"].color = ft.Colors.GREEN_300
        c["status"].value = "Done"
        c["status"].color = ft.Colors.GREEN_300

    def _mark_error(step_name: str, message: str):
        c = step_cards[step_name]
        c["icon"].name = ft.Icons.ERROR_OUTLINE
        c["icon"].color = ft.Colors.RED_300
        c["status"].value = message
        c["status"].color = ft.Colors.RED_300

    def _fill_step_bodies(state: orchestra.ManuscriptDocument):
        step_cards["outline"]["body"].value = state.structured_outline
        step_cards["plotting"]["body"].value = state.plot_placeholders
        step_cards["literature"]["body"].value = state.literature_review
        # Writing covers three subsections — concatenate for the card.
        writing_parts = []
        if state.drafted_methodology:
            writing_parts.append("## Methodology\n" + state.drafted_methodology)
        if state.drafted_experiments:
            writing_parts.append("## Experiments\n" + state.drafted_experiments)
        if state.drafted_conclusion:
            writing_parts.append("## Conclusion\n" + state.drafted_conclusion)
        step_cards["writing"]["body"].value = "\n\n".join(writing_parts)
        step_cards["refinement"]["body"].value = state.refinement_feedback

    def _on_run(_):
        paper_ids = _selected_paper_ids()
        ideas = (user_ideas_field.value or "").strip()
        if not ideas:
            run_status.value = "Type some raw ideas first — the Outline Agent needs them."
            run_status.color = ft.Colors.RED_300
            page.update()
            return
        if not paper_ids:
            run_status.value = (
                "Pick at least one paper — the Literature Synthesis Agent "
                "needs sources to work from."
            )
            run_status.color = ft.Colors.RED_300
            page.update()
            return

        _reset_step_cards()
        run_btn.disabled = True
        run_busy.visible = True
        run_status.value = "Starting pipeline…"
        run_status.color = ft.Colors.BLUE_GREY_300
        page.update()

        # Track which step the progress callback is currently on so we can
        # flip the corresponding card icon. The orchestrator emits messages
        # like "Step 'outline' (active) — running…".
        active_step = {"name": None}

        def on_progress(msg: str):
            run_status.value = msg
            run_status.color = ft.Colors.BLUE_GREY_300
            for name, _label, _kind in STEP_DEFS:
                token = f"'{name}'"
                if token in msg:
                    if "running" in msg.lower():
                        active_step["name"] = name
                        _mark_running(name)
                    elif "done" in msg.lower():
                        _mark_done(name)
                    break
            page.update()

        selected_model = model_dropdown.value or _gc.DEFAULT_MODEL

        def run():
            try:
                thesis_context = db.get_thesis_context(conn)
                literature_block, bib = orchestra.build_provided_literature(
                    paper_ids, conn,
                )
                state = orchestra.ManuscriptDocument(
                    user_ideas=ideas,
                    provided_literature=literature_block,
                    thesis_context=thesis_context,
                    bibliography=bib,
                )
                ctx = orchestra.PipelineContext(
                    conn=conn, model=selected_model,
                    paper_ids=paper_ids, on_progress=on_progress,
                )
                pipeline = orchestra.Pipeline()
                state = pipeline.run(state, ctx)
                _fill_step_bodies(state)
                run_status.value = "Pipeline finished."
                run_status.color = ft.Colors.GREEN_300
            except Exception as exc:
                # Mark the currently-running step as the failure point.
                if active_step["name"]:
                    _mark_error(active_step["name"], f"Error: {exc}")
                run_status.value = f"Pipeline failed: {exc}"
                run_status.color = ft.Colors.RED_300
            finally:
                run_btn.disabled = False
                run_busy.visible = False
                page.update()

        threading.Thread(target=run, daemon=True).start()

    run_btn.on_click = _on_run

    # Initial population
    _refresh_paper_list()

    # =================================================================
    # Layout
    # =================================================================
    left_panel = ft.Container(
        width=360,
        content=ft.Column(
            [
                ft.Text("Lanes", size=11, color=ft.Colors.BLUE_GREY_400,
                        weight=ft.FontWeight.W_500),
                ft.Row(lane_checkboxes, spacing=4, wrap=True),
                ft.Divider(height=1, color=ft.Colors.BLUE_GREY_700),
                ft.Row(
                    [
                        ft.Text("Papers", size=11, color=ft.Colors.BLUE_GREY_400,
                                weight=ft.FontWeight.W_500),
                        paper_count_text,
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                ft.Container(
                    bgcolor=ft.Colors.BLUE_GREY_800,
                    border_radius=6,
                    padding=ft.Padding(left=6, right=6, top=6, bottom=6),
                    expand=True,
                    content=paper_list_column,
                ),
                ft.Divider(height=1, color=ft.Colors.BLUE_GREY_700),
                model_dropdown,
                ft.Row(
                    [run_btn, run_busy],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                run_status,
            ],
            spacing=8,
            expand=True,
        ),
    )

    right_panel = ft.Container(
        expand=True,
        content=ft.Column(
            [
                user_ideas_field,
                ft.Divider(height=1, color=ft.Colors.BLUE_GREY_700),
                ft.Text("Pipeline steps", size=13, weight=ft.FontWeight.W_600,
                        color=ft.Colors.WHITE),
                step_cards_column,
            ],
            spacing=10,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    return ft.Container(
        padding=ft.Padding(left=16, top=16, right=16, bottom=16),
        expand=True,
        content=ft.Column(
            [
                ft.Text("Chapter Writer — Multi-Agent Pipeline",
                        size=18, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.WHITE),
                ft.Text(
                    "PaperOrchestra-style pipeline: Outline → Plotting "
                    "(filler) → Literature Synthesis → Section Writing → "
                    "Refinement (dummy). The Outline, Literature and "
                    "Writing agents call Gemini; Plotting and Refinement "
                    "inject placeholder text but stay wired in so real "
                    "agents can be swapped in later.",
                    size=12, color=ft.Colors.BLUE_GREY_400,
                ),
                ft.Row(
                    [left_panel, right_panel],
                    expand=True,
                    spacing=16,
                    vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
            ],
            spacing=10,
            expand=True,
        ),
    )
