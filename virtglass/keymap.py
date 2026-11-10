"""Keyboard helpers.

Looking Glass input uses Linux evdev KEY_* codes. On Linux (both Wayland and
X11) Qt's nativeScanCode() is the XKB keycode, which is the evdev code + 8.
"""

from PySide6.QtCore import Qt

KEY_ESC = 1
KEY_1 = 2
KEY_0 = 11
KEY_MINUS = 12
KEY_EQUAL = 13
KEY_BACKSPACE = 14
KEY_TAB = 15
KEY_Q = 16
KEY_W = 17
KEY_E = 18
KEY_R = 19
KEY_T = 20
KEY_Y = 21
KEY_U = 22
KEY_I = 23
KEY_O = 24
KEY_P = 25
KEY_LEFTBRACE = 26
KEY_RIGHTBRACE = 27
KEY_ENTER = 28
KEY_LEFTCTRL = 29
KEY_A = 30
KEY_S = 31
KEY_D = 32
KEY_F = 33
KEY_G = 34
KEY_H = 35
KEY_J = 36
KEY_K = 37
KEY_L = 38
KEY_SEMICOLON = 39
KEY_APOSTROPHE = 40
KEY_GRAVE = 41
KEY_LEFTSHIFT = 42
KEY_BACKSLASH = 43
KEY_Z = 44
KEY_X = 45
KEY_C = 46
KEY_V = 47
KEY_B = 48
KEY_N = 49
KEY_M = 50
KEY_COMMA = 51
KEY_DOT = 52
KEY_SLASH = 53
KEY_RIGHTSHIFT = 54
KEY_LEFTALT = 56
KEY_SPACE = 57
KEY_CAPSLOCK = 58
KEY_F1 = 59
KEY_F4 = 62
KEY_F10 = 68
KEY_NUMLOCK = 69
KEY_SCROLLLOCK = 70
KEY_F11 = 87
KEY_F12 = 88
KEY_RIGHTCTRL = 97
KEY_SYSRQ = 99
KEY_RIGHTALT = 100
KEY_DELETE = 111
KEY_PAUSE = 119
KEY_LEFTMETA = 125
KEY_RIGHTMETA = 126
KEY_COMPOSE = 127

MODIFIERS = {KEY_LEFTCTRL, KEY_RIGHTCTRL, KEY_LEFTSHIFT, KEY_RIGHTSHIFT,
             KEY_LEFTALT, KEY_RIGHTALT, KEY_LEFTMETA, KEY_RIGHTMETA}


def linux_code(event):
    """Linux KEY_* code for a QKeyEvent, or None."""
    sc = event.nativeScanCode()
    if sc > 8:
        return sc - 8
    return None


# Named key combinations offered in the "Send keys" menu.
COMBOS = [
    ("Ctrl+Alt+Del", [KEY_LEFTCTRL, KEY_LEFTALT, KEY_DELETE]),
    ("Ctrl+Shift+Esc (Task Manager)", [KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_ESC]),
    ("Windows key", [KEY_LEFTMETA]),
    ("Alt+Tab", [KEY_LEFTALT, KEY_TAB]),
    ("Alt+F4", [KEY_LEFTALT, KEY_F4]),
    ("Win+D (show desktop)", [KEY_LEFTMETA, KEY_D]),
    ("Win+E (Explorer)", [KEY_LEFTMETA, KEY_E]),
    ("Win+R (Run)", [KEY_LEFTMETA, KEY_R]),
    ("Win+L (lock)", [KEY_LEFTMETA, KEY_L]),
    ("Print Screen", [KEY_SYSRQ]),
    ("Esc", [KEY_ESC]),
    ("F11 (to the guest)", [KEY_F11]),
]


# US-layout characters -> (key, shift) for "type text into guest".
_UNSHIFTED = {
    "`": KEY_GRAVE, "-": KEY_MINUS, "=": KEY_EQUAL, "[": KEY_LEFTBRACE,
    "]": KEY_RIGHTBRACE, "\\": KEY_BACKSLASH, ";": KEY_SEMICOLON,
    "'": KEY_APOSTROPHE, ",": KEY_COMMA, ".": KEY_DOT, "/": KEY_SLASH,
    " ": KEY_SPACE, "\n": KEY_ENTER, "\t": KEY_TAB,
}
_SHIFTED = {
    "~": KEY_GRAVE, "_": KEY_MINUS, "+": KEY_EQUAL, "{": KEY_LEFTBRACE,
    "}": KEY_RIGHTBRACE, "|": KEY_BACKSLASH, ":": KEY_SEMICOLON,
    '"': KEY_APOSTROPHE, "<": KEY_COMMA, ">": KEY_DOT, "?": KEY_SLASH,
}
_DIGIT_SHIFTED = ")!@#$%^&*("
_LETTERS = {
    "q": KEY_Q, "w": KEY_W, "e": KEY_E, "r": KEY_R, "t": KEY_T, "y": KEY_Y,
    "u": KEY_U, "i": KEY_I, "o": KEY_O, "p": KEY_P, "a": KEY_A, "s": KEY_S,
    "d": KEY_D, "f": KEY_F, "g": KEY_G, "h": KEY_H, "j": KEY_J, "k": KEY_K,
    "l": KEY_L, "z": KEY_Z, "x": KEY_X, "c": KEY_C, "v": KEY_V, "b": KEY_B,
    "n": KEY_N, "m": KEY_M,
}


def char_to_key(ch):
    """Return (linux_code, needs_shift) for a US-layout character, or None."""
    if ch in _UNSHIFTED:
        return _UNSHIFTED[ch], False
    if ch in _SHIFTED:
        return _SHIFTED[ch], True
    if ch.isdigit() and ch.isascii():
        d = int(ch)
        return (KEY_0 if d == 0 else KEY_1 + d - 1), False
    if ch in _DIGIT_SHIFTED:
        d = _DIGIT_SHIFTED.index(ch)
        return (KEY_0 if d == 0 else KEY_1 + d - 1), True
    low = ch.lower()
    if low in _LETTERS and ch.isascii():
        return _LETTERS[low], ch.isupper()
    return None


def qt_button(button):
    """Map a Qt mouse button to the Looking Glass button number."""
    return {
        Qt.LeftButton: 1,
        Qt.MiddleButton: 2,
        Qt.RightButton: 3,
        Qt.BackButton: 6,
        Qt.ForwardButton: 7,
    }.get(button)
