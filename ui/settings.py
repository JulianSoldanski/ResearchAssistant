import threading
import flet as ft
import sqlite3
from db import database as db


def build(conn: sqlite3.Connection, page: ft.Page) -> ft.Control:
    samples_field = ft.TextField(
        label="Writing samples (paste your text here)",
        value=db.get_setting(conn, "writing_style", ""),
        hint_text=(
            "Paste 2–4 paragraphs of your own writing. The more varied the "
            "better — e.g. one paragraph of formal argumentation, one of "
            "methodology, one of discussion."
        ),
        multiline=True,
        min_lines=8,
        max_lines=20,
        text_size=13,
        on_change=lambda e: db.set_setting(conn, "writing_style", e.control.value),
    )

    profile_field = ft.TextField(
        label="Style profile (sent to the AI as a style guide — editable)",
        value=db.get_setting(conn, "writing_style_analysis", ""),
        hint_text=(
            "Click 'Analyze writing style' to populate this with a structured "
            "description of HOW you write. Edit any line freely — only this "
            "profile is sent to the AI, never the raw samples."
        ),
        multiline=True,
        min_lines=10,
        max_lines=30,
        text_size=13,
        on_change=lambda e: db.set_setting(
            conn, "writing_style_analysis", e.control.value
        ),
    )

    info_text = ft.Text("", size=11, italic=True, color=ft.Colors.BLUE_GREY_400)
    busy_ring = ft.ProgressRing(width=18, height=18, visible=False)

    def _on_analyze(_):
        sample = (samples_field.value or "").strip()
        if not sample:
            info_text.value = "Paste a writing sample above first."
            info_text.color = ft.Colors.RED_300
            page.update()
            return

        analyze_btn.disabled = True
        busy_ring.visible = True
        info_text.value = "Analyzing writing style…"
        info_text.color = ft.Colors.BLUE_GREY_400
        page.update()

        def run():
            try:
                from api import gemini_client
                profile = gemini_client.analyze_writing_style(sample)
                profile_field.value = profile
                db.set_setting(conn, "writing_style_analysis", profile)
                info_text.value = "✓ Style profile generated. Edit it below if needed."
                info_text.color = ft.Colors.GREEN_300
            except Exception as exc:
                info_text.value = f"Error: {exc}"
                info_text.color = ft.Colors.RED_300
            finally:
                analyze_btn.disabled = False
                busy_ring.visible = False
                page.update()

        threading.Thread(target=run, daemon=True).start()

    analyze_btn = ft.ElevatedButton(
        content="(Re-)Analyze writing style",
        icon=ft.Icons.AUTO_FIX_HIGH,
        on_click=_on_analyze,
    )

    return ft.Container(
        padding=ft.Padding(left=16, top=16, right=16, bottom=16),
        expand=True,
        content=ft.Column(
            [
                ft.Text("Settings", size=18, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.WHITE),
                ft.Text(
                    "Configure how the AI responds. Auto-saves on every edit.",
                    size=12, color=ft.Colors.BLUE_GREY_400,
                ),
                ft.Divider(height=1, color=ft.Colors.BLUE_GREY_700),
                ft.Text("Writing style", size=13,
                        color=ft.Colors.BLUE_GREY_200, weight=ft.FontWeight.W_600),
                ft.Text(
                    "Paste samples of your writing, then click Analyze. The AI "
                    "extracts a structured style profile (tone, sentence structure, "
                    "vocabulary, etc.) that you can edit. Only the profile — not "
                    "the raw samples — is injected into AI prompts as a style "
                    "guide, so your phrasing won't be copied verbatim.",
                    size=11, color=ft.Colors.BLUE_GREY_400,
                ),
                samples_field,
                ft.Row(
                    [analyze_btn, busy_ring, info_text],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                profile_field,
            ],
            spacing=10,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
    )
