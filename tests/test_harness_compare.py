import csv
import json

import pytest

from harness import compare
from harness.labels import load_labels
from harness.run import CellRow

COLS = ["月", "火", "水", "木", "金", "土", "日"]


def _row(truth, state, conf=0.99, photo="real-001", cell="r0c0"):
    return CellRow("x", photo, cell, truth, state, conf, False)


def _labels(tmp_path, cells=None, status="confirmed", notes="", name="labels.json"):
    cells = cells or {f"r0c{i}": "full" for i in range(7)}
    data = {
        "photos": [
            {
                "id": "real-001",
                "file": "p.png",
                "pillbox": {
                    "layout": {"rows": 1, "cols": 7, "row_labels": [""], "col_labels": COLS}
                },
                "cells": cells,
                "label_status": status,
                "conditions": {"crop": "strip", "orientation": "horizontal"},
                "notes": notes,
            }
        ]
    }
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _run_dir(tmp_path, name, rows, calls):
    d = tmp_path / name
    d.mkdir()
    with (d / "1-cells.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["reader", "photo_id", "cell_id", "truth", "state", "confidence", "is_target"])
        for r in rows:
            w.writerow([name, r.photo_id, r.cell_id, r.truth, r.state, r.confidence, False])
    with (d / "1-calls.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "reader",
                "photo_id",
                "kind",
                "latency_ms",
                "input_tokens",
                "output_tokens",
                "cost_usd",
            ]
        )
        for c in calls:
            w.writerow([name, "real-001", "cells", c[0], 1, 1, c[1]])
    return d


def test_counts_separates_hidden_and_error_directions():
    c = compare.counts(
        [
            _row("full", "empty"),
            _row("full", "full"),
            _row("empty", "full"),
            _row("empty", "unclear"),
            _row("not_visible", "empty"),
            _row("not_visible", "unclear"),
        ]
    )
    assert (c["visible"], c["correct"], c["unclear"]) == (4, 1, 1)
    assert (c["full"], c["false_empty"]) == (2, 1)
    assert (c["empty"], c["false_full"], c["empty_passed"]) == (2, 1, 0)
    assert (c["hidden"], c["hidden_unclear"], c["hidden_empty"]) == (2, 1, 1)


def test_threshold_turns_low_confidence_into_unclear_with_strict_lower_bound():
    rows = [_row("full", "empty", 0.8), _row("empty", "empty", 0.9)]
    assert compare.counts(rows, 0.8)["false_empty"] == 1  # 기준과 같으면 판단으로 남는다
    c = compare.counts(rows, 0.85)
    assert (c["false_empty"], c["unclear"], c["empty_passed"]) == (0, 1, 1)
    assert compare.counts(rows, 0.95)["empty_passed"] == 0


def test_lowest_safe_threshold():
    rows = [_row("full", "empty", 0.82), _row("full", "full", 0.5)]
    assert compare.lowest_safe_threshold(rows, [0.9, 0.75, 0.85]) == 0.85
    assert compare.lowest_safe_threshold([_row("full", "empty", 0.99)], [0.9, 0.95]) is None
    assert compare.lowest_safe_threshold([_row("full", "full")], [0.9, 0.75]) == 0.75


def test_wilson_upper_for_zero_errors_is_not_zero():
    assert compare.wilson_upper(0, 0) is None
    assert 0.01 < compare.wilson_upper(0, 221) < 0.02
    assert compare.wilson_upper(221, 221) == 1.0


def test_lid_open_cells_read_from_notes(tmp_path):
    labels, _ = load_labels(_labels(tmp_path, notes="전체 사진. 火 뚜껑 열림, 라벨 안 보임"))
    assert compare.lid_open_cells(labels) == {("real-001", "r0c1")}


def test_report_lists_photo_numbers_only_and_flags_failed_reading(tmp_path):
    secret_note = "메모내용비공개"
    labels, _ = load_labels(_labels(tmp_path, notes=secret_note))
    rows = [_row("full", "empty", 0.7, cell=f"r0c{i}") for i in range(7)]
    ok = compare.load_run("a", _run_dir(tmp_path, "a", rows, [(1000, 0.001)]), labels)
    failed = compare.load_run("b", _run_dir(tmp_path, "b", [], []), labels)
    text = compare.report([ok, failed], labels, None, [0.75, 0.9])
    assert secret_note not in text
    assert "real-001(7칸)" in text
    assert "0/1" in text  # 판독 실패한 조건의 판독된 사진 수
    assert "- **b**" in text and "판독 실패: real-001" in text
    assert "| a | 0.75 |" in text


def test_load_run_requires_exactly_one_result(tmp_path):
    labels, _ = load_labels(_labels(tmp_path))
    d = _run_dir(tmp_path, "a", [], [])
    (d / "2-cells.csv").write_text("", encoding="utf-8")
    with pytest.raises(SystemExit):
        compare.load_run("a", d, labels)


def test_main_refuses_draft_labels(tmp_path, capsys):
    path = _labels(tmp_path, status="draft")
    assert compare.main(["--labels", str(path), "--run", f"a={tmp_path}"]) == 2
    assert "초안" in capsys.readouterr().err


def test_tags_define_lid_and_partial_subsets(tmp_path):
    labels, _ = load_labels(_labels(tmp_path))
    labels.photos[0].check_cells = ["r0c2"]
    tags = {"lid_open": {"real-001": ["r0c0"]}, "partial": {"real-001": ["r0c1"]}}
    out = compare.subsets(labels, None, tags)
    assert out["(a) 뚜껑 열린 칸 (사진 표시)"] == {("real-001", "r0c0")}
    both = out["(b) 일부만 보이는 칸 (사진 표시 + 확신 못 한 칸 + 고친 칸)"]
    assert both == {("real-001", "r0c1"), ("real-001", "r0c2")}
    assert "뚜껑 열린 칸 (메모 기준)" not in out


def test_mean_confidence_skips_hidden_cells():
    assert (
        compare.mean_confidence([_row("empty", "empty", 0.8), _row("not_visible", "empty", 0.1)])
        == "0.800"
    )
    assert compare.mean_confidence([]) == "-"


def test_simulate_retakes_shows_other_cell_unclear_forces_retake():
    rows = [
        _row("empty", "empty", 0.95, cell="r0c0"),
        _row("full", "full", 0.99, cell="r0c1"),
        _row("not_visible", "unclear", 0.1, cell="r0c2"),
    ]
    s = compare.simulate_retakes(rows, 0.9)
    assert s["taken_n"] == 1 and s["skipped_n"] == 1
    assert s["taken_now_unclear"] == 1  # 안 보이는 칸 때문에 재촬영
    assert s["taken_target_normal"] == 1  # 다른 칸을 무시하면 정상


def test_simulate_retakes_counts_false_normal_when_target_misread():
    rows = [_row("full", "empty", 0.95, cell="r0c0"), _row("full", "full", 0.99, cell="r0c1")]
    assert compare.simulate_retakes(rows, 0.9)["skipped_now_normal"] == 1
    assert compare.simulate_retakes(rows, 0.96)["skipped_now_normal"] == 0


def test_agreement_counts_same_state_on_shared_cells():
    a = compare.Run("a", compare.ReaderResult(spec="a"), [])
    b = compare.Run("b", compare.ReaderResult(spec="b"), [])
    a.result.rows = [_row("full", "full", cell="r0c0"), _row("full", "empty", cell="r0c1")]
    b.result.rows = [_row("full", "full", cell="r0c0"), _row("full", "full", cell="r0c1")]
    assert compare.agreement(a, b) == (1, 2)
