from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def make_icon(size: int = 64, background: str = "#101722", accent: str = "#4fd1b5") -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    scale = size / 64
    draw.rounded_rectangle(
        (2 * scale, 2 * scale, 62 * scale, 62 * scale),
        radius=15 * scale,
        fill=background,
    )
    draw.line(
        (
            23 * scale,
            31 * scale,
            23 * scale,
            47 * scale,
            41 * scale,
            47 * scale,
            41 * scale,
            31 * scale,
        ),
        fill=accent,
        width=max(2, round(4 * scale)),
        joint="curve",
    )
    draw.polygon(
        (
            (31 * scale, 15 * scale),
            (43 * scale, 27 * scale),
            (35 * scale, 27 * scale),
            (35 * scale, 39 * scale),
            (27 * scale, 39 * scale),
            (27 * scale, 27 * scale),
            (19 * scale, 27 * scale),
        ),
        fill=accent,
    )
    return image


LINE_ICONS: dict[str, list[tuple[str, tuple[float, ...]]]] = {
    "search": [
        ("circle", (11, 11)),
        ("line", (16, 16, 21, 21)),
    ],
    "download": [
        ("line", (12, 4, 12, 15)),
        ("line", (7, 10, 12, 15, 17, 10)),
        ("line", (5, 20, 19, 20)),
    ],
    "folder": [
        ("polyline", (3, 7, 21, 7, 21, 20, 3, 20, 3, 7)),
        ("polyline", (4, 5, 11, 5, 11, 9)),
    ],
    "refresh": [
        ("arc", (5, 5, 19, 19, 45, 315)),
        ("line", (17, 4, 20, 9, 15, 9)),
    ],
    "external": [
        ("polyline", (13, 4, 20, 4, 20, 11)),
        ("line", (20, 4, 11, 13)),
        ("polyline", (18, 14, 18, 20, 4, 20, 4, 5, 10, 5)),
    ],
    "play": [("polygon", (8, 5, 19, 12, 8, 19))],
    "pause": [("rect", (7, 5, 10, 19)), ("rect", (14, 5, 17, 19))],
    "stop": [("rect", (6, 6, 18, 18))],
    "copy": [("rect", (8, 8, 20, 21)), ("rect", (4, 3, 16, 16))],
    "check": [("polyline", (4, 12, 9, 18, 20, 5))],
    "warning": [
        ("polygon", (12, 3, 22, 21, 2, 21)),
        ("line", (12, 9, 12, 15)),
        ("dot", (12, 18)),
    ],
    "terminal": [
        ("rect", (3, 4, 21, 20)),
        ("polyline", (7, 9, 11, 12, 7, 15)),
        ("line", (13, 16, 18, 16)),
    ],
    "trash": [
        ("line", (5, 7, 19, 7)),
        ("rect", (8, 4, 16, 6)),
        ("polyline", (7, 8, 8, 21, 16, 21, 17, 8)),
    ],
    "arrow": [("line", (5, 12, 19, 12)), ("polyline", (13, 6, 19, 12, 13, 18))],
    "plus": [("line", (12, 5, 12, 19)), ("line", (5, 12, 19, 12))],
    "info": [("circle", (12, 12)), ("line", (12, 11, 12, 17)), ("dot", (12, 7))],
}


def make_line_icon(name: str, size: int = 24, color: str = "#a6b5c3") -> Image.Image:
    """Draw a crisp line icon at high resolution, then downsample for anti-aliasing."""
    supersample = 4
    scale = size * supersample / 24
    image = Image.new(
        "RGBA", (size * supersample, size * supersample), (0, 0, 0, 0)
    )
    draw = ImageDraw.Draw(image)
    width = max(2, round(1.8 * scale))
    operations = LINE_ICONS.get(name, LINE_ICONS["info"])
    for kind, values in operations:
        if kind == "line":
            points = [
                (values[index] * scale, values[index + 1] * scale)
                for index in range(0, len(values), 2)
            ]
            draw.line(points, fill=color, width=width, joint="curve")
        elif kind in {"polyline", "polygon"}:
            points = [
                (values[index] * scale, values[index + 1] * scale)
                for index in range(0, len(values), 2)
            ]
            if kind == "polyline":
                draw.line(points, fill=color, width=width, joint="curve")
            else:
                draw.polygon(points, fill=color)
        elif kind == "circle":
            x, y = values[0] * scale, values[1] * scale
            radius = 6.2 * scale
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                outline=color,
                width=width,
            )
        elif kind == "dot":
            x, y = values[0] * scale, values[1] * scale
            radius = 1.1 * scale
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=color,
            )
        elif kind == "rect":
            coords = [values[index] * scale for index in range(4)]
            radius = 2 * scale
            draw.rounded_rectangle(
                coords, radius=radius, outline=color, width=width
            )
        elif kind == "arc":
            coords = [values[index] * scale for index in range(4)]
            draw.arc(
                coords,
                start=int(values[4]),
                end=int(values[5]),
                fill=color,
                width=width,
            )
    return image.resize((size, size), Image.Resampling.LANCZOS)


def ensure_ico(path: Path) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        make_icon(64).save(path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128)])
    return path
