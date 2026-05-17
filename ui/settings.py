import flet as ft
import sqlite3
from db import database as db


def build(conn: sqlite3.Connection, page: ft.Page) -> ft.Control:
    writing_style_field = ft.TextField(
        label="Writing style examples",
        value=db.get_setting(conn, "writing_style", ""),
        hint_text=(
            "Paste 1–3 paragraphs of your own writing (from a previous paper, "
            "your thesis intro, etc.). The AI will match this voice when "
            "generating analyses and search results."
        ),
        multiline=True,
        min_lines=10,
        max_lines=30,
        text_size=13,
        on_change=lambda e: db.set_setting(conn, "writing_style", e.control.value),
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
                ft.Text("Writing style", size=12,
                        color=ft.Colors.BLUE_GREY_300, weight=ft.FontWeight.W_500),
                ft.Text(
                    "Provide samples of your writing — the AI will mirror tone, "
                    "vocabulary, and sentence structure in its outputs (paper "
                    "summaries, cross-paper searches, etc.).",
                    size=11, color=ft.Colors.BLUE_GREY_400,
                ),
                writing_style_field,
            ],
            spacing=10,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
    )
