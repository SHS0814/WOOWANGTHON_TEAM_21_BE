"""판독기 정확도 비교. 같은 라벨 데이터로 여러 판독기를 돌려 칸 단위 정확도, 애매 비율, 지연을 표로 낸다.

    uv run python -m harness.run --labels harness/samples/labels.json --readers mock
    uv run python -m harness.run --labels harness/data/labels.json \\
        --readers mock,claude:claude-haiku-5-5,claude:claude-sonnet-5-5,ollama:qwen2.5vl:7b \\
        --layout --thresholds 0.7,0.8,0.9 --out-dir harness/results

키나 서버가 없는 판독기는 건너뛰고 표 아래에 이유를 적는다.
"""

import argparse
import csv
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.reader import Reader, ReaderError, ReaderUnavailable, build_reader
from harness.labels import LabelSet, load_labels


@dataclass
class CellRow:
    reader: str
    photo_id: str
    cell_id: str
    truth: str
    state: str
    confidence: float
    is_target: bool


@dataclass
class ReaderResult:
    spec: str
    provider: str = ""
    model: str = ""
    is_mock: bool = False
    rows: list[CellRow] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    layout_checked: int = 0
    layout_matched: int = 0
    photos: int = 0


def evaluate(spec: str, reader: Reader, labels: LabelSet, base: Path, check_layout: bool):
    res = ReaderResult(spec=spec, provider=reader.provider, model=reader.model)
    for photo in labels.photos:
        res.photos += 1
        image = (base / photo.file).read_bytes()
        layout = photo.pillbox.layout
        if check_layout:
            try:
                prop = reader.detect_layout(image)
                res.is_mock |= prop.is_mock
                res.layout_checked += 1
                res.layout_matched += (prop.layout.rows, prop.layout.cols) == (
                    layout.rows,
                    layout.cols,
                )
            except ReaderError as e:
                res.errors.append(f"{photo.id} layout: {e}")
        started = time.perf_counter()
        try:
            readings = reader.read_cells(image, layout)
        except ReaderError as e:
            res.errors.append(f"{photo.id}: {e}")
            continue
        res.latencies_ms.append((time.perf_counter() - started) * 1000)
        res.is_mock |= readings.is_mock
        got = readings.by_id()
        for cid, truth in photo.cells.items():
            r = got[cid]
            res.rows.append(
                CellRow(
                    spec, photo.id, cid, truth, r.state, r.confidence, cid == photo.target_cell_id
                )
            )
    return res


def _pct(n: int, d: int) -> str:
    return "-" if d == 0 else f"{100 * n / d:.1f}%"


def _apply_threshold(rows: list[CellRow], th: float | None) -> list[str]:
    return [
        "unclear" if th is not None and r.state != "unclear" and r.confidence < th else r.state
        for r in rows
    ]


def summarize(res: ReaderResult, th: float | None = None) -> dict:
    states = _apply_threshold(res.rows, th)
    pairs = list(zip(res.rows, states))
    total = len(pairs)
    correct = sum(s == r.truth for r, s in pairs)
    unclear = sum(s == "unclear" for _, s in pairs)
    truth_full = [s for r, s in pairs if r.truth == "full"]
    truth_empty = [s for r, s in pairs if r.truth == "empty"]
    target = [(r, s) for r, s in pairs if r.is_target]
    by_photo: dict[str, bool] = {}
    for r, s in pairs:
        by_photo[r.photo_id] = by_photo.get(r.photo_id, True) and s == r.truth
    lat = sorted(res.latencies_ms)
    return {
        "reader": res.spec + (" (모의)" if res.is_mock else ""),
        "threshold": "-" if th is None else f"{th:.2f}",
        "photos": f"{len(by_photo)}/{res.photos}",
        "cells": total,
        "cell_acc": _pct(correct, total),
        "decided_acc": _pct(correct, total - unclear),
        "unclear_rate": _pct(unclear, total),
        "false_empty": _pct(sum(s == "empty" for s in truth_full), len(truth_full)),
        "false_full": _pct(sum(s == "full" for s in truth_empty), len(truth_empty)),
        "target_acc": _pct(sum(s == r.truth for r, s in target), len(target)),
        "photo_all_ok": _pct(sum(by_photo.values()), len(by_photo)),
        "layout_ok": _pct(res.layout_matched, res.layout_checked),
        "p50_ms": "-" if not lat else f"{statistics.median(lat):.0f}",
        "p95_ms": "-" if not lat else f"{lat[min(len(lat) - 1, int(0.95 * len(lat)))]:.0f}",
        "errors": len(res.errors),
    }


