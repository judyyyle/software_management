from __future__ import annotations

import csv
import math
import struct
import zlib
from pathlib import Path
from typing import Any


CSV_FIELDS = [
    "gen",
    "best",
    "avg",
    "median",
    "worst",
    "feasible_count",
    "hard_feasible_count",
    "soft_penalty_count",
    "A_count",
    "B_count",
    "C_count",
    "individuals_with_B",
    "individuals_with_C",
    "individuals_all_A",
    "b_success",
    "b_infeasible",
    "b_repaired",
    "c_success",
    "c_infeasible",
    "c_repaired",
    "best_A_count",
    "best_B_count",
    "best_C_count",
    "truck_distance",
    "uav_distance",
    "energy",
    "penalty",
    "elapsed",
]


def write_evolution_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    extra_fields = sorted({key for row in rows for key in row if key not in CSV_FIELDS})
    fields = CSV_FIELDS + extra_fields
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_evolution_plots(rows: list[dict[str, Any]], log_dir: Path) -> None:
    if not rows:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    _draw_line_chart(
        rows,
        log_dir / "ga_fitness_curve.png",
        [("best", (27, 94, 32)), ("avg", (46, 125, 50)), ("median", (2, 119, 189))],
    )
    _draw_line_chart(
        rows,
        log_dir / "ga_mode_distribution.png",
        [("A_count", (80, 80, 80)), ("B_count", (198, 40, 40)), ("C_count", (25, 118, 210))],
    )
    _draw_line_chart(
        rows,
        log_dir / "ga_feasible_count.png",
        [("feasible_count", (46, 125, 50)), ("hard_feasible_count", (0, 100, 0))],
    )
    _draw_line_chart(
        rows,
        log_dir / "ga_b_diagnostics.png",
        [("b_success", (46, 125, 50)), ("b_infeasible", (198, 40, 40)), ("b_repaired", (251, 140, 0))],
    )
    _draw_line_chart(
        rows,
        log_dir / "ga_penalty_curve.png",
        [
            ("repair_penalty", (251, 140, 0)),
            ("station_queue_penalty", (142, 36, 170)),
            ("infeasible_penalty", (198, 40, 40)),
            ("unserved_penalty", (91, 55, 183)),
            ("penalty", (60, 60, 60)),
        ],
    )


def _draw_line_chart(
    rows: list[dict[str, Any]],
    path: Path,
    series: list[tuple[str, tuple[int, int, int]]],
) -> None:
    width, height = 960, 540
    margin_left, margin_right, margin_top, margin_bottom = 64, 32, 32, 54
    img = [[(255, 255, 255) for _ in range(width)] for _ in range(height)]
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    xs = [_finite_float(row.get("gen"), float(index)) for index, row in enumerate(rows)]
    values_by_name = {
        name: [_finite_float(row.get(name), math.nan) for row in rows]
        for name, _ in series
    }
    ys = [value for values in values_by_name.values() for value in values if math.isfinite(value)]
    if not xs or not ys:
        _write_png(path, img)
        return

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    if math.isclose(min_x, max_x):
        max_x = min_x + 1.0
    if math.isclose(min_y, max_y):
        max_y = min_y + 1.0

    for color in ((235, 235, 235), (245, 245, 245)):
        pass
    for tick in range(6):
        y = margin_top + int(plot_h * tick / 5)
        _line(img, margin_left, y, width - margin_right, y, (235, 235, 235))
    _line(img, margin_left, margin_top, margin_left, height - margin_bottom, (80, 80, 80))
    _line(img, margin_left, height - margin_bottom, width - margin_right, height - margin_bottom, (80, 80, 80))

    def map_x(value: float) -> int:
        return margin_left + int((value - min_x) / (max_x - min_x) * plot_w)

    def map_y(value: float) -> int:
        return height - margin_bottom - int((value - min_y) / (max_y - min_y) * plot_h)

    for name, color in series:
        points = [
            (map_x(xs[index]), map_y(value))
            for index, value in enumerate(values_by_name.get(name, []))
            if math.isfinite(value)
        ]
        for p0, p1 in zip(points, points[1:]):
            _line(img, p0[0], p0[1], p1[0], p1[1], color)
        for x, y in points:
            _rect(img, x - 2, y - 2, x + 2, y + 2, color)

    _write_png(path, img)


def _finite_float(value: Any, default: float) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _line(img: list[list[tuple[int, int, int]]], x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
    width = len(img[0])
    height = len(img)
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        if 0 <= x0 < width and 0 <= y0 < height:
            img[y0][x0] = color
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _rect(img: list[list[tuple[int, int, int]]], x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
    width = len(img[0])
    height = len(img)
    for y in range(max(0, y0), min(height, y1 + 1)):
        for x in range(max(0, x0), min(width, x1 + 1)):
            img[y][x] = color


def _write_png(path: Path, img: list[list[tuple[int, int, int]]]) -> None:
    height = len(img)
    width = len(img[0]) if height else 0
    raw = bytearray()
    for row in img:
        raw.append(0)
        for red, green, blue in row:
            raw.extend((red, green, blue))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)
