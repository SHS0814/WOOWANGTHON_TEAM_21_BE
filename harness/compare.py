"""이미 돌린 harness.run 결과 여러 개를 비교한다. API를 부르지 않고 저장된 csv만 읽는다.

추론 강도처럼 환경 변수로 바꾸는 조건은 harness.run 한 번에 하나만 돌릴 수 있어서, 조건마다
--out-dir 을 나눠 돌린 뒤 이 스크립트로 합친다.

    uv run python -m harness.compare --labels harness/data/real/labels.json \\
        --run luna-default=harness/results/t-ai3/luna-default \\
        --run sol-default=harness/results/t-ai3/sol-default \\
        --thresholds 0.75,0.8,0.85,0.9,0.95 --out harness/results/t-ai3/compare.md

출력에는 사진 번호(real-NNN)와 집계만 넣는다. 라벨 메모·칸별 정답·판독 원문은 넣지 않는다.
"""

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.reader.base import CellReading, CellReadings
from app.services.classify import classify
from harness.labels import LabelSet, load_labels
from harness.run import CellRow, ReaderResult, summarize

LID_OPEN = re.compile(r"(\S)\s*뚜껑 열림")


@dataclass
class Run:
    name: str
    result: ReaderResult
    missing: list[str]  # 판독에 실패해 칸 행이 없는 사진

    @property
    def rows(self) -> list[CellRow]:
        return self.result.rows


def _only(directory: Path, suffix: str) -> Path:
    found = sorted(directory.glob(f"*{suffix}"))
    if len(found) != 1:
        raise SystemExit(f"{directory} 에 {suffix} 파일이 {len(found)}개다. 조건마다 1개여야 한다")
    return found[0]


