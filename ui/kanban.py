import shutil
import subprocess
import threading
from pathlib import Path

import flet as ft
import sqlite3
from db import database as db

# Uploaded PDFs live alongside the SQLite DB, namespaced by paper id
_UPLOADS_DIR = Path(__file__).parent.parent / "uploads"

COLUMNS = [
    (0, "Trash", ft.Icons.DELETE_OUTLINE, ft.Colors.GREY_700),
    (1, "Inbox", ft.Icons.INBOX, ft.Colors.BLUE_GREY_700),
    (2, "Interesting", ft.Icons.STAR_OUTLINE, ft.Colors.TEAL_700),
    (3, "Relevant", ft.Icons.BOOKMARK_OUTLINE, ft.Colors.ORANGE_700),
    (4, "Crucial", ft.Icons.BOLT, ft.Colors.RED_700),
]

PROMPT_TEMPLATES = [
    (
        "Summarize key findings",
        "Act as an expert academic researcher. Summarize the key findings and main conclusions of this paper. Use a structured format with the following bolded headings: 1. Core Problem, 2. Main Research Questions & Hypotheses, 3. Key Results (extract specific quantitative metrics or data points), and 4. Overall Conclusion. Be highly concise and avoid filler words."
    ),
    (
        "Analyze methodology",
        "Act as a critical peer reviewer. Analyze the research methodology and design of this paper. Structure your response into: 1. Experimental Setup/Design, 2. Datasets/Sample Size, 3. Variables & Controls, and 4. Validity Assessment. In the validity section, evaluate the robustness of the methods and point out any potential biases."
    ),
    (
        "Extract key concepts",
        "Identify the core theoretical concepts, frameworks, and specific definitions introduced or heavily utilized in this paper. Format the output as a structured academic glossary: use bolded terms followed by clear, precise definitions based strictly on the provided text. Do not invent external definitions."
    ),
    (
        "Limitations & future work",
        "Extract the limitations and future research directions explicitly stated by the authors. Format these as two separate bulleted lists. Afterward, briefly act as a critical reviewer and suggest one additional potential limitation or confounding variable that the authors might have overlooked."
    ),
    (
        "Relate to thesis goals",
        "Act as my academic advisor. Using my core thesis goals provided in the system prompt, evaluate this paper's direct relevance. Provide: 1. A brief explanation of how the paper aligns with my goals, 2. Specific arguments, methodologies, or data points I can directly cite, and 3. How this paper either supports or challenges my core thesis hypothesis."
    ),
    ("Custom query", None),
]


