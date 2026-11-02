"""Texts in several languages. `language = "es" | "en"` in config.toml.

Each locale (kingdom/locales/<code>.py) defines TEXT: key -> template or
list of variants (one is picked at random), and WORDS: structured vocabulary
(titles, epithets, building names...). The chronicle is written in the language
that is active when each event happens.
"""
import importlib
import random

_LOCALES: dict[str, object] = {}
LANG = "es"


def _locale(code: str):
    if code not in _LOCALES:
        _LOCALES[code] = importlib.import_module(f"kingdom.locales.{code}")
    return _LOCALES[code]


def detect() -> str:
    """System language: Spanish if the locale is es_*, otherwise English."""
    import os
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(var, "")
        if value:
            return "es" if value.lower().startswith("es") else "en"
    return "en"


def set_lang(code: str):
    global LANG
    if code == "auto":
        code = detect()
    try:
        _locale(code)
        LANG = code
    except ModuleNotFoundError:
        LANG = "es"


def tr(key: str, rng: random.Random | None = None, **kw) -> str:
    """Translated, formatted template; if there are variants, picks one."""
    text = _locale(LANG).TEXT.get(key)
    if text is None:
        text = _locale("es").TEXT[key]
    if isinstance(text, (list, tuple)):
        text = (rng or random).choice(text)
    return text.format(**kw)


def words(key: str):
    """Structured vocabulary of the active language (lists, dicts)."""
    w = _locale(LANG).WORDS
    return w[key] if key in w else _locale("es").WORDS[key]


def polish(text: str) -> str:
    """Grammar touch-ups of the language (Spanish contractions)."""
    if LANG == "es":
        text = text.replace(" a el ", " al ").replace(" de el ", " del ")
    return text


def ui(key: str, **kw):
    """Interface texts (HUD, info cards, powers)."""
    table = _locale(LANG).UI
    text = table[key] if key in table else _locale("es").UI[key]
    return text.format(**kw) if kw and isinstance(text, str) else text


def raw(key: str):
    """Raw TEXT catalog entry (for structures such as story.count)."""
    text = _locale(LANG).TEXT.get(key)
    return text if text is not None else _locale("es").TEXT[key]
