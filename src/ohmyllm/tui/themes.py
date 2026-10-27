"""OhMyLLM themes.

Every theme MUST define the solid CSS variables the stylesheet relies on:

- ``$ring``      frames, focus, scrollbar thumbs, primary buttons (bright)
- ``$ring-text`` text drawn ON ``$ring`` (dark on bright accents)
- ``$tint``      solid dark accent for section backgrounds / headers

There are NO alpha washes anywhere: ``$primary 60%`` over a dark surface
composites to a desaturated grey — that is exactly what the user saw as
"it's fucking grey". Solid colors only.
"""

from __future__ import annotations

from dataclasses import replace

from textual.theme import BUILTIN_THEMES, Theme


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def blend(top: str, bottom: str, alpha: float) -> str:
    """Solid mix of two hex colors (``top`` at ``alpha``) — no transparency
    in the result, so terminals never composite it against a third color."""
    a = _rgb(top)
    b = _rgb(bottom)
    return "#" + "".join(f"{round(a[i] * alpha + b[i] * (1 - alpha)):02x}" for i in range(3))


def _custom(
    name: str,
    ring: str,
    background: str,
    *,
    secondary: str | None = None,
    tint: str | None = None,
    ring_text: str | None = None,
    foreground: str = "#e8f6fa",
    success: str = "#3ddc84",
    warning: str = "#ffb300",
    error: str = "#ff4d5e",
) -> Theme:
    surface = blend("#ffffff", background, 0.045)
    return Theme(
        name=name,
        primary=ring,
        secondary=secondary or blend(ring, background, 0.8),
        success=success,
        warning=warning,
        error=error,
        accent=ring,
        foreground=foreground,
        background=background,
        surface=surface,
        # panel/boost intentionally unset: Textual derives a cyan-tinted
        # panel + subtle boost overlay from surface+primary — and setting
        # panel explicitly would force $boost to transparent.
        variables={
            "ring": ring,
            "ring-text": ring_text or blend(ring, "#000000", 0.12),
            "tint": tint or blend(ring, background, 0.22),
        },
    )


def _augment(theme: Theme) -> Theme:
    """Give a built-in theme the solid frame variables our stylesheet uses."""
    bg = theme.background or "#0e1116"
    ring = theme.primary
    return replace(
        theme,
        variables={
            **theme.variables,
            "ring": ring,
            "ring-text": blend(ring, "#000000" if theme.dark else "#ffffff", 0.12),
            "tint": blend(ring, bg, 0.22),
        },
    )


CUSTOM_THEMES: dict[str, Theme] = {
    t.name: t
    for t in [
        _custom("electric-cyan", "#00e5ff", "#0a0e12", secondary="#00b8d4"),
        _custom("toxic-green", "#00ff9c", "#080f0b", secondary="#00c777", foreground="#eafff3"),
        _custom("hot-magenta", "#ff3ddc", "#100a0f", secondary="#c61fa8", foreground="#fbeaff"),
        _custom("solar-amber", "#ffb000", "#100d06", secondary="#c78700", foreground="#fff4dc"),
        _custom("ice-blue", "#4da6ff", "#080c12", secondary="#2b7fd4", foreground="#e6f1ff"),
        _custom("crimson", "#ff4d5e", "#110809", secondary="#d42b3c", foreground="#ffe8ea"),
    ]
}

# curated built-ins, augmented with $ring/$tint/$ring-text
BUILTIN_PICKS: tuple[str, ...] = (
    "dracula",
    "nord",
    "gruvbox",
    "catppuccin-mocha",
    "tokyo-night",
    "rose-pine",
    "monokai",
    "solarized-dark",
    "textual-dark",
    "textual-light",
)

THEMES: dict[str, Theme] = {
    **CUSTOM_THEMES,
    **{name: _augment(BUILTIN_THEMES[name]) for name in BUILTIN_PICKS},
}

# display order for the settings picker
THEME_ORDER: tuple[str, ...] = (*CUSTOM_THEMES, *BUILTIN_PICKS)

DEFAULT_THEME = "electric-cyan"

_LEGACY = {"dark": DEFAULT_THEME, "light": "textual-light", "textual-dark": "textual-dark"}


def resolve_theme(name: str | None) -> str:
    """Map any persisted value (incl. pre-theme-system configs) to a registered theme."""
    if name and name in THEMES:
        return name
    return _LEGACY.get(name or "", DEFAULT_THEME)


__all__ = ["THEMES", "THEME_ORDER", "DEFAULT_THEME", "resolve_theme", "blend"]
