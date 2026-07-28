"""
ui/tui/mascot.py — Half-block пиксель-арт маскот FlashSync (флешка на ножках).

Техника half-block: символ ▀ (U+2580) несёт ДВА независимых 24-bit цвета:
  - foreground → верхняя половина символа
  - background → нижняя половина символа
Итог: 2 пикселя на 1 символ терминала, полный RGB, работает везде где
поддерживается 256/true-color (Windows Terminal, GNOME Term, iTerm2, Kitty...).

Размер спрайта: 10 px × 10 px = 10 × 5 символов терминала.
Публичный API — только set_active(bool). Всё остальное внутреннее.
"""
from __future__ import annotations
import copy, random
from textual.widgets import Static
from textual.reactive import reactive

# ── ФОНОВЫЙ ЦВЕТ (должен совпадать с темой приложения) ──────────────────────
_BG = (8, 8, 18)

# ── ПАЛИТРА ──────────────────────────────────────────────────────────────────
T  = None              # прозрачный
BO = (6,  26, 105)     # обводка корпуса
BL = (28, 92, 245)     # светлая часть корпуса
BM = (18, 60, 185)     # средний тон корпуса
S  = (165,165,165)     # серебристый USB-нож
SH = (210,210,210)     # серебристый блик
SS = (108,108,108)     # серебристая тень
EW = (248,248,255)     # белки глаз
_EP_IDLE   = (255, 65, 65)   # зрачок: красное свечение (idle)
_EP_ACTIVE = ( 80,200,255)   # зрачок: синее свечение   (active)
_EP_BLINK  = (248,248,255)   # зрачок: закрыт = цвет белков
GL = ( 45,245,110)     # зелёный LED
Y  = (252,195, 18)     # золотая полоска
LG = ( 65, 65,108)     # ноги
FT = ( 28, 28, 52)     # ботинки

# ── СПРАЙТ ТЕЛА  (10 px × 8 px) ─────────────────────────────────────────────
# Хранится с заглушкой EP вместо реального цвета зрачка —
# цвет подставляется при сборке кадра, чтобы анимировать глаза отдельно.
# Заглушка для зрачков: уникальный цвет (значение за пределами 0-255 не
# встречается в настоящей палитре), deepcopy копирует кортеж по значению,
# поэтому сравнение == работает без проблем с id().
_EP = (-1, -1, -1)

_BODY = [
    # 0    1    2    3    4    5    6    7    8    9
    [ T,   T,  SS,  S,   S,   S,   S,  SS,   T,   T ],  # 0  USB-нож (верх)
    [ T,   T,  SS,  SH,  SH,  SH,  SH,  SS,  T,   T ],  # 1  USB-нож (низ)
    [ T,  BO,  BO,  BO,  BO,  BO,  BO,  BO,  BO,  T ],  # 2  верх корпуса
    [ T,  BO,  BL,  EW, _EP, _EP,  EW,  BL,  BO,  T ],  # 3  глаза
    [ T,  BO,  BL,  EW, _EP, _EP,  EW,  BL,  BO,  T ],  # 4  глаза
    [ T,  BO,  BL,  BL,  GL,  GL,  BL,  BL,  BO,  T ],  # 5  LED-индикаторы
    [ T,   Y,   Y,   Y,   Y,   Y,   Y,   Y,   Y,  T ],  # 6  золотая полоска
    [ T,  BO,  BO,  BO,  BO,  BO,  BO,  BO,  BO,  T ],  # 7  низ корпуса
]

# ── ВАРИАНТЫ НОГ  (10 px × 2 px) ────────────────────────────────────────────
_L_STAND = [
    [ T,   T,   T,  LG,   T,   T,  LG,   T,   T,   T ],  # 8
    [ T,   T,  FT,  FT,   T,   T,  FT,  FT,   T,   T ],  # 9
]
_L_WALK1 = [
    [ T,   T,  LG,   T,   T,   T,   T,  LG,   T,   T ],  # ноги врозь
    [ T,  FT,  FT,   T,   T,   T,   T,  FT,  FT,   T ],
]
_L_WALK2 = [
    [ T,   T,   T,   T,  LG,  LG,   T,   T,   T,   T ],  # ноги вместе
    [ T,   T,   T,  FT,  FT,  FT,  FT,   T,   T,   T ],
]

