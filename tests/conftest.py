"""Shared test fixtures and lightweight fakes."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeColor:
    """Stand-in for rmscene's PenColor enum (has a ``.name``)."""

    name: str


@dataclass
class FakeRect:
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0


@dataclass
class FakeGlyphRange:
    """Duck-typed stand-in for rmscene.scene_items.GlyphRange."""

    text: str
    color: FakeColor = field(default_factory=lambda: FakeColor("YELLOW"))
    rectangles: list[FakeRect] = field(default_factory=list)