def build(conn: sqlite3.Connection, page: ft.Page) -> ft.Control:
    board_ref = ft.Ref[ft.Row]()

    def refresh(_=None):
        board_ref.current.controls = _build_columns()
        page.update()

    # Tracks which paper's detail dialog is open (None = no dialog).
    # Read by the page-level keyboard handler to navigate Arrow keys.
    # `update_tag` is a callback the open dialog registers, so Arrow Left/Right
    # can refresh the lane badge without rebuilding the whole dialog.
    open_paper_state: dict = {"pid": None, "level": None, "update_tag": None}

    def _navigate_lane(direction: int):
        """direction: +1 = next paper in lane, -1 = previous. Wraps around."""
        pid = open_paper_state["pid"]
        level = open_paper_state["level"]
        if pid is None or level is None:
            return
        papers = db.get_papers_by_priority(conn, level)
        if not papers:
            return
        ids = [p["id"] for p in papers]
        try:
            idx = ids.index(pid)
        except ValueError:
            return
        next_paper = dict(papers[(idx + direction) % len(ids)])
        page.pop_dialog()
        _show_paper_detail(next_paper)

    def _move_open_paper(direction: int):
        """direction: -1 = move to lane on the left, +1 = right. Clamped."""
        pid = open_paper_state["pid"]
        level = open_paper_state["level"]
        if pid is None or level is None:
            return
        new_level = max(0, min(4, level + direction))
        if new_level == level:
            return
        db.set_priority(conn, pid, new_level)
        open_paper_state["level"] = new_level
        if open_paper_state["update_tag"]:
            open_paper_state["update_tag"](new_level)
        refresh()  # board re-renders the card in its new column

    def _on_key(e):
        if open_paper_state["pid"] is None:
            return
        key = e.key
        if key in ("Arrow Down", "ArrowDown"):
            _navigate_lane(+1)
        elif key in ("Arrow Up", "ArrowUp"):
            _navigate_lane(-1)
        elif key in ("Arrow Right", "ArrowRight"):
            _move_open_paper(+1)
        elif key in ("Arrow Left", "ArrowLeft"):
            _move_open_paper(-1)

    page.on_keyboard_event = _on_key

    def _lane_for_level(level):
        for lv, label, icon, color in COLUMNS:
            if lv == level:
                return label, icon, color
        return "?", ft.Icons.HELP_OUTLINE, ft.Colors.GREY_700

    def _show_paper_detail(paper: dict):
        pid = paper["id"]
        open_paper_state["pid"] = pid
        open_paper_state["level"] = paper.get("priority_level")

        notes_field = ft.TextField(
            label="Why is this paper relevant?",
            multiline=True,
            min_lines=3,
            max_lines=5,
            value=paper.get("notes") or "",
            text_size=13,
            on_change=lambda e: db.update_paper_notes(conn, pid, e.control.value),
        )

        comments_column = ft.Column([], spacing=6, scroll=ft.ScrollMode.AUTO, expand=True)
        comment_input = ft.TextField(
            label="Add a comment",
            hint_text="Write what you just found…",
            multiline=True,
            min_lines=2,
            max_lines=3,
            text_size=13,
            expand=True,
        )

        def _refresh_comments():
            comments_column.controls.clear()
            rows = db.get_comments(conn, pid)
            if not rows:
                comments_column.controls.append(
                    ft.Text("No comments yet. Add your first thought below.",
                            size=11, italic=True, color=ft.Colors.BLUE_GREY_500)
                )
            else:
                for c in rows:
                    cid = c["id"]
                    comments_column.controls.append(
                        ft.Container(
                            bgcolor=ft.Colors.BLUE_GREY_700,
                            border_radius=6,
                            padding=ft.Padding(left=10, right=8, top=8, bottom=6),
                            content=ft.Column(
                                [
                                    ft.Text(c["comment_text"], size=12, selectable=True),
                                    ft.Row(
                                        [
                                            ft.Text(c["created_at"], size=10,
                                                    color=ft.Colors.BLUE_GREY_400, italic=True),
                                            ft.IconButton(
                                                icon=ft.Icons.DELETE_OUTLINE,
                                                icon_size=14,
                                                tooltip="Delete comment",
                                                on_click=lambda _, x=cid: _delete_comment(x),
                                            ),
                                        ],
                                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                    ),
                                ],
                                spacing=2,
                                tight=True,
                            ),
                        )
                    )
            page.update()

        def _add_comment(_):
            text = (comment_input.value or "").strip()
            if not text:
                return
            db.add_comment(conn, pid, text)
            comment_input.value = ""
            _refresh_comments()

        def _delete_comment(cid: int):
            db.delete_comment(conn, cid)
            _refresh_comments()

        add_comment_btn = ft.ElevatedButton(
            content="Add Comment",
            icon=ft.Icons.ADD_COMMENT,
            on_click=_add_comment,
        )

        template_options = [
            ft.DropdownOption(key=name, text=name) for name, _ in PROMPT_TEMPLATES
        ]
        prompt_dropdown = ft.Dropdown(
            label="Prompt Template",
            options=template_options,
            value=PROMPT_TEMPLATES[0][0],
            text_size=13,
            expand=True,
            on_select=lambda e: _on_template_change(e.control.value),
        )

        from api import gemini_client as _gc
        model_dropdown = ft.Dropdown(
            label="Model",
            options=[ft.DropdownOption(key=mid, text=label)
                     for label, mid in _gc.AVAILABLE_MODELS],
            value=_gc.DEFAULT_MODEL,
            text_size=13,
            width=180,
        )

        custom_field = ft.TextField(
            label="Custom query",
            multiline=True,
            min_lines=2,
            max_lines=4,
            visible=False,
            text_size=13,
        )

        analyses_list = ft.Column([], spacing=8, scroll=ft.ScrollMode.AUTO, expand=True)

        def _label_for_prompt(prompt_text: str) -> str:
            for name, text in PROMPT_TEMPLATES:
                if text is not None and text == prompt_text:
                    return name
            return "Custom query"

        def _label_for_model(model_id: str) -> str:
            for label, mid in _gc.AVAILABLE_MODELS:
                if mid == model_id:
                    return label
            return model_id or "unknown"

        def _make_analysis_tile(row) -> ft.Container:
            aid = row["id"]
            response = row["generated_response"] or ""
            label = _label_for_prompt(row["prompt_text"] or "")
            model_label = _label_for_model(row["model_used"] or "")
            created_at = row["created_at"] or ""

            lines = response.splitlines()
            preview = "\n".join(lines[:5])
            if len(lines) > 5 or len(preview) < len(response):
                preview = preview.rstrip() + " …"

            preview_md = ft.Markdown(
                value=preview, selectable=False,
                extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            )
            full_md = ft.Markdown(
                value=response, selectable=True,
                extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                visible=False,
            )
            expand_icon = ft.Icon(ft.Icons.EXPAND_MORE, size=18,
                                  color=ft.Colors.BLUE_GREY_300)
            state = {"open": False}

            def _toggle(_):
                state["open"] = not state["open"]
                preview_md.visible = not state["open"]
                full_md.visible = state["open"]
                expand_icon.name = (ft.Icons.EXPAND_LESS if state["open"]
                                    else ft.Icons.EXPAND_MORE)
                page.update()

            def _delete(_):
                db.delete_analysis(conn, aid)
                _refresh_analyses()

            header = ft.Container(
                on_click=_toggle,
                ink=True,
                padding=ft.Padding(left=10, right=8, top=8, bottom=8),
                content=ft.Row([
                    ft.Column([
                        ft.Text(label, size=12, weight=ft.FontWeight.W_600,
                                color=ft.Colors.WHITE),
                        ft.Text(f"{created_at} · {model_label}", size=10,
                                color=ft.Colors.BLUE_GREY_400),
                    ], spacing=2, tight=True, expand=True),
                    expand_icon,
                    ft.IconButton(
                        icon=ft.Icons.DELETE_OUTLINE, icon_size=16,
                        tooltip="Delete this analysis",
                        on_click=_delete,
                    ),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            )

            body = ft.Container(
                padding=ft.Padding(left=10, right=10, top=0, bottom=10),
                content=ft.Column([preview_md, full_md], spacing=0),
            )

            return ft.Container(
                bgcolor=ft.Colors.BLUE_GREY_700,
                border_radius=6,
                content=ft.Column([header, body], spacing=0, tight=True),
            )

        def _refresh_analyses():
            analyses_list.controls.clear()
            rows = db.get_analyses_for_paper(conn, pid)
            if not rows:
                analyses_list.controls.append(
                    ft.Text("No analyses yet. Pick a prompt and click Analyze.",
                            size=11, italic=True, color=ft.Colors.BLUE_GREY_500)
                )
            else:
                for r in rows:
                    analyses_list.controls.append(_make_analysis_tile(r))
            page.update()

        busy_ring = ft.ProgressRing(width=20, height=20, visible=False)
        analyze_btn = ft.ElevatedButton(
            content="Analyze",
            icon=ft.Icons.PSYCHOLOGY,
            on_click=lambda _: _do_analyze(),
        )
        info_text = ft.Text("", size=11, color=ft.Colors.BLUE_GREY_400, italic=True)

        def _on_template_change(value: str):
            custom_field.visible = (value == "Custom query")
            page.update()

        def _do_analyze():
            if not paper.get("local_file_path"):
                info_text.value = "No PDF path linked to this paper."
                page.update()
                return

            template_name = prompt_dropdown.value
            prompt_text = None
            for name, text in PROMPT_TEMPLATES:
                if name == template_name:
                    prompt_text = text
                    break
            if prompt_text is None:
                prompt_text = custom_field.value.strip()
            if not prompt_text:
                info_text.value = "Please enter a custom query."
                page.update()
                return

            info_text.value = "Analyzing…"
            info_text.color = ft.Colors.BLUE_GREY_400
            analyze_btn.disabled = True
            busy_ring.visible = True
            page.update()

            selected_model = model_dropdown.value or _gc.DEFAULT_MODEL

            def run():
                try:
                    from api import gemini_client
                    thesis_context = db.get_thesis_context(conn)
                    _response, from_cache = gemini_client.query(
                        pid, paper["local_file_path"], prompt_text, conn,
                        thesis_context, selected_model,
                    )
                    info_text.value = "⚡ Loaded from cache." if from_cache else "🌐 Saved."
                    info_text.color = ft.Colors.GREEN_300
                    _refresh_analyses()
                except Exception as exc:
                    info_text.value = f"Error: {exc}"
                    info_text.color = ft.Colors.RED_300
                finally:
                    analyze_btn.disabled = False
                    busy_ring.visible = False
                    page.update()

            threading.Thread(target=run, daemon=True).start()

        has_pdf = bool(paper.get("local_file_path"))

        def _open_pdf(_=None):
            if paper.get("local_file_path"):
                subprocess.run(["open", paper["local_file_path"]], check=False)

        open_pdf_btn = ft.ElevatedButton(
            content="Open PDF",
            icon=ft.Icons.OPEN_IN_NEW,
            on_click=_open_pdf,
            visible=has_pdf,
        )
        upload_pdf_btn = ft.ElevatedButton(
            content="Upload PDF",
            icon=ft.Icons.UPLOAD_FILE,
            visible=not has_pdf,
            tooltip="No PDF linked. Pick one from disk to attach.",
        )

        async def _do_upload(_):
            fp = getattr(page, "file_picker", None)
            if fp is None:
                info_text.value = "File picker not available."
                info_text.color = ft.Colors.RED_300
                page.update()
                return
            try:
                files = await fp.pick_files(
                    dialog_title="Select PDF for this paper",
                    allowed_extensions=["pdf"],
                    allow_multiple=False,
                )
                if not files:
                    return
                src = Path(files[0].path)
                dest_dir = _UPLOADS_DIR / str(pid)
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / src.name
                shutil.copy2(src, dest)
                new_path = str(dest.resolve())
                db.set_paper_pdf_path(conn, pid, new_path)
                paper["local_file_path"] = new_path
                open_pdf_btn.visible = True
                upload_pdf_btn.visible = False
                info_text.value = f"✓ PDF attached: {src.name}"
                info_text.color = ft.Colors.GREEN_300
            except Exception as exc:
                info_text.value = f"Upload failed: {exc}"
                info_text.color = ft.Colors.RED_300
            finally:
                page.update()

        upload_pdf_btn.on_click = _do_upload

        left_panel = ft.Container(
            width=340,
            content=ft.Column(
                [
                    ft.Text("Notes", size=12, color=ft.Colors.BLUE_GREY_400,
                            weight=ft.FontWeight.W_500),
                    notes_field,
                    ft.Divider(height=1, color=ft.Colors.BLUE_GREY_600),
                    ft.Text("Comments", size=12, color=ft.Colors.BLUE_GREY_400,
                            weight=ft.FontWeight.W_500),
                    ft.Container(
                        expand=True,
                        content=comments_column,
                    ),
                    comment_input,
                    ft.Row([add_comment_btn], alignment=ft.MainAxisAlignment.END),
                ],
                spacing=8,
                expand=True,
            ),
        )

        right_panel = ft.Container(
            expand=True,
            content=ft.Column(
                [
                    ft.Text("AI Analysis", size=12, color=ft.Colors.BLUE_GREY_400,
                            weight=ft.FontWeight.W_500),
                    ft.Row([prompt_dropdown, model_dropdown], spacing=8),
                    custom_field,
                    ft.Row(
                        [analyze_btn, open_pdf_btn, upload_pdf_btn, busy_ring],
                        alignment=ft.MainAxisAlignment.START,
                        spacing=8,
                    ),
                    info_text,
                    ft.Divider(height=1, color=ft.Colors.BLUE_GREY_600),
                    ft.Text("Saved Analyses", size=12, color=ft.Colors.BLUE_GREY_400,
                            weight=ft.FontWeight.W_500),
                    analyses_list,
                ],
                expand=True,
                spacing=6,
            ),
        )

        authors = paper.get("authors") or ""
        year = str(paper.get("year")) if paper.get("year") else ""
        subtitle = ", ".join(p for p in [authors, year] if p)

        def _close(_=None):
            open_paper_state["pid"] = None
            open_paper_state["level"] = None
            open_paper_state["update_tag"] = None
            page.pop_dialog()
            page.update()

        title_value = paper.get("title") or "Untitled"

        async def _copy_title(_):
            clip = getattr(page, "clipboard_service", None)
            if clip is None:
                info_text.value = "Clipboard not available."
                info_text.color = ft.Colors.RED_300
                page.update()
                return
            try:
                await clip.set(title_value)
                info_text.value = "✓ Title copied to clipboard."
                info_text.color = ft.Colors.GREEN_300
            except Exception as exc:
                info_text.value = f"Copy failed: {exc}"
                info_text.color = ft.Colors.RED_300
            page.update()

        # Lane tag (colored pill showing which Kanban column this paper is in)
        lane_label, lane_icon, lane_color = _lane_for_level(paper.get("priority_level"))
        lane_icon_ctrl = ft.Icon(lane_icon, size=14, color=ft.Colors.WHITE)
        lane_label_ctrl = ft.Text(lane_label, size=11, weight=ft.FontWeight.BOLD,
                                  color=ft.Colors.WHITE)
        lane_tag = ft.Container(
            bgcolor=lane_color,
            border_radius=12,
            padding=ft.Padding(left=10, right=12, top=4, bottom=4),
            content=ft.Row([lane_icon_ctrl, lane_label_ctrl],
                           spacing=6, tight=True,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
        )

        def _update_lane_tag(new_level: int):
            label, icon, color = _lane_for_level(new_level)
            lane_icon_ctrl.name = icon
            lane_label_ctrl.value = label
            lane_tag.bgcolor = color
            page.update()

        open_paper_state["update_tag"] = _update_lane_tag

        title_row = ft.Row(
            [
                ft.Column(
                    [
                        ft.Row([lane_tag], tight=True),
                        ft.Text(title_value, size=15,
                                weight=ft.FontWeight.BOLD, selectable=True,
                                max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(subtitle, size=11, color=ft.Colors.BLUE_GREY_400,
                                selectable=True),
                    ],
                    spacing=4,
                    expand=True,
                    tight=True,
                ),
                ft.IconButton(
                    icon=ft.Icons.CONTENT_COPY,
                    tooltip="Copy title",
                    on_click=_copy_title,
                ),
                ft.IconButton(
                    icon=ft.Icons.CLOSE,
                    tooltip="Close",
                    on_click=_close,
                ),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )

        dialog = ft.AlertDialog(
            modal=False,
            title=title_row,
            content=ft.Container(
                width=1000,
                height=580,
                content=ft.Row(
                    [left_panel, ft.VerticalDivider(width=1), right_panel],
                    expand=True,
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
            ),
            actions=[
                ft.TextButton(content="Close", on_click=_close),
            ],
        )

        page.show_dialog(dialog)
        _refresh_comments()
        _refresh_analyses()

    def _paper_card(paper: sqlite3.Row) -> ft.Draggable:
        pid = paper["id"]
        title = paper["title"] or "Untitled"
        authors = paper["authors"] or ""
        year = str(paper["year"]) if paper["year"] else ""

        subtitle_parts = [p for p in [authors[:40] + ("…" if len(authors) > 40 else ""), year] if p]
        subtitle = ", ".join(subtitle_parts)

        def open_detail(_):
            fresh = db.get_paper_by_id(conn, pid)
            _show_paper_detail(dict(fresh))

        card = ft.Card(
            content=ft.Container(
                padding=ft.Padding(left=10, right=10, top=8, bottom=8),
                on_click=open_detail,
                ink=True,
                content=ft.Column(
                    [
                        ft.Text(title, size=12, weight=ft.FontWeight.W_600,
                                max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(subtitle, size=10,
                                color=ft.Colors.BLUE_GREY_300, max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS),
                    ],
                    spacing=2,
                    tight=True,
                ),
            ),
            elevation=1,
            margin=ft.Margin(left=0, top=0, right=0, bottom=6),
        )

        # Compact preview shown under the cursor while dragging
        feedback = ft.Container(
            width=240,
            bgcolor=ft.Colors.BLUE_GREY_700,
            border_radius=6,
            padding=ft.Padding(left=10, right=10, top=8, bottom=8),
            content=ft.Text(title, size=12, weight=ft.FontWeight.W_600,
                            color=ft.Colors.WHITE,
                            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
            opacity=0.9,
        )

        # The card stays visible (faded) in its original spot while being dragged
        ghost = ft.Container(opacity=0.3, content=card)

        return ft.Draggable(
            group="paper",
            data=str(pid),
            content=card,
            content_when_dragging=ghost,
            content_feedback=feedback,
            # Only start dragging on horizontal motion so vertical scrolling
            # of the column still works when the cursor is over a card.
            affinity=ft.Axis.HORIZONTAL,
        )

    def _build_column(level: int, label: str, icon, color) -> ft.Container:
        papers = db.get_papers_by_priority(conn, level)
        cards = [_paper_card(p) for p in papers]

        # Body uses ListView (built-in scroll) rather than Column scroll=AUTO,
        # which is unreliable in deeply nested expand chains on Flet 0.85.
        # Body is NOT wrapped in a DragTarget — the header below is.
        body = ft.Container(
            bgcolor=ft.Colors.BLUE_GREY_800,
            border_radius=ft.BorderRadius(top_left=0, top_right=0,
                                          bottom_left=8, bottom_right=8),
            padding=ft.Padding(left=8, right=8, top=8, bottom=8),
            expand=True,
            content=ft.ListView(
                controls=cards if cards else [
                    ft.Text("Drop on the header to move here", size=11,
                            color=ft.Colors.BLUE_GREY_500, italic=True)
                ],
                expand=True,
                spacing=0,
                auto_scroll=False,
            ),
        )

        # The colored header IS the drop zone. Drag a card up to a column
        # header to move it. This trades a slightly worse drop target (smaller
        # area) for fully working scroll in the body.
        header_inner = ft.Container(
            bgcolor=color,
            border_radius=ft.BorderRadius(top_left=8, top_right=8,
                                          bottom_left=0, bottom_right=0),
            padding=ft.Padding(left=10, right=10, top=10, bottom=10),
            content=ft.Row([
                ft.Icon(icon, size=16, color=ft.Colors.WHITE),
                ft.Text(label, size=13, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.WHITE),
                ft.Text(f"({len(papers)})", size=11, color=ft.Colors.WHITE70),
            ], spacing=6),
        )

        def _on_will_accept(e):
            # Lighten the header to signal "drop here"
            header_inner.opacity = 0.7
            page.update()

        def _on_leave(e):
            header_inner.opacity = 1.0
            page.update()

        def _on_accept(e):
            header_inner.opacity = 1.0
            src = page.get_control(e.src_id)
            if src is not None and getattr(src, "data", None):
                try:
                    pid = int(src.data)
                except (TypeError, ValueError):
                    page.update()
                    return
                paper = db.get_paper_by_id(conn, pid)
                if paper and paper["priority_level"] != level:
                    db.set_priority(conn, pid, level)
            refresh()

        header_drop = ft.DragTarget(
            group="paper",
            content=header_inner,
            on_will_accept=_on_will_accept,
            on_leave=_on_leave,
            on_accept=_on_accept,
        )

        return ft.Container(
            expand=True,
            content=ft.Column(
                [header_drop, body],
                spacing=0,
                expand=True,
            ),
        )

    def _build_columns() -> list:
        return [_build_column(level, label, icon, color)
                for level, label, icon, color in COLUMNS]

    board = ft.Row(
        ref=board_ref,
        controls=_build_columns(),
        spacing=12,
        expand=True,
        # STRETCH gives each column a bounded height so the inner Column with
        # scroll=AUTO can actually scroll when content overflows.
        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
    )

    return ft.Container(
        content=ft.Column(
            [
                ft.Text("Kanban Priority Board", size=18,
                        weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                ft.Text("Drag cards between columns to re-prioritize. Click a card to open notes and AI analysis.",
                        size=12, color=ft.Colors.BLUE_GREY_400),
                board,
            ],
            spacing=12,
            expand=True,
        ),
        padding=ft.Padding(left=16, top=16, right=16, bottom=16),
        expand=True,
    )