# ── HALF-BLOCK РЕНДЕРЕР ──────────────────────────────────────────────────────
def _hex(rgb) -> str:
    if rgb is None or (isinstance(rgb, tuple) and any(v < 0 for v in rgb)):
        r, g, b = _BG
    else:
        r, g, b = rgb
    return f"#{r:02x}{g:02x}{b:02x}"

def _render(rows: list) -> str:
    """Конвертирует 2D-массив RGB-пикселей в Rich-разметку с символами ▀."""
    H = len(rows)
    W = max(len(r) for r in rows)
    lines = []
    for y in range(0, H, 2):
        parts = []
        for x in range(W):
            top = rows[y][x]    if x < len(rows[y])              else None
            bot = rows[y+1][x]  if (y+1 < H and x < len(rows[y+1])) else None
            tf, bf = _hex(top), _hex(bot)
            parts.append(f"[on {bf}] [/]" if tf == bf else f"[{tf} on {bf}]▀[/]")
        lines.append("".join(parts))
    return "\n".join(lines)

def _frame(legs: list, eye_col) -> str:
    """Собирает кадр: тело + ноги, подставляет реальный цвет зрачков."""
    body = copy.deepcopy(_BODY)
    for row in body:
        for i, px in enumerate(row):
            if px == _EP:
                row[i] = eye_col
    return _render(body + copy.deepcopy(legs))

# ── ПРЕДВЫЧИСЛЕННЫЕ КАДРЫ  (вычисляются 1 раз при загрузке модуля) ──────────
FRAME_IDLE   = _frame(_L_STAND, _EP_IDLE)
FRAME_BLINK  = _frame(_L_STAND, _EP_BLINK)
FRAME_WALK1  = _frame(_L_WALK1, _EP_ACTIVE)
FRAME_WALK2  = _frame(_L_WALK2, _EP_ACTIVE)

# ── ВИДЖЕТ ───────────────────────────────────────────────────────────────────
class MascotWidget(Static):
    """
    Маскот-флешка в статус-баре. Единственный публичный метод — set_active(bool).

    False (idle):  стоит, раз в ~2.6с моргает.
    True  (walk):  шагает, пока идёт скан или синхронизация.

    Чисто декоративный виджет — все методы обёрнуты в try/except,
    сбой здесь не может повлиять на логику приложения.
    """

    DEFAULT_CSS = """
    MascotWidget {
        width: 10;
        height: 5;
        content-align: center middle;
    }
    """

    active: reactive[bool] = reactive(False)

    def __init__(self, **kwargs):
        super().__init__(FRAME_IDLE, **kwargs)
        self._walk_toggle = False
        self._idle_timer  = None
        self._walk_timer  = None

    def on_mount(self) -> None:
        self._idle_timer = self.set_interval(2.6, self._idle_tick)

    def _idle_tick(self) -> None:
        if self.active:
            return
        try:
            if random.random() < 0.45:
                self.update(FRAME_BLINK)
                self.set_timer(0.13, lambda: self.update(FRAME_IDLE))
        except Exception:
            pass

    def set_active(self, value: bool) -> None:
        if value == self.active:
            return
        self.active = value
        try:
            if value:
                self._walk_toggle = False
                self._walk_timer = self.set_interval(0.27, self._walk_tick)
            else:
                if self._walk_timer is not None:
                    self._walk_timer.stop()
                    self._walk_timer = None
                self.update(FRAME_IDLE)
        except Exception:
            pass

    def _walk_tick(self) -> None:
        try:
            self._walk_toggle = not self._walk_toggle
            self.update(FRAME_WALK1 if self._walk_toggle else FRAME_WALK2)
        except Exception:
            pass
