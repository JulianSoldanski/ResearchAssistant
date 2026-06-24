"""LaTeX chapter editor.

Points at an external folder containing one .tex file per chapter and
lets the user load / edit / save those files from inside the app.
Editing is explicit (Save / Reload) so working in parallel with VS Code
or TeXShop is safe — no auto-save races.
"""
import subprocess
from datetime import datetime
from pathlib import Path

import flet as ft
import sqlite3

from db import database as db


SETTING_DIR = "latex_dir"
SETTING_ACTIVE = "latex_active_file"


def build(conn: sqlite3.Connection, page: ft.Page) -> ft.Control:
    # --- Mutable state held across closures ---
    active_path: dict[str, Path | None] = {"value": None}
    on_disk_text: dict[str, str] = {"value": ""}
    chapter_buttons: dict[str, ft.TextButton] = {}

    # --- Controls (declared early so closures can reference them) ---
    path_field = ft.TextField(
        label="LaTeX folder (absolute path)",
        value=db.get_setting(conn, SETTING_DIR, ""),
        hint_text="/Users/you/Thesis/latex",
        text_size=13,
        expand=True,
    )

    chapter_list_column = ft.Column(
        [], spacing=2, scroll=ft.ScrollMode.AUTO, expand=True,
    )

    editor_field = ft.TextField(
        label="No chapter loaded",
        value="",
        multiline=True,
        min_lines=24,
        max_lines=None,
        text_size=13,
        read_only=True,
        border=ft.InputBorder.OUTLINE,
        expand=True,
    )

    dirty_text = ft.Text(
        "saved", size=11, italic=True, color=ft.Colors.BLUE_GREY_400,
    )
    status_text = ft.Text(
        "", size=11, italic=True, color=ft.Colors.BLUE_GREY_400,
    )

    # --- Helpers ---
    def _set_status(msg: str, color=ft.Colors.BLUE_GREY_400):
        status_text.value = msg
        status_text.color = color

    def _is_dirty() -> bool:
        if active_path["value"] is None:
            return False
        return (editor_field.value or "") != on_disk_text["value"]

    def _update_dirty_marker():
        if active_path["value"] is None:
            dirty_text.value = ""
            return
        if _is_dirty():
            dirty_text.value = "● unsaved"
            dirty_text.color = ft.Colors.AMBER_300
        else:
            dirty_text.value = "saved"
            dirty_text.color = ft.Colors.BLUE_GREY_400

    def _resolve_dir() -> Path | None:
        raw = (path_field.value or "").strip()
        if not raw:
            return None
        p = Path(raw).expanduser()
        return p

    def _scan_tex_files() -> list[Path]:
        d = _resolve_dir()
        if d is None or not d.is_dir():
            return []
        return sorted(
            (f for f in d.iterdir() if f.is_file() and f.suffix.lower() == ".tex"),
            key=lambda f: f.name.lower(),
        )

    def _refresh_button_styles():
        active_name = active_path["value"].name if active_path["value"] else None
        for name, btn in chapter_buttons.items():
            is_active = name == active_name
            btn.style = ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=6),
                padding=ft.Padding(left=10, right=10, top=6, bottom=6),
                bgcolor=ft.Colors.BLUE_800 if is_active else ft.Colors.TRANSPARENT,
                color=ft.Colors.WHITE if is_active else ft.Colors.BLUE_100,
            )

    def _load_file(path: Path):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            _set_status(f"Failed to read {path.name}: {exc}", ft.Colors.RED_300)
            page.update()
            return
        active_path["value"] = path
        on_disk_text["value"] = text
        editor_field.value = text
        editor_field.label = path.name
        editor_field.read_only = False
        db.set_setting(conn, SETTING_ACTIVE, path.name)
        _refresh_button_styles()
        _update_dirty_marker()
        _set_status(f"Loaded {path.name}.")
        page.update()

    def _refresh_chapter_list():
        chapter_list_column.controls.clear()
        chapter_buttons.clear()
        d = _resolve_dir()
        if d is None:
            chapter_list_column.controls.append(
                ft.Text(
                    "Set a folder above to list chapters.",
                    size=11, italic=True, color=ft.Colors.BLUE_GREY_400,
                )
            )
            page.update()
            return
        if not d.exists():
            _set_status(f"Folder does not exist: {d}", ft.Colors.RED_300)
            chapter_list_column.controls.append(
                ft.Text(
                    "Folder does not exist.",
                    size=11, italic=True, color=ft.Colors.RED_300,
                )
            )
            page.update()
            return
        if not d.is_dir():
            _set_status(f"Not a directory: {d}", ft.Colors.RED_300)
            chapter_list_column.controls.append(
                ft.Text(
                    "Path is not a directory.",
                    size=11, italic=True, color=ft.Colors.RED_300,
                )
            )
            page.update()
            return

        tex_files = _scan_tex_files()
        if not tex_files:
            chapter_list_column.controls.append(
                ft.Text(
                    "No .tex files in this folder.",
                    size=11, italic=True, color=ft.Colors.BLUE_GREY_400,
                )
            )
            page.update()
            return

        for f in tex_files:
            name = f.name

            def _on_pick(_e, path=f):
                _load_file(path)

            btn = ft.TextButton(
                content=ft.Row(
                    [
                        ft.Icon(ft.Icons.ARTICLE_OUTLINED, size=16),
                        ft.Text(name, size=12, max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS, expand=True),
                    ],
                    spacing=8,
                ),
                on_click=_on_pick,
            )
            chapter_buttons[name] = btn
            chapter_list_column.controls.append(btn)

        _refresh_button_styles()
        page.update()

    # --- Path field handlers ---
    def _on_path_change(e):
        db.set_setting(conn, SETTING_DIR, e.control.value or "")
        _refresh_chapter_list()

    path_field.on_change = _on_path_change

    async def _on_browse(_):
        fp = getattr(page, "file_picker", None)
        if fp is None:
            _set_status("File picker not available.", ft.Colors.RED_300)
            page.update()
            return
        try:
            result = await fp.pick_directory(
                dialog_title="Select LaTeX chapter folder",
            )
            if not result:
                return
            path_field.value = result
            db.set_setting(conn, SETTING_DIR, result)
            _refresh_chapter_list()
            page.update()
        except Exception as exc:
            _set_status(f"Browse failed: {exc}", ft.Colors.RED_300)
            page.update()

    browse_btn = ft.ElevatedButton(
        content="Browse…",
        icon=ft.Icons.FOLDER_OPEN,
        on_click=_on_browse,
    )

    # --- Editor change handler ---
    def _on_editor_change(_e):
        _update_dirty_marker()
        page.update()

    editor_field.on_change = _on_editor_change

    # --- Action buttons ---
    def _on_save(_):
        path = active_path["value"]
        if path is None:
            _set_status("No chapter loaded.", ft.Colors.RED_300)
            page.update()
            return
        text = editor_field.value or ""
        try:
            path.write_text(text, encoding="utf-8")
        except Exception as exc:
            _set_status(f"Save failed: {exc}", ft.Colors.RED_300)
            page.update()
            return
        on_disk_text["value"] = text
        _update_dirty_marker()
        _set_status(
            f"Saved {path.name} at {datetime.now().strftime('%H:%M:%S')}",
            ft.Colors.GREEN_300,
        )
        page.update()

    def _do_reload():
        path = active_path["value"]
        if path is None:
            return
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            _set_status(f"Reload failed: {exc}", ft.Colors.RED_300)
            page.update()
            return
        on_disk_text["value"] = text
        editor_field.value = text
        _update_dirty_marker()
        _set_status(f"Reloaded {path.name}.", ft.Colors.GREEN_300)
        page.update()

    def _on_reload(_):
        path = active_path["value"]
        if path is None:
            _set_status("No chapter loaded.", ft.Colors.RED_300)
            page.update()
            return

        if not _is_dirty():
            _do_reload()
            return

        def _close(_e=None):
            page.pop_dialog()
            page.update()

        def _confirm(_e):
            page.pop_dialog()
            _do_reload()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Discard unsaved changes?"),
            content=ft.Text(
                f"{path.name} has unsaved edits. Reloading from disk will "
                "overwrite them. Continue?",
                size=13,
            ),
            actions=[
                ft.TextButton(content="Cancel", on_click=_close),
                ft.ElevatedButton(
                    content="Discard & Reload",
                    icon=ft.Icons.REFRESH,
                    on_click=_confirm,
                    style=ft.ButtonStyle(
                        bgcolor=ft.Colors.RED_700, color=ft.Colors.WHITE,
                    ),
                ),
            ],
        )
        page.show_dialog(dialog)

    def _on_open_externally(_):
        path = active_path["value"]
        if path is None:
            _set_status("No chapter loaded.", ft.Colors.RED_300)
            page.update()
            return
        try:
            subprocess.Popen(["open", str(path)])
            _set_status(f"Opened {path.name} in default editor.")
        except Exception as exc:
            _set_status(f"Open failed: {exc}", ft.Colors.RED_300)
        page.update()

    def _on_refresh_list(_):
        _refresh_chapter_list()
        _set_status("Chapter list refreshed.")
        page.update()

    save_btn = ft.ElevatedButton(
        content="Save", icon=ft.Icons.SAVE,
        on_click=_on_save,
        style=ft.ButtonStyle(bgcolor=ft.Colors.BLUE_700, color=ft.Colors.WHITE),
    )
    reload_btn = ft.OutlinedButton(
        content="Reload", icon=ft.Icons.REFRESH, on_click=_on_reload,
    )
    open_external_btn = ft.OutlinedButton(
        content="Open externally", icon=ft.Icons.OPEN_IN_NEW,
        on_click=_on_open_externally,
    )
    refresh_list_btn = ft.OutlinedButton(
        content="Refresh list", icon=ft.Icons.LIST_ALT,
        on_click=_on_refresh_list,
    )

    # --- Initial population ---
    _refresh_chapter_list()

    # Restore last-opened chapter if it still exists
    last_name = db.get_setting(conn, SETTING_ACTIVE, "")
    if last_name and last_name in chapter_buttons:
        d = _resolve_dir()
        if d is not None:
            candidate = d / last_name
            if candidate.is_file():
                _load_file(candidate)

    # --- Layout ---
    path_bar = ft.Row(
        [path_field, browse_btn],
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    left_panel = ft.Container(
        width=300,
        content=ft.Column(
            [
                ft.Text("Chapters", size=11, color=ft.Colors.BLUE_GREY_400,
                        weight=ft.FontWeight.W_500),
                ft.Container(
                    bgcolor=ft.Colors.BLUE_GREY_800,
                    border_radius=6,
                    padding=ft.Padding(left=6, right=6, top=6, bottom=6),
                    expand=True,
                    content=chapter_list_column,
                ),
                refresh_list_btn,
            ],
            spacing=8,
            expand=True,
        ),
    )

    right_panel = ft.Container(
        expand=True,
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Text("Editor", size=11, color=ft.Colors.BLUE_GREY_400,
                                weight=ft.FontWeight.W_500),
                        dirty_text,
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                editor_field,
                ft.Row(
                    [save_btn, reload_btn, open_external_btn, status_text],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
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
                ft.Text("LaTeX", size=18, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.WHITE),
                ft.Text(
                    "Edit chapter .tex files that live outside this project. "
                    "Pick a folder, choose a chapter, edit, then Save. Reload "
                    "pulls in changes made by external editors.",
                    size=12, color=ft.Colors.BLUE_GREY_400,
                ),
                path_bar,
                ft.Divider(height=1, color=ft.Colors.BLUE_GREY_700),
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
