import json
from pathlib import Path

import pytest

from app.reader.base import CellReadings
from harness import review_sheet, run
from harness.labels import load_labels
from harness.pricing import PRICES, call_cost

SAMPLES = Path(__file__).parent.parent / "harness" / "samples" / "labels.json"


def test_sample_labels_are_valid():
    labels, base = load_labels(SAMPLES)
    assert len(labels.photos) >= 3
    for p in labels.photos:
        assert (base / p.file).exists()


def test_run_with_mock_and_skips_unavailable(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9")
    run.get_settings.cache_clear()
    code = run.main(
        [
            "--labels",
            str(SAMPLES),
            "--readers",
            "mock,claude,ollama",
            "--layout",
            "--thresholds",
            "0.8",
            "--out-dir",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "mock (모의)" in out
    assert "건너뛴 판독기" in out and "`claude`" in out and "`ollama`" in out
    assert len(list(tmp_path.glob("*-cells.csv"))) == 1
    assert len(list(tmp_path.glob("*-calls.csv"))) == 1


def _write_labels(tmp_path, cells, status="confirmed"):
    (tmp_path / "p.png").write_bytes((SAMPLES.parent / "synthetic-1x7.png").read_bytes())
    data = {
        "photos": [
            {
                "id": "p1",
                "file": "p.png",
                "pillbox": {"layout": {"rows": 1, "cols": 3}},
                "cells": cells,
                "label_status": status,
            }
        ]
    }
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_not_visible_label_accepted_but_unknown_value_rejected(tmp_path):
    path = _write_labels(tmp_path, {"r0c0": "full", "r0c1": "not_visible", "r0c2": "empty"})
    labels, _ = load_labels(path)
    assert labels.photos[0].cells["r0c1"] == "not_visible"
    bad = _write_labels(tmp_path, {"r0c0": "full", "r0c1": "unclear", "r0c2": "empty"})
    with pytest.raises(ValueError):
        load_labels(bad)


def test_summarize_excludes_not_visible_and_counts_unclear():
    res = run.ReaderResult(spec="x", photos=1)
    rows = [
        ("r0c0", "full", "full"),
        ("r0c1", "full", "empty"),  # 위험 오류
        ("r0c2", "not_visible", "unclear"),
        ("r0c3", "not_visible", "full"),  # 안 보이는 칸을 추측
    ]
    res.rows = [run.CellRow("x", "p1", cid, t, s, 0.9, False) for cid, t, s in rows]
    out = run.summarize(res)
    assert out["cells"] == 2
    assert out["cell_acc"] == "50.0%"
    assert out["false_empty"] == "50.0%"
    assert out["hidden_cells"] == 2
    assert out["hidden_unclear"] == "50.0%"


def test_draft_labels_refused_unless_allowed(tmp_path, capsys):
    path = _write_labels(
        tmp_path, {"r0c0": "full", "r0c1": "not_visible", "r0c2": "empty"}, status="draft"
    )
    assert run.main(["--labels", str(path), "--readers", "mock"]) == 2
    assert run.main(["--labels", str(path), "--readers", "mock", "--allow-draft"]) == 0
    out = capsys.readouterr().out
    assert "결과로 쓰지 않는다" in out and "안 보이는 칸" in out


def test_cost_per_photo():
    assert call_cost("ollama", "qwen2.5vl:7b", None, None) == 0.0
    assert call_cost("openai", "unknown-model", 10, 10) is None
    model = next(iter(PRICES))
    res = run.ReaderResult(spec="x", photos=2)
    meta = CellReadings(
        provider="claude",
        model=model,
        is_mock=False,
        cells=[],
        input_tokens=1_000_000,
        output_tokens=0,
    )
    res.add_cost(meta)
    assert run.summarize(res)["cost_per_photo"] == f"${PRICES[model][0] / 2:.5f}"
    res.add_cost(meta.model_copy(update={"input_tokens": None}))
    assert run.summarize(res)["cost_per_photo"].startswith("모름")


def test_review_sheet_builds(tmp_path):
    path = _write_labels(
        tmp_path, {"r0c0": "full", "r0c1": "not_visible", "r0c2": "empty"}, status="draft"
    )
    review_sheet.main([str(path)])
    html = (tmp_path / "review.html").read_text(encoding="utf-8")
    assert "AI 에이전트(Claude)가 사진을 눈으로 보고 만든 것" in html
    assert '"label_status": "draft"' in html
