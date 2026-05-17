import json
import threading
import flet as ft
import sqlite3
from db import database as db

# Mirrors kanban.COLUMNS but only what we need here
LANE_DEFS = [
    (0, "Trash"),
    (1, "Inbox"),
    (2, "Interesting"),
    (3, "Relevant"),
    (4, "Crucial"),
]


def build(conn: sqlite3.Connection, page: ft.Page) -> ft.Control:
    # --- State ---
    selected_lanes: set[int] = {level for level, _ in LANE_DEFS}
    deselected_paper_ids: set[int] = set()

    # --- Paper list area (rebuilt when lanes/papers toggle) ---
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
                                on_change=lambda e, x=pid: _on_paper_toggle(x, e.control.value),
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

    # --- Lane checkboxes ---
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
            on_change=lambda e, lv=level: _on_lane_toggle(lv, e.control.value),
        )
        for level, label in LANE_DEFS
    ]

    # --- Query inputs ---
    query_input = ft.TextField(
        label="Your search query (plain language)",
        hint_text="e.g. What methodologies are used for multi-issue negotiation?",
        multiline=True,
        min_lines=2,
        max_lines=4,
        text_size=13,
    )
    optimized_input = ft.TextField(
        label="Optimized search prompt (editable)",
        hint_text="Click 'Generate Optimized Search' to populate this field.",
        multiline=True,
        min_lines=4,
        max_lines=10,
        text_size=12,
    )

    searches_list = ft.Column([], spacing=8, scroll=ft.ScrollMode.AUTO, expand=True)

    def _make_search_tile(row, expand_by_default: bool = False) -> ft.Container:
        sid = row["id"]
        raw_query = row["raw_query"] or ""
        optimized = row["optimized_query"] or ""
        response = row["response"] or ""
        created_at = row["created_at"] or ""
        try:
            paper_id_list = json.loads(row["paper_ids"]) if row["paper_ids"] else []
        except (json.JSONDecodeError, TypeError):
            paper_id_list = []

        # Resolve current titles (papers may have been deleted since)
        titles = []
        for pid in paper_id_list:
            p = db.get_paper_by_id(conn, pid)
            titles.append(p["title"] if p else f"(deleted paper #{pid})")

        # Header label: prefer the raw user query, fall back to optimized
        header_text = raw_query.strip() or optimized.strip()
        if len(header_text) > 90:
            header_text = header_text[:90].rstrip() + "…"

        papers_section = ft.Column(
            [ft.Text(f"• {t}", size=11, color=ft.Colors.BLUE_GREY_200,
                     max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
             for t in titles] or
            [ft.Text("(none)", size=11, italic=True, color=ft.Colors.BLUE_GREY_500)],
            spacing=2, tight=True,
        )

        body = ft.Container(
            padding=ft.Padding(left=10, right=10, top=0, bottom=10),
            visible=expand_by_default,
            content=ft.Column(
                [
                    ft.Text("Optimized prompt", size=10,
                            color=ft.Colors.BLUE_GREY_400, weight=ft.FontWeight.W_500),
                    ft.Container(
                        bgcolor=ft.Colors.BLUE_GREY_800,
                        border_radius=4,
                        padding=ft.Padding(left=8, right=8, top=6, bottom=6),
                        content=ft.Text(optimized, size=11,
                                        color=ft.Colors.BLUE_GREY_100, selectable=True),
                    ),
                    ft.Text(f"Papers used ({len(titles)})", size=10,
                            color=ft.Colors.BLUE_GREY_400, weight=ft.FontWeight.W_500),
                    papers_section,
                    ft.Divider(height=1, color=ft.Colors.BLUE_GREY_600),
                    ft.Text("Results", size=10,
                            color=ft.Colors.BLUE_GREY_400, weight=ft.FontWeight.W_500),
                    ft.Markdown(
                        value=response, selectable=True,
                        extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                    ),
                ],
                spacing=6, tight=True,
            ),
        )

        expand_icon = ft.Icon(
            ft.Icons.EXPAND_LESS if expand_by_default else ft.Icons.EXPAND_MORE,
            size=18, color=ft.Colors.BLUE_GREY_300,
        )
        state = {"open": expand_by_default}

        def _toggle(_):
            state["open"] = not state["open"]
            body.visible = state["open"]
            expand_icon.name = (ft.Icons.EXPAND_LESS if state["open"]
                                else ft.Icons.EXPAND_MORE)
            page.update()

        def _delete(_):
            db.delete_search(conn, sid)
            _refresh_searches()

        header = ft.Container(
            on_click=_toggle, ink=True,
            padding=ft.Padding(left=10, right=8, top=8, bottom=8),
            content=ft.Row(
                [
                    ft.Column(
                        [
                            ft.Text(header_text, size=12,
                                    weight=ft.FontWeight.W_600,
                                    color=ft.Colors.WHITE,
                                    max_lines=2,
                                    overflow=ft.TextOverflow.ELLIPSIS),
                            ft.Text(f"{created_at} · {len(titles)} papers",
                                    size=10, color=ft.Colors.BLUE_GREY_400),
                        ],
                        spacing=2, tight=True, expand=True,
                    ),
                    expand_icon,
                    ft.IconButton(
                        icon=ft.Icons.DELETE_OUTLINE, icon_size=16,
                        tooltip="Delete this search",
                        on_click=_delete,
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
        )

        return ft.Container(
            bgcolor=ft.Colors.BLUE_GREY_700,
            border_radius=6,
            content=ft.Column([header, body], spacing=0, tight=True),
        )

    def _refresh_searches(highlight_id: int | None = None):
        searches_list.controls.clear()
        rows = db.get_all_searches(conn)
        if not rows:
            searches_list.controls.append(
                ft.Text("No searches yet. Run a search to save one here.",
                        size=11, italic=True, color=ft.Colors.BLUE_GREY_500)
            )
        else:
            for r in rows:
                searches_list.controls.append(
                    _make_search_tile(r, expand_by_default=(r["id"] == highlight_id))
                )
        page.update()

    info_text = ft.Text("", size=11, italic=True, color=ft.Colors.BLUE_GREY_400)
    busy_ring = ft.ProgressRing(width=18, height=18, visible=False)

    from api import gemini_client as _gc
    model_dropdown = ft.Dropdown(
        label="Model",
        options=[ft.DropdownOption(key=mid, text=label)
                 for label, mid in _gc.AVAILABLE_MODELS],
        value=_gc.DEFAULT_MODEL,
        text_size=13,
        width=180,
    )

    # --- Actions ---
    def _set_busy(busy: bool, label: str = ""):
        busy_ring.visible = busy
        generate_btn.disabled = busy
        search_btn.disabled = busy
        if label:
            info_text.value = label
            info_text.color = ft.Colors.BLUE_GREY_400
        page.update()

    def _on_generate(_):
        raw = (query_input.value or "").strip()
        if not raw:
            info_text.value = "Type a query first."
            info_text.color = ft.Colors.RED_300
            page.update()
            return

        _set_busy(True, "Optimizing query…")

        def run():
            try:
                from api import gemini_client
                optimized_input.value = gemini_client.optimize_search_query(raw)
                info_text.value = "✓ Optimized prompt ready. Review and click Run Search."
                info_text.color = ft.Colors.GREEN_300
            except Exception as exc:
                info_text.value = f"Error: {exc}"
                info_text.color = ft.Colors.RED_300
            finally:
                _set_busy(False)

        threading.Thread(target=run, daemon=True).start()

    def _on_search(_):
        prompt = (optimized_input.value or "").strip()
        if not prompt:
            info_text.value = "Generate an optimized prompt first (or paste one)."
            info_text.color = ft.Colors.RED_300
            page.update()
            return

        paper_ids = _selected_paper_ids()
        if not paper_ids:
            info_text.value = "No papers selected."
            info_text.color = ft.Colors.RED_300
            page.update()
            return

        _set_busy(True, f"Searching across {len(paper_ids)} papers…")
        raw_query_value = (query_input.value or "").strip()
        selected_model = model_dropdown.value or _gc.DEFAULT_MODEL

        def run():
            try:
                from api import gemini_client
                thesis_context = db.get_thesis_context(conn)
                result = gemini_client.cross_paper_search(
                    paper_ids, conn, prompt, thesis_context, selected_model
                )
                sid = db.save_search(conn, raw_query_value, prompt, paper_ids, result)
                _refresh_searches(highlight_id=sid)
                info_text.value = f"✓ Done. {len(paper_ids)} papers searched."
                info_text.color = ft.Colors.GREEN_300
            except Exception as exc:
                info_text.value = f"Error: {exc}"
                info_text.color = ft.Colors.RED_300
            finally:
                _set_busy(False)

        threading.Thread(target=run, daemon=True).start()

    generate_btn = ft.ElevatedButton(
        content="Generate Optimized Search",
        icon=ft.Icons.AUTO_FIX_HIGH,
        on_click=_on_generate,
    )
    search_btn = ft.ElevatedButton(
        content="Run Search",
        icon=ft.Icons.SEARCH,
        on_click=_on_search,
    )

    # Build initial paper list and load saved searches
    _refresh_paper_list()
    _refresh_searches()

    # --- Layout ---
    left_panel = ft.Container(
        width=380,
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
            ],
            spacing=8,
            expand=True,
        ),
    )

    right_panel = ft.Container(
        expand=True,
        content=ft.Column(
            [
                query_input,
                ft.Row([generate_btn, busy_ring, info_text],
                       spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                optimized_input,
                ft.Row([search_btn, model_dropdown], spacing=8,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Divider(height=1, color=ft.Colors.BLUE_GREY_700),
                ft.Text("Saved Searches", size=11, color=ft.Colors.BLUE_GREY_400,
                        weight=ft.FontWeight.W_500),
                ft.Container(expand=True, content=searches_list),
            ],
            spacing=8,
            expand=True,
        ),
    )

    return ft.Container(
        padding=ft.Padding(left=16, top=16, right=16, bottom=16),
        expand=True,
        content=ft.Column(
            [
                ft.Text("Cross-Paper Search", size=18, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.WHITE),
                ft.Text(
                    "Search across multiple papers with Gemini Flash. Pick which lanes "
                    "and papers should be included, then generate an optimized prompt "
                    "and run the search.",
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
