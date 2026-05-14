import threading
import subprocess
import flet as ft
import sqlite3
from db import database as db

COLUMNS = [
    (0, "Trash", ft.Icons.DELETE_OUTLINE, ft.Colors.GREY_700),
    (1, "Inbox", ft.Icons.INBOX, ft.Colors.BLUE_GREY_700),
    (2, "Interesting", ft.Icons.STAR_OUTLINE, ft.Colors.TEAL_700),
    (3, "Relevant", ft.Icons.BOOKMARK_OUTLINE, ft.Colors.ORANGE_700),
    (4, "Crucial", ft.Icons.BOLT, ft.Colors.RED_700),
]

PROMPT_TEMPLATES = [
    ("Summarize key findings", "Summarize the key findings and main conclusions of this paper in detail."),
    ("Analyze methodology", "Analyze the research methodology and design of this paper. What methods were used, how was the study designed, and how valid are the results?"),
    ("Extract key concepts", "Extract and explain the key concepts, theories, and definitions introduced or used in this paper."),
    ("Limitations & future work", "Identify the limitations of this study and the directions for future work mentioned by the authors."),
    ("Relate to thesis goals", "Based on my core thesis goals provided above, explain how this paper is relevant to my research. What can I directly use or cite?"),
    ("Custom query", None),
]


