import flet as ft
import sqlite3
from db import database as db


SIDEBAR_WIDTH = 260

NAV_ITEMS = [
    ("Kanban Board", "kanban", ft.Icons.VIEW_KANBAN),
    ("Cross-Paper Search", "search", ft.Icons.SEARCH),
    ("Chapter Writer", "chapters", ft.Icons.EDIT_NOTE),
    ("Thesis", "thesis", ft.Icons.DESCRIPTION),
    ("Settings", "settings", ft.Icons.SETTINGS),
]


def build(
    page: ft.Page,
    conn: sqlite3.Connection,
    on_nav: callable,
    active_view: list,  # mutable single-element list so sidebar can read current state
) -> ft.Container:

    def nav_button(label: str, view_key: str, icon) -> ft.TextButton:
        def on_click(_):
            active_view[0] = view_key
            _refresh_nav()
            on_nav(view_key)

        btn = ft.TextButton(
            content=ft.Row([
                ft.Icon(icon, size=18),
                ft.Text(label, size=13),
            ], spacing=8),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=6),
                padding=ft.Padding(left=12, right=12, top=8, bottom=8),
            ),
            on_click=on_click,
        )
        return btn

    nav_buttons = {key: nav_button(label, key, icon) for label, key, icon in NAV_ITEMS}

    def _refresh_nav():
        for key, btn in nav_buttons.items():
            is_active = key == active_view[0]
            btn.style = ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=6),
                padding=ft.Padding(left=12, right=12, top=8, bottom=8),
                bgcolor=ft.Colors.BLUE_800 if is_active else ft.Colors.TRANSPARENT,
                color=ft.Colors.WHITE if is_active else ft.Colors.BLUE_100,
            )
        page.update()

    _refresh_nav()

    sync_result_text = ft.Text("", size=11, color=ft.Colors.GREEN_300, italic=True)

    def do_sync(_):
        sync_result_text.value = "Syncing..."
        sync_result_text.color = ft.Colors.BLUE_GREY_300
        page.update()
        try:
            from api import zotero_client
            added, skipped = zotero_client.sync_papers(conn)
            msg = f"Synced: {added} papers."
            if skipped:
                msg += f" {skipped} skipped (unsupported type)."
            sync_result_text.value = msg
            sync_result_text.color = ft.Colors.GREEN_300
            on_nav(active_view[0])
        except Exception as exc:
            sync_result_text.value = f"Error: {exc}"
            sync_result_text.color = ft.Colors.RED_300
        page.update()

    def do_push(_):
        sync_result_text.value = "Pushing to Zotero..."
        sync_result_text.color = ft.Colors.BLUE_GREY_300
        page.update()
        try:
            from api import zotero_client
            pushed, failed = zotero_client.push_to_zotero(conn)
            msg = f"Pushed: {pushed} items."
            if failed:
                msg += f" {failed} failed."
            sync_result_text.value = msg
            sync_result_text.color = ft.Colors.GREEN_300 if not failed else ft.Colors.ORANGE_300
        except Exception as exc:
            sync_result_text.value = f"Error: {exc}"
            sync_result_text.color = ft.Colors.RED_300
        page.update()

    def do_reset(_):
        def _close(_=None):
            page.pop_dialog()
            page.update()

        def _confirm(_):
            page.pop_dialog()
            try:
                db.reset_database(conn)
                sync_result_text.value = "Database reset. Click Sync Zotero to repopulate."
                sync_result_text.color = ft.Colors.GREEN_300
                on_nav(active_view[0])
            except Exception as exc:
                sync_result_text.value = f"Error: {exc}"
                sync_result_text.color = ft.Colors.RED_300
            page.update()

        confirm_dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Reset database?"),
            content=ft.Text(
                "This deletes all papers, comments, and AI cache from the local "
                "database. Your thesis description is kept. Zotero is NOT touched. "
                "Re-sync afterwards to repopulate.",
                size=13,
            ),
            actions=[
                ft.TextButton(content="Cancel", on_click=_close),
                ft.ElevatedButton(
                    content="Reset",
                    icon=ft.Icons.DELETE_FOREVER,
                    on_click=_confirm,
                    style=ft.ButtonStyle(bgcolor=ft.Colors.RED_700, color=ft.Colors.WHITE),
                ),
            ],
        )
        page.show_dialog(confirm_dialog)

    sync_button = ft.ElevatedButton(
        content="Sync Zotero",
        icon=ft.Icons.SYNC,
        on_click=do_sync,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=6)),
    )

    push_button = ft.ElevatedButton(
        content="Push to Zotero",
        icon=ft.Icons.CLOUD_UPLOAD,
        on_click=do_push,
        tooltip="Move papers to their Zotero sub-collections based on local Kanban position",
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=6)),
    )

    reset_button = ft.OutlinedButton(
        content="Reset Database",
        icon=ft.Icons.DELETE_FOREVER,
        on_click=do_reset,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=6),
            color=ft.Colors.RED_300,
        ),
    )

    sidebar = ft.Container(
        width=SIDEBAR_WIDTH,
        bgcolor=ft.Colors.BLUE_GREY_900,
        padding=ft.Padding(left=16, top=16, right=16, bottom=16),
        content=ft.Column(
            [
                ft.Text("ResearchAssistant", size=16, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.WHITE),
                ft.Divider(height=1, color=ft.Colors.BLUE_GREY_700),
                ft.Text("Navigation", size=11, color=ft.Colors.BLUE_GREY_400,
                        weight=ft.FontWeight.W_500),
                *nav_buttons.values(),
                ft.Divider(height=1, color=ft.Colors.BLUE_GREY_700),
                ft.Text("Zotero", size=11, color=ft.Colors.BLUE_GREY_400,
                        weight=ft.FontWeight.W_500),
                sync_button,
                push_button,
                reset_button,
                sync_result_text,
            ],
            spacing=8,
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    return sidebar