COLUMNS = [
    ("reader", "판독기"),
    ("threshold", "신뢰도 기준"),
    ("photos", "사진"),
    ("cells", "칸"),
    ("cell_acc", "칸 정확도"),
    ("decided_acc", "판단한 칸 정확도"),
    ("unclear_rate", "애매 비율"),
    ("false_empty", "약 있는데 빈 칸으로 봄"),
    ("false_full", "빈 칸인데 약 있다고 봄"),
    ("target_acc", "대상 칸 정확도"),
    ("photo_all_ok", "사진 전체 정답"),
    ("layout_ok", "구조 인식"),
    ("p50_ms", "지연 p50(ms)"),
    ("p95_ms", "지연 p95(ms)"),
    ("errors", "오류"),
]


def to_markdown(rows: list[dict]) -> str:
    head = "| " + " | ".join(t for _, t in COLUMNS) + " |"
    sep = "|" + "|".join("---" for _ in COLUMNS) + "|"
    body = ["| " + " | ".join(str(r[k]) for k, _ in COLUMNS) + " |" for r in rows]
    return "\n".join([head, sep, *body])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument(
        "--readers", default="mock", help="쉼표로 구분: mock,claude[:모델],ollama[:모델]"
    )
    ap.add_argument("--layout", action="store_true", help="detect_layout 도 검사")
    ap.add_argument("--thresholds", default="", help="신뢰도 기준 후보 (예: 0.7,0.9)")
    ap.add_argument("--out-dir", type=Path, help="결과 표(md)와 칸별 상세(csv)를 저장할 곳")
    args = ap.parse_args(argv)

    labels, base = load_labels(args.labels)
    settings = get_settings()
    thresholds = [float(t) for t in args.thresholds.split(",") if t.strip()]

    results: list[ReaderResult] = []
    skipped: list[tuple[str, str]] = []
    for spec in [s.strip() for s in args.readers.split(",") if s.strip()]:
        try:
            reader = build_reader(spec, settings)
        except ReaderUnavailable as e:
            skipped.append((spec, str(e)))
            continue
        print(f"running {spec} on {len(labels.photos)} photos...", file=sys.stderr)
        results.append(evaluate(spec, reader, labels, base, args.layout))

    table = [summarize(r) for r in results]
    for th in thresholds:
        table += [summarize(r, th) for r in results]

    lines = [
        f"# 판독 정확도 ({datetime.now().isoformat(timespec='seconds')})",
        "",
        f"- 라벨: `{args.labels}` (사진 {len(labels.photos)}장)",
        (
            "- 칸 정확도: 애매(unclear)도 틀린 것으로 셈. "
            "'약 있는데 빈 칸으로 봄'은 복용으로 잘못 기록될 수 있는 위험 오류다 (원칙 5)"
        ),
        "- (모의) 표시 판독기는 실제 AI가 아니다",
        "",
        to_markdown(table) if table else "(실행한 판독기 없음)",
    ]
    if skipped:
        lines += ["", "건너뛴 판독기:"] + [f"- `{s}`: {why}" for s, why in skipped]
    errors = [f"- `{r.spec}` {e}" for r in results for e in r.errors]
    if errors:
        lines += ["", "오류:", *errors]
    report = "\n".join(lines) + "\n"
    print(report)

    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (args.out_dir / f"{stamp}.md").write_text(report, encoding="utf-8")
        with (args.out_dir / f"{stamp}-cells.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(
                ["reader", "photo_id", "cell_id", "truth", "state", "confidence", "is_target"]
            )
            for r in results:
                for c in r.rows:
                    w.writerow(
                        [
                            c.reader,
                            c.photo_id,
                            c.cell_id,
                            c.truth,
                            c.state,
                            c.confidence,
                            c.is_target,
                        ]
                    )
        meta = [
            {"spec": r.spec, "provider": r.provider, "model": r.model, "is_mock": r.is_mock}
            for r in results
        ]
        (args.out_dir / f"{stamp}-readers.json").write_text(
            json.dumps({"readers": meta, "skipped": skipped}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