def load_run(name: str, directory: Path, labels: LabelSet) -> Run:
    res = ReaderResult(spec=name, photos=len(labels.photos))
    with _only(directory, "-cells.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            res.rows.append(
                CellRow(
                    name,
                    r["photo_id"],
                    r["cell_id"],
                    r["truth"],
                    r["state"],
                    float(r["confidence"]),
                    r["is_target"] == "True",
                )
            )
    with _only(directory, "-calls.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["cost_usd"]:
                res.cost_usd += float(r["cost_usd"])
            else:
                res.cost_unknown += 1
            if r["kind"] == "cells":
                res.latencies_ms.append(float(r["latency_ms"]))
    seen = {r.photo_id for r in res.rows}
    missing = [p.id for p in labels.photos if p.id not in seen]
    res.errors = [f"{m}: 판독 실패" for m in missing]
    return Run(name, res, missing)


def _state(r: CellRow, th: float | None) -> str:
    if th is not None and r.state != "unclear" and r.confidence < th:
        return "unclear"
    return r.state


def counts(rows: list[CellRow], th: float | None = None) -> dict:
    """not_visible 은 정확도에서 빼고 따로 센다. 표에 비율과 분모를 함께 쓰려고 개수로 돌려준다."""
    visible = [r for r in rows if r.truth != "not_visible"]
    full = [r for r in visible if r.truth == "full"]
    empty = [r for r in visible if r.truth == "empty"]
    hidden = [r for r in rows if r.truth == "not_visible"]
    return {
        "visible": len(visible),
        "correct": sum(_state(r, th) == r.truth for r in visible),
        "unclear": sum(_state(r, th) == "unclear" for r in visible),
        "full": len(full),
        "false_empty": sum(_state(r, th) == "empty" for r in full),
        "empty": len(empty),
        "false_full": sum(_state(r, th) == "full" for r in empty),
        # 정말 빈 칸을 빈 칸으로, 기준을 넘겨 판단한 수. 나머지 빈 칸은 복약 때 재촬영으로 간다
        "empty_passed": sum(_state(r, th) == "empty" for r in empty),
        "hidden": len(hidden),
        "hidden_unclear": sum(_state(r, th) == "unclear" for r in hidden),
        "hidden_empty": sum(_state(r, th) == "empty" for r in hidden),
        "hidden_full": sum(_state(r, th) == "full" for r in hidden),
    }


def wilson_upper(k: int, n: int, z: float = 1.96) -> float | None:
    """오류 비율 95% 신뢰구간 상한 (윌슨). 같은 사진의 칸끼리는 독립이 아니라서 실제로는 더 넓다."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    return min(
        1.0, (p + z * z / (2 * n) + z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    )


def lowest_safe_threshold(rows: list[CellRow], thresholds: list[float]) -> float | None:
    """위험 오류(약 있는데 빈 칸으로 봄)가 0인 가장 낮은 후보 기준. 없으면 None."""
    for th in sorted(thresholds):
        if counts(rows, th)["false_empty"] == 0:
            return th
    return None


def lid_open_cells(labels: LabelSet) -> set[tuple[str, str]]:
    """라벨 메모의 '<요일> 뚜껑 열림'에서 뚜껑 열린 칸을 찾는다 (1행 약통 기준)."""
    cells = set()
    for p in labels.photos:
        cols = p.pillbox.layout.col_labels
        for label in LID_OPEN.findall(p.notes):
            if label in cols:
                cells.add((p.id, f"r0c{cols.index(label)}"))
    return cells


def _tagged(tags: dict, key: str) -> set[tuple[str, str]]:
    return {(photo, cell) for photo, cells in tags.get(key, {}).items() for cell in cells}


def subsets(
    labels: LabelSet, draft: LabelSet | None, tags: dict | None = None
) -> dict[str, set[tuple[str, str]]]:
    def photos(pred) -> set[tuple[str, str]]:
        return {(p.id, c) for p in labels.photos if pred(p) for c in p.cells}

    out = {
        "띠 사진 (전체 칸)": photos(lambda p: p.conditions.get("crop") == "strip"),
        "전체 사진 (전체 칸)": photos(lambda p: p.conditions.get("crop") == "full"),
        "세로·거꾸로 사진 (전체 칸)": photos(
            lambda p: p.conditions.get("orientation") in ("vertical", "upside_down")
        ),
        "안 보이는 칸이 있는 사진 (전체 칸)": photos(lambda p: "not_visible" in p.cells.values()),
        "뚜껑 열린 칸 (메모 기준)": lid_open_cells(labels),
        "초안 작성자가 확신 못 한 칸": {(p.id, c) for p in labels.photos for c in p.check_cells},
    }
    fixed: set[tuple[str, str]] = set()
    if draft is not None:
        before = {(p.id, c): s for p in draft.photos for c, s in p.cells.items()}
        fixed = {
            (p.id, c)
            for p in labels.photos
            for c, s in p.cells.items()
            if before.get((p.id, c), s) != s
        }
        out["사용자가 초안에서 고친 칸"] = fixed
    if tags is not None:
        del out["뚜껑 열린 칸 (메모 기준)"]
        checked = out["초안 작성자가 확신 못 한 칸"]
        out["(a) 뚜껑 열린 칸 (사진 표시)"] = _tagged(tags, "lid_open")
        out["(b) 일부만 보이는 칸 (사진 표시 + 확신 못 한 칸 + 고친 칸)"] = (
            _tagged(tags, "partial") | checked | fixed
        )
        cut = _tagged(tags, "partial")
        lid = _tagged(tags, "lid_open")
        out["(b-1) 사진 가장자리에서 잘린 칸 (사진 표시만)"] = cut
        truth = {(p.id, c): v for p in labels.photos for c, v in p.cells.items()}
        out["(b-1) 중 정답이 빈 칸"] = {k for k in cut if truth[k] == "empty"}
        out["(b-1) 중 정답이 약 있음"] = {k for k in cut if truth[k] == "full"}
        special = lid | cut | checked | fixed
        out["비교: (a)(b) 밖의 빈 칸"] = {
            k for k, v in truth.items() if v == "empty" and k not in special
        }
        out["비교: (a)(b) 밖의 약 있는 칸"] = {
            k for k, v in truth.items() if v == "full" and k not in special
        }
    return out


def mean_confidence(rows: list[CellRow]) -> str:
    """안 보이는 칸을 뺀 칸의 평균 신뢰도. 애매 답변의 신뢰도도 포함한다."""
    values = [r.confidence for r in rows if r.truth != "not_visible"]
    return "-" if not values else f"{sum(values) / len(values):.3f}"


def simulate_retakes(rows: list[CellRow], threshold: float) -> dict:
    """실제 classify() 로 재촬영 규칙의 영향을 추정한다. 사진마다 칸을 '이번 칸'으로 하나씩 가정한다.

    사진에 이번 칸 정보가 없어서 쓰는 가정이다. 다른 칸의 기대 상태는 정답과 같다고 둔다(안 보이는 칸은
    ignored). '이번 칸만' 규칙은 다른 칸의 unclear 를 무시한 가상의 규칙으로, 서버에는 없다.
    """
    by_photo: dict[str, list[CellRow]] = {}
    for r in rows:
        by_photo.setdefault(r.photo_id, []).append(r)
    out = Counter()
    for cells in by_photo.values():
        for target in cells:
            if target.truth == "not_visible":
                continue
            expected = {
                c.cell_id: ("ignored" if c.truth == "not_visible" else c.truth)
                for c in cells
                if c is not target
            }
            expected[target.cell_id] = "full"
            prefix = "taken" if target.truth == "empty" else "skipped"
            out[f"{prefix}_n"] += 1
            for rule, relaxed in (("now", False), ("target", True)):
                readings = [
                    CellReading(
                        cell_id=c.cell_id,
                        state="full"
                        if relaxed and c is not target and c.state == "unclear"
                        else c.state,
                        confidence=c.confidence,
                    )
                    for c in cells
                ]
                result = CellReadings(provider="x", model="x", is_mock=False, cells=readings)
                kind = classify(result, expected, target.cell_id, threshold).kind
                out[f"{prefix}_{rule}_{kind}"] += 1
    return out


def agreement(a: Run, b: Run) -> tuple[int, int]:
    """같은 조건을 두 번 돌렸을 때 판독(state)이 같은 칸 수와 전체 칸 수."""
    other = {(r.photo_id, r.cell_id): r.state for r in b.rows}
    pairs = [
        (r.state, other[(r.photo_id, r.cell_id)])
        for r in a.rows
        if (r.photo_id, r.cell_id) in other
    ]
    return sum(x == y for x, y in pairs), len(pairs)


def _n(k: int, d: int) -> str:
    return "-" if d == 0 else f"{k}/{d} ({100 * k / d:.1f}%)"


def _table(head: list[str], rows: list[list]) -> list[str]:
    return [
        "| " + " | ".join(head) + " |",
        "|" + "---|" * len(head),
        *["| " + " | ".join(str(c) for c in row) + " |" for row in rows],
    ]


def _photos_of(rows: list[CellRow], pred) -> list[str]:
    return sorted({r.photo_id for r in rows if pred(r)})


def report(
    runs: list[Run],
    labels: LabelSet,
    draft: LabelSet | None,
    thresholds: list[float],
    tags: dict | None = None,
    pairs: list[tuple[str, str]] | None = None,
) -> str:
    lines = [
        f"# 판독 설정 비교 (사진 {len(labels.photos)}장)",
        "",
        "## 조건별 지표 (신뢰도 기준 적용 전)",
        "",
    ]
    rows = []
    for run in runs:
        c = counts(run.rows)
        s = summarize(run.result)
        upper = wilson_upper(c["false_empty"], c["full"])
        rows.append(
            [
                run.name,
                f"{len(labels.photos) - len(run.missing)}/{len(labels.photos)}",
                _n(c["correct"], c["visible"]),
                _n(c["correct"], c["visible"] - c["unclear"]),
                _n(c["unclear"], c["visible"]),
                _n(c["false_empty"], c["full"]),
                "-" if upper is None else f"{100 * upper:.1f}%",
                _n(c["false_full"], c["empty"]),
                f"{s['p50_ms']} / {s['p95_ms']}",
                s["cost_per_photo"],
                f"${run.result.cost_usd:.4f}",
            ]
        )
    lines += _table(
        [
            "조건",
            "판독된 사진",
            "칸 정확도",
            "판단한 칸 정확도",
            "애매",
            "약 있는데 빈 칸으로 봄",
            "그 95% 상한",
            "빈 칸인데 약 있다고 봄",
            "지연 p50/p95(ms)",
            "1장당 비용",
            "총비용",
        ],
        rows,
    )
    lines += ["", f"총비용 합계: ${sum(r.result.cost_usd for r in runs):.4f}", ""]

    lines += ["## 안 보이는 칸 처리", ""]
    rows = []
    for run in runs:
        c = counts(run.rows)
        rows.append(
            [
                run.name,
                c["hidden"],
                _n(c["hidden_unclear"], c["hidden"]),
                c["hidden_empty"],
                c["hidden_full"],
                ", ".join(
                    _photos_of(
                        run.rows, lambda r: r.truth == "not_visible" and r.state != "unclear"
                    )
                )
                or "-",
            ]
        )
    lines += _table(
        ["조건", "안 보이는 칸", "애매로 돌림", "빈 칸으로 추측", "약 있다고 추측", "추측한 사진"],
        rows,
    )

    lines += ["", "## 사진 단위", ""]
    rows = []
    n = len(labels.photos)
    for run in runs:
        wrong = {r.photo_id for r in run.rows if r.truth != "not_visible" and r.state != r.truth}
        any_unclear = {r.photo_id for r in run.rows if r.state == "unclear"}
        done = {r.photo_id for r in run.rows}
        rows.append(
            [
                run.name,
                _n(len(done - wrong), n),
                _n(len(any_unclear), n),
                len(_photos_of(run.rows, lambda r: r.truth == "full" and r.state == "empty")),
            ]
        )
    lines += _table(
        ["조건", "보이는 칸이 모두 정답", "애매 칸이 1개 이상", "위험 오류가 나온 사진 수"], rows
    )

    lines += ["", "## 신뢰도 기준별", ""]
    rows = []
    for run in runs:
        for th in thresholds:
            c = counts(run.rows, th)
            rows.append(
                [
                    run.name,
                    f"{th:.2f}",
                    _n(c["correct"], c["visible"]),
                    _n(c["correct"], c["visible"] - c["unclear"]),
                    _n(c["unclear"], c["visible"]),
                    _n(c["false_empty"], c["full"]),
                    _n(c["false_full"], c["empty"]),
                    _n(c["empty_passed"], c["empty"]),
                ]
            )
    lines += _table(
        [
            "조건",
            "기준",
            "칸 정확도",
            "판단한 칸 정확도",
            "애매",
            "약 있는데 빈 칸으로 봄",
            "빈 칸인데 약 있다고 봄",
            "빈 칸을 기준 넘겨 빈 칸으로 봄",
        ],
        rows,
    )

    lines += ["", "## 위험 오류 0을 지키는 가장 낮은 기준", ""]
    rows = []
    for run in runs:
        safe = lowest_safe_threshold(run.rows, thresholds)
        risky = [r.confidence for r in run.rows if r.truth == "full" and r.state == "empty"]
        rows.append(
            [
                run.name,
                "후보 중 없음" if safe is None else f"{safe:.2f}",
                len(risky),
                f"{max(risky):.2f}" if risky else "-",
            ]
        )
    lines += _table(["조건", "가장 낮은 기준", "기준 전 위험 오류", "그중 최고 신뢰도"], rows)

    lines += ["", "## 하위 집합 (칸 수는 안 보이는 칸 제외)", ""]
    rows = []
    for title, keys in subsets(labels, draft, tags).items():
        for run in runs:
            picked = [r for r in run.rows if (r.photo_id, r.cell_id) in keys]
            c = counts(picked)
            rows.append(
                [
                    title,
                    run.name,
                    c["visible"],
                    _n(c["correct"], c["visible"]),
                    _n(c["unclear"], c["visible"]),
                    mean_confidence(picked),
                    _n(c["false_empty"], c["full"]),
                    _n(c["false_full"], c["empty"]),
                ]
            )
    lines += _table(
        [
            "집합",
            "조건",
            "칸",
            "칸 정확도",
            "애매",
            "평균 신뢰도",
            "약 있는데 빈 칸으로 봄",
            "빈 칸인데 약 있다고 봄",
        ],
        rows,
    )

    lines += [
        "",
        "## 재촬영 규칙 영향 추정 (가정: 각 칸을 이번 칸으로 하나씩 가정, 실제 classify 사용)",
        "",
    ]
    rows = []
    for run in runs:
        for th in thresholds:
            s = simulate_retakes(run.rows, th)
            t, sk = s["taken_n"], s["skipped_n"]
            rows.append(
                [
                    run.name,
                    f"{th:.2f}",
                    _n(s["taken_now_normal"], t),
                    _n(s["taken_now_unclear"], t),
                    _n(s["taken_target_unclear"], t),
                    _n(s["taken_now_double_suspected"], t),
                    _n(s["skipped_now_unclear"], sk),
                    _n(s["skipped_target_unclear"], sk),
                    _n(s["skipped_now_normal"], sk),
                    _n(s["skipped_target_normal"], sk),
                ]
            )
    lines += _table(
        [
            "조건",
            "기준",
            "드신 경우: 정상 기록",
            "드신 경우: 재촬영(현재)",
            "드신 경우: 재촬영(이번 칸만)",
            "드신 경우: 이중 복용 의심",
            "안 드신 경우: 재촬영(현재)",
            "안 드신 경우: 재촬영(이번 칸만)",
            "안 드신 경우: 잘못 정상 기록(현재)",
            "안 드신 경우: 잘못 정상 기록(이번 칸만)",
        ],
        rows,
    )

    if pairs:
        by_name = {r.name: r for r in runs}
        lines += ["", "## 같은 조건 반복 실행의 일치", ""]
        lines += _table(
            ["비교", "판독이 같은 칸"],
            [[f"{a} / {b}", _n(*agreement(by_name[a], by_name[b]))] for a, b in pairs],
        )

    lines += ["", "## 실패 사례 (사진 번호만)", ""]
    for run in runs:
        fe = Counter(r.photo_id for r in run.rows if r.truth == "full" and r.state == "empty")
        ff = Counter(r.photo_id for r in run.rows if r.truth == "empty" and r.state == "full")
        un = Counter(
            r.photo_id for r in run.rows if r.truth != "not_visible" and r.state == "unclear"
        )
        lines += [
            f"- **{run.name}**",
            f"  - 약 있는데 빈 칸으로 봄: {_fmt(fe)}",
            f"  - 빈 칸인데 약 있다고 봄: {_fmt(ff)}",
            f"  - 보이는 칸을 애매로 답함: {_fmt(un)}",
            f"  - 판독 실패: {', '.join(run.missing) or '-'}",
        ]
    lines += ["", "## 모든 조건이 라벨과 다르게 답한 칸이 있는 사진 (라벨 재확인 후보)", ""]
    per_run = [
        {
            (r.photo_id, r.cell_id)
            for r in run.rows
            if r.truth != "not_visible" and r.state in ("empty", "full") and r.state != r.truth
        }
        for run in runs
    ]
    common = set.intersection(*per_run) if per_run else set()
    lines.append(", ".join(sorted({p for p, _ in common})) or "없음")
    return "\n".join(lines) + "\n"


def _fmt(c: Counter) -> str:
    if not c:
        return "없음"
    return ", ".join(f"{p}({n}칸)" if n > 1 else p for p, n in sorted(c.items()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--draft", type=Path, help="초안 라벨 (고친 칸 집계용, 없으면 생략)")
    ap.add_argument("--tags", type=Path, help="칸 표시 파일 (lid_open, partial. 커밋하지 않는다)")
    ap.add_argument("--run", action="append", required=True, metavar="이름=폴더")
    ap.add_argument(
        "--pair", action="append", default=[], metavar="이름,이름", help="반복 실행 일치 비교"
    )
    ap.add_argument("--thresholds", default="0.75,0.8,0.85,0.9,0.95")
    ap.add_argument("--out", type=Path, help="표(md)를 저장할 곳")
    args = ap.parse_args(argv)

    labels, _ = load_labels(args.labels)
    if labels.drafts():
        print(f"라벨 {len(labels.drafts())}장이 확인 전 초안이다. 비교하지 않는다", file=sys.stderr)
        return 2
    draft = load_labels(args.draft)[0] if args.draft else None
    thresholds = [float(t) for t in args.thresholds.split(",") if t.strip()]
    runs = []
    for item in args.run:
        name, _, directory = item.partition("=")
        runs.append(load_run(name, Path(directory), labels))
    tags = json.loads(args.tags.read_text(encoding="utf-8")) if args.tags else None
    pairs = [tuple(p.split(",", 1)) for p in args.pair]
    text = report(runs, labels, draft, thresholds, tags, pairs)
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
