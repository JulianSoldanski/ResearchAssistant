import flet as ft
from db import database as db
from ui import sidebar, kanban


def main(page: ft.Page):
    page.title = "ResearchAssistant"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = ft.Colors.BLUE_GREY_900
    page.window.width = 1280
    page.window.height = 800
    page.window.min_width = 900
    page.window.min_height = 600
    page.padding = 0

    conn = db.get_connection()
    db.init_db(conn)

    active_view = ["kanban"]
    main_content = ft.Container(expand=True)

    def navigate_to(view_key: str, **_):
        active_view[0] = view_key
        if view_key == "kanban":
            main_content.content = kanban.build(conn, page)
        page.update()

    sidebar_widget = sidebar.build(
        page=page,
        conn=conn,
        on_nav=navigate_to,
        on_sync=lambda _: navigate_to(active_view[0]),
        active_view=active_view,
    )

    page.add(
        ft.Row(
            [sidebar_widget, main_content],
            expand=True,
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        )
    )

    navigate_to("kanban")


if __name__ == "__main__":
    ft.app(target=main)
