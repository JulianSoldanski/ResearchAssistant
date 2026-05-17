import flet as ft
import sqlite3
from db import database as db

# (label, settings key, hint placeholder)
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


def build(conn: sqlite3.Connection, page: ft.Page) -> ft.Control:
    fields = []
    for label, key, hint in SECTIONS:
        fields.append(ft.TextField(
            label=label,
            value=db.get_setting(conn, key, ""),
            hint_text=hint,
            multiline=True,
            min_lines=6,
            max_lines=20,
            text_size=13,
            on_change=lambda e, k=key: db.set_setting(conn, k, e.control.value),
        ))

    return ft.Container(
        padding=ft.Padding(left=16, top=16, right=16, bottom=16),
        expand=True,
        content=ft.Column(
            [
                ft.Text("Thesis", size=18, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.WHITE),
                ft.Text("Capture the structural parts of your thesis. Auto-saves on every edit.",
                        size=12, color=ft.Colors.BLUE_GREY_400),
                ft.Divider(height=1, color=ft.Colors.BLUE_GREY_700),
                *fields,
            ],
            spacing=14,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
    )