def build(conn: sqlite3.Connection, page: ft.Page) -> ft.Control:
    board_ref = ft.Ref[ft.Row]()

    def refresh(_=None):
        board_ref.current.controls = _build_columns()
        page.update()

    def _show_paper_detail(paper: dict):
        pid = paper["id"]

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
            on_select=lambda e: _on_template_change(e.control.value),
        )

        custom_field = ft.TextField(
            label="Custom query",
            multiline=True,
            min_lines=2,
            max_lines=4,
            visible=False,
            text_size=13,
        )

        cache_badge = ft.Text("", size=11, italic=True)
        output_md = ft.Markdown(
            value="*Select a prompt and click Analyze.*",
            selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            expand=True,
        )

        busy_ring = ft.ProgressRing(width=20, height=20, visible=False)
        analyze_btn = ft.ElevatedButton(
            content="Analyze",
            icon=ft.Icons.PSYCHOLOGY,
            on_click=lambda _: _do_analyze(),
        )
        info_text = ft.Text("", size=11, color=ft.Colors.RED_300)

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

            info_text.value = ""
            analyze_btn.disabled = True
            busy_ring.visible = True
            output_md.value = "*Analyzing…*"
            cache_badge.value = ""
            page.update()

            def run():
                try:
                    from api import gemini_client
                    thesis_goals = db.get_setting(conn, "thesis_goals", "")
                    response, from_cache = gemini_client.query(
                        pid, paper["local_file_path"], prompt_text, conn, thesis_goals
                    )
                    output_md.value = response
                    cache_badge.value = "⚡ From cache" if from_cache else "🌐 From Gemini API"
                    cache_badge.color = ft.Colors.GREEN_300 if from_cache else ft.Colors.BLUE_300
                except Exception as exc:
                    output_md.value = f"**Error:** {exc}"
                    cache_badge.value = ""
                finally:
                    analyze_btn.disabled = False
                    busy_ring.visible = False
                    page.update()

            threading.Thread(target=run, daemon=True).start()

        has_pdf = bool(paper.get("local_file_path"))

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
                    ft.Row([prompt_dropdown], expand=True),
                    custom_field,
                    ft.Row(
                        [
                            analyze_btn,
                            ft.ElevatedButton(
                                content="Open PDF",
                                icon=ft.Icons.OPEN_IN_NEW,
                                on_click=lambda _: subprocess.run(
                                    ["open", paper["local_file_path"]], check=False
                                ),
                                disabled=not has_pdf,
                            ),
                            busy_ring,
                        ],
                        alignment=ft.MainAxisAlignment.START,
                        spacing=8,
                    ),
                    info_text,
                    ft.Row([
                        ft.Text("Output", size=12, color=ft.Colors.BLUE_GREY_400,
                                weight=ft.FontWeight.W_500),
                        cache_badge,
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    ft.Divider(height=1, color=ft.Colors.BLUE_GREY_600),
                    ft.Column([output_md], scroll=ft.ScrollMode.AUTO, expand=True),
                ],
                expand=True,
                spacing=6,
            ),
        )

        authors = paper.get("authors") or ""
        year = str(paper.get("year")) if paper.get("year") else ""
        subtitle = ", ".join(p for p in [authors, year] if p)

        def _close(_=None):
            page.pop_dialog()
            page.update()

        title_row = ft.Row(
            [
                ft.Column(
                    [
                        ft.Text(paper.get("title") or "Untitled", size=15,
                                weight=ft.FontWeight.BOLD,
                                max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(subtitle, size=11, color=ft.Colors.BLUE_GREY_400),
                    ],
                    spacing=2,
                    expand=True,
                    tight=True,
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

    def _paper_card(paper: sqlite3.Row) -> ft.Card:
        pid = paper["id"]
        level = paper["priority_level"]
        title = paper["title"] or "Untitled"
        authors = paper["authors"] or ""
        year = str(paper["year"]) if paper["year"] else ""

        subtitle_parts = [p for p in [authors[:40] + ("…" if len(authors) > 40 else ""), year] if p]

        def move_left(e):
            e.control.disabled = True
            db.set_priority(conn, pid, level - 1)
            refresh()

        def move_right(e):
            e.control.disabled = True
            db.set_priority(conn, pid, level + 1)
            refresh()

        def open_detail(_):
            fresh = db.get_paper_by_id(conn, pid)
            _show_paper_detail(dict(fresh))

        return ft.Card(
            content=ft.Container(
                padding=ft.Padding(left=10, right=10, top=8, bottom=8),
                on_click=open_detail,
                ink=True,
                content=ft.Column(
                    [
                        ft.Text(title, size=12, weight=ft.FontWeight.W_600,
                                max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(", ".join(subtitle_parts), size=10,
                                color=ft.Colors.BLUE_GREY_300, max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Row(
                            [
                                ft.IconButton(
                                    icon=ft.Icons.CHEVRON_LEFT, icon_size=16,
                                    tooltip="Move left",
                                    disabled=(level <= 0),
                                    on_click=move_left,
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.CHEVRON_RIGHT, icon_size=16,
                                    tooltip="Move right",
                                    disabled=(level >= 4),
                                    on_click=move_right,
                                ),
                            ],
                            spacing=0,
                            alignment=ft.MainAxisAlignment.END,
                        ),
                    ],
                    spacing=2,
                    tight=True,
                ),
            ),
            elevation=1,
            margin=ft.Margin(left=0, top=0, right=0, bottom=6),
        )

    def _build_column(level: int, label: str, icon, color) -> ft.Container:
        papers = db.get_papers_by_priority(conn, level)
        cards = [_paper_card(p) for p in papers]

        return ft.Container(
            expand=True,
            content=ft.Column(
                [
                    ft.Container(
                        bgcolor=color,
                        border_radius=ft.BorderRadius(top_left=8, top_right=8,
                                                      bottom_left=0, bottom_right=0),
                        padding=ft.Padding(left=10, right=10, top=6, bottom=6),
                        content=ft.Row([
                            ft.Icon(icon, size=16, color=ft.Colors.WHITE),
                            ft.Text(label, size=13, weight=ft.FontWeight.BOLD,
                                    color=ft.Colors.WHITE),
                            ft.Text(f"({len(papers)})", size=11, color=ft.Colors.WHITE70),
                        ], spacing=6),
                    ),
                    ft.Container(
                        bgcolor=ft.Colors.BLUE_GREY_800,
                        border_radius=ft.BorderRadius(top_left=0, top_right=0,
                                                      bottom_left=8, bottom_right=8),
                        padding=ft.Padding(left=8, right=8, top=8, bottom=8),
                        expand=True,
                        content=ft.Column(
                            cards if cards else [
                                ft.Text("No papers", size=11,
                                        color=ft.Colors.BLUE_GREY_500, italic=True)
                            ],
                            scroll=ft.ScrollMode.AUTO,
                            spacing=0,
                            expand=True,
                        ),
                    ),
                ],
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
        vertical_alignment=ft.CrossAxisAlignment.START,
    )

    return ft.Container(
        content=ft.Column(
            [
                ft.Text("Kanban Priority Board", size=18,
                        weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                ft.Text("Click a card to open notes and AI analysis. Use arrows to move between stages.",
                        size=12, color=ft.Colors.BLUE_GREY_400),
                board,
            ],
            spacing=12,
            expand=True,
        ),
        padding=ft.Padding(left=16, top=16, right=16, bottom=16),
        expand=True,
    )
