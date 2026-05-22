import threading
import flet as ft
import sqlite3
from db import database as db

# (display label, settings key, hint placeholder).
# The plain label sent to the AI is the display label minus the leading "N. " prefix.
SECTIONS = [
    ("1. Problem", "thesis_problem",
     "What problem are you addressing? Why does it matter?"),
    ("2. Research Questions", "thesis_research_questions",
     "List the specific research questions your thesis answers."),
    ("3. Methodology", "thesis_methodology",
     "Describe the methods, approach, and data sources."),
    ("4. Rough Outline", "thesis_outline",
     "Chapter structure, major sections, milestones."),
    ("5. Data Management Plan", "thesis_data_management",
     "How data will be collected, stored, shared, and archived."),
]


def _plain_label(display_label: str) -> str:
    """Strip the leading "N. " prefix from a section label."""
    parts = display_label.split(". ", 1)
    return parts[1] if len(parts) == 2 else display_label


def build(conn: sqlite3.Connection, page: ft.Page) -> ft.Control:
    field_controls: dict[str, ft.TextField] = {}

    def _build_section(display_label: str, key: str, hint: str) -> ft.Control:
        plain = _plain_label(display_label)

        field = ft.TextField(
            label=display_label,
            value=db.get_setting(conn, key, ""),
            hint_text=hint,
            multiline=True,
            min_lines=6,
            max_lines=20,
            text_size=13,
            on_change=lambda e, k=key: db.set_setting(conn, k, e.control.value),
        )
        field_controls[key] = field

        info = ft.Text("", size=10, italic=True, color=ft.Colors.BLUE_GREY_400)
        busy = ft.ProgressRing(width=14, height=14, visible=False)
        gen_btn = ft.ElevatedButton(
            content="Generate / Improve with AI",
            icon=ft.Icons.AUTO_AWESOME,
        )

        def _on_generate(_):
            # Gather all OTHER non-empty sections as context
            other_drafts: dict[str, str] = {}
            for d_lbl, s_key, _h in SECTIONS:
                if s_key == key:
                    continue
                txt = (field_controls[s_key].value or "").strip()
                if txt:
                    other_drafts[_plain_label(d_lbl)] = txt

            existing = (field.value or "").strip()
            gen_btn.disabled = True
            busy.visible = True
            info.value = f"Generating {plain}…"
            info.color = ft.Colors.BLUE_GREY_400
            page.update()

            def run():
                try:
                    from api import gemini_client
                    style_profile = db.get_setting(conn, "writing_style_analysis", "")
                    result = gemini_client.generate_thesis_section(
                        plain, other_drafts, existing, style_profile,
                    )
                    field.value = result
                    db.set_setting(conn, key, result)
                    info.value = "✓ Done."
                    info.color = ft.Colors.GREEN_300
                except Exception as exc:
                    info.value = f"Error: {exc}"
                    info.color = ft.Colors.RED_300
                finally:
                    gen_btn.disabled = False
                    busy.visible = False
                    page.update()

            threading.Thread(target=run, daemon=True).start()

        gen_btn.on_click = _on_generate

        return ft.Column(
            [
                field,
                ft.Row(
                    [gen_btn, busy, info],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
            spacing=4,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    sections_ui = [_build_section(lbl, key, hint) for lbl, key, hint in SECTIONS]

    return ft.Container(
        padding=ft.Padding(left=16, top=16, right=16, bottom=16),
        expand=True,
        content=ft.Column(
            [
                ft.Text("Thesis", size=18, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.WHITE),
                ft.Text(
                    "Capture the structural parts of your thesis. Auto-saves on every edit. "
                    "Click 'Generate / Improve with AI' on a section to draft or refine it — "
                    "the AI uses the other sections as context.",
                    size=12, color=ft.Colors.BLUE_GREY_400,
                ),
                ft.Divider(height=1, color=ft.Colors.BLUE_GREY_700),
                *sections_ui,
            ],
            spacing=14,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
    )
