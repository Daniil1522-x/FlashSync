"""
ui/tui/splash.py — Заставка при запуске FlashSync Pro.

Полноэкранный Screen, показывается ~1.8с поверх главного экрана и
автоматически закрывается (self.app.pop_screen()), открывая обычный UI.
Никак не блокирует и не задерживает реальную инициализацию приложения —
вся настройка (load_profiles, setup_logger и т.д.) уже выполнена в
FlashSyncApp.__init__ к моменту показа заставки.
"""
from __future__ import annotations
from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.screen import Screen
from textual.widgets import Static, Label

from domain.version import __version__
from ui.tui.mascot import FRAME_WALK1, FRAME_WALK2

SPLASH_DURATION = 1.8
WALK_FRAME_INTERVAL = 0.22


class SplashScreen(Screen):
    CSS = """
    SplashScreen {
        align: center middle;
        background: $background;
    }
    #splash-box {
        width: auto; height: auto;
        align: center middle;
        layout: vertical;
    }
    #splash-title {
        text-align: center; color: ansi_yellow;
        text-style: bold;
        margin-bottom: 1;
    }
    #splash-mascot {
        width: 10; height: 5;
        content-align: center middle;
        margin-bottom: 1;
    }
    #splash-sub {
        text-align: center; color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="splash-box"):
            with Vertical():
                yield Label("⚡ F L A S H S Y N C   P R O ⚡", id="splash-title")
                yield Static(FRAME_WALK1, id="splash-mascot")
                yield Label(f"v{__version__}  —  загрузка...", id="splash-sub")

    def on_mount(self) -> None:
        self._toggle = False
        self._walk_timer = self.set_interval(WALK_FRAME_INTERVAL, self._walk_tick)
        self.set_timer(SPLASH_DURATION, self._finish)

    def _walk_tick(self) -> None:
        self._toggle = not self._toggle
        try:
            self.query_one("#splash-mascot", Static).update(
                FRAME_WALK1 if self._toggle else FRAME_WALK2
            )
        except Exception:
            pass

    def _finish(self) -> None:
        try:
            self._walk_timer.stop()
        except Exception:
            pass
        self.app.pop_screen()
