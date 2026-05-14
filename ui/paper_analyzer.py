import subprocess
import threading
import flet as ft
import sqlite3
from db import database as db

PROMPT_TEMPLATES = [
    ("Summarize key findings", "Summarize the key findings and main conclusions of this paper in detail."),
    ("Analyze methodology", "Analyze the research methodology and design of this paper. What methods were used, how was the study designed, and how valid are the results?"),
    ("Extract key concepts", "Extract and explain the key concepts, theories, and definitions introduced or used in this paper."),
    ("Limitations & future work", "Identify the limitations of this study and the directions for future work mentioned by the authors."),
    ("Relate to thesis goals", "Based on my core thesis goals provided above, explain how this paper is relevant to my research. What can I directly use or cite?"),
    ("Custom query", None),
]


def build(conn: sqlite3.Connection, page: ft.Page,
          preloaded_paper: dict | None = None) -> ft.Control:

    # --- State ---
    selected_paper_id: list[int | None] = [None]

    # --- Paper selector ---
    all_papers = db.get_all_papers(conn)
    paper_options = [
        ft.DropdownOption(key=str(p["id"]), text=p["title"])
        for p in all_papers
    ]

    paper_dropdown = ft.Dropdown(
        label="Select Paper",
        options=paper_options,
        expand=True,
        text_size=13,
        on_select=lambda e: _on_paper_change(e.control.value),
    )

    if preloaded_paper and preloaded_paper.get("id"):
        paper_dropdown.value = str(preloaded_paper["id"])
        selected_paper_id[0] = preloaded_paper["id"]

    # --- Prompt selector ---
    template_options = [
        ft.DropdownOption(key=name, text=name) for name, _ in PROMPT_TEMPLATES
    ]
    prompt_dropdown = ft.Dropdown(
        label="Prompt Template",
        options=template_options,
        value=PROMPT_TEMPLATES[0][0],
        expand=True,
        text_size=13,
        on_select=lambda e: _on_template_change(e.control.value),
    )

    custom_field = ft.TextField(
        label="Custom query",
        multiline=True,
        min_lines=3,
        max_lines=6,
        visible=False,
        expand=True,
        text_size=13,
    )

    # --- Output panel ---
    cache_badge = ft.Text("", size=11, italic=True, color=ft.Colors.GREEN_300)
    output_md = ft.Markdown(
        value="*Select a paper and prompt, then click Analyze.*",
        selectable=True,
        extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
        expand=True,
    )
    output_scroll = ft.Column(
        [output_md],
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )

    busy_ring = ft.ProgressRing(width=20, height=20, visible=False)
    analyze_btn = ft.ElevatedButton(
        content="Analyze",
        icon=ft.Icons.PSYCHOLOGY,
        on_click=lambda _: _do_analyze(),
    )

    open_pdf_btn = ft.ElevatedButton(
        content="Open PDF",
        icon=ft.Icons.OPEN_IN_NEW,
        on_click=lambda _: _open_pdf(),
        disabled=True,
    )

    info_text = ft.Text("", size=11, color=ft.Colors.RED_300)

    # --- Helpers ---

    def _on_paper_change(value: str | None):
        if value:
            selected_paper_id[0] = int(value)
            paper = db.get_paper_by_id(conn, int(value))
            open_pdf_btn.disabled = not (paper and paper["local_file_path"])
            info_text.value = ""
        else:
            selected_paper_id[0] = None
            open_pdf_btn.disabled = True
        page.update()

    def _on_template_change(value: str):
        is_custom = value == "Custom query"
        custom_field.visible = is_custom
        page.update()

    def _open_pdf():
        pid = selected_paper_id[0]
        if not pid:
            return
        paper = db.get_paper_by_id(conn, pid)
        if paper and paper["local_file_path"]:
            subprocess.run(["open", paper["local_file_path"]], check=False)

    def _do_analyze():
        pid = selected_paper_id[0]
        if not pid:
            info_text.value = "Please select a paper."
            page.update()
            return

        paper = db.get_paper_by_id(conn, pid)
        if not paper or not paper["local_file_path"]:
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

    # --- Layout ---
    left_panel = ft.Container(
        width=320,
        padding=ft.Padding(left=12, top=12, right=12, bottom=12),
        bgcolor=ft.Colors.BLUE_GREY_800,
        border_radius=8,
        content=ft.Column(
            [
                ft.Text("Paper", size=12, color=ft.Colors.BLUE_GREY_400,
                        weight=ft.FontWeight.W_500),
                ft.Row([paper_dropdown], expand=True),
                open_pdf_btn,
                ft.Divider(height=1, color=ft.Colors.BLUE_GREY_600),
                ft.Text("Prompt", size=12, color=ft.Colors.BLUE_GREY_400,
                        weight=ft.FontWeight.W_500),
                ft.Row([prompt_dropdown], expand=True),
                custom_field,
                ft.Row([analyze_btn, busy_ring], alignment=ft.MainAxisAlignment.START),
                info_text,
            ],
            spacing=10,
        ),
    )

    right_panel = ft.Container(
        expand=True,
        padding=ft.Padding(left=12, top=12, right=12, bottom=12),
        bgcolor=ft.Colors.BLUE_GREY_850 if hasattr(ft.Colors, "BLUE_GREY_850") else ft.Colors.BLUE_GREY_800,
        border_radius=8,
        content=ft.Column(
            [
                ft.Row([
                    ft.Text("Analysis Output", size=12, color=ft.Colors.BLUE_GREY_400,
                            weight=ft.FontWeight.W_500),
                    cache_badge,
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Divider(height=1, color=ft.Colors.BLUE_GREY_600),
                output_scroll,
            ],
            expand=True,
            spacing=8,
        ),
    )

    return ft.Container(
        expand=True,
        padding=ft.Padding(left=16, top=16, right=16, bottom=16),
        content=ft.Column(
            [
                ft.Text("Paper Analyzer", size=18, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.WHITE),
                ft.Row(
                    [left_panel, right_panel],
                    expand=True,
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
            ],
            spacing=12,
            expand=True,
        ),
    )
