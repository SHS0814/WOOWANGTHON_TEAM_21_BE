"""합성 샘플 약통 이미지와 라벨을 만든다. 실제 사진이 아니다 (동작 확인용).

외부 라이브러리 없이 PNG를 직접 쓴다. 칸은 회색 테두리, 약은 색 원으로 그린다.

    uv run python -m harness.make_samples
"""

import json
import random
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).parent / "samples"

SAMPLES = [
    # (id, rows, cols, row_labels, seed)
    ("synthetic-2x7", 2, 7, ["아침", "저녁"], 1),
    ("synthetic-1x7", 1, 7, [""], 2),
    ("synthetic-4x7", 4, 7, ["아침", "점심", "저녁", "취침"], 3),
]
WEEK = ["월", "화", "수", "목", "금", "토", "일"]
CELL = 60
PAD = 8


def _png(width: int, height: int, pixels: list[bytearray]) -> bytes:
    raw = b"".join(b"\x00" + bytes(row) for row in pixels)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def draw(rows: int, cols: int, truth: dict[str, str]) -> bytes:
    w, h = cols * CELL + PAD * 2, rows * CELL + PAD * 2
    px = [bytearray([245, 245, 240] * w) for _ in range(h)]

    def put(x: int, y: int, rgb: tuple[int, int, int]) -> None:
        px[y][x * 3 : x * 3 + 3] = bytes(rgb)

    for r in range(rows):
        for c in range(cols):
            x0, y0 = PAD + c * CELL, PAD + r * CELL
            for i in range(CELL):
                for edge in (0, CELL - 1):
                    put(x0 + i, y0 + edge, (120, 120, 120))
                    put(x0 + edge, y0 + i, (120, 120, 120))
            if truth[f"r{r}c{c}"] == "full":
                cx, cy, rad = x0 + CELL // 2, y0 + CELL // 2, CELL // 4
                for y in range(cy - rad, cy + rad):
                    for x in range(cx - rad, cx + rad):
                        if (x - cx) ** 2 + (y - cy) ** 2 <= rad * rad:
                            put(x, y, (220, 90, 60))
    return _png(w, h, px)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    photos = []
    for sid, rows, cols, row_labels, seed in SAMPLES:
        rng = random.Random(seed)
        truth = {
            f"r{r}c{c}": ("empty" if rng.random() < 0.4 else "full")
            for r in range(rows)
            for c in range(cols)
        }
        (OUT / f"{sid}.png").write_bytes(draw(rows, cols, truth))
        photos.append(
            {
                "id": sid,
                "file": f"{sid}.png",
                "pillbox": {
                    "name": f"합성 {rows}x{cols}",
                    "layout": {
                        "rows": rows,
                        "cols": cols,
                        "row_labels": row_labels,
                        "col_labels": WEEK[:cols],
                    },
                },
                "cells": truth,
                "target_cell_id": "r0c2",
                "conditions": {"synthetic": True},
                "notes": "합성 이미지. 실제 사진 아님",
            }
        )
    labels = {"version": 1, "photos": photos}
    (OUT / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(photos)} samples to {OUT}")


if __name__ == "__main__":
    main()
