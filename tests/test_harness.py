from pathlib import Path

from harness import run
from harness.labels import load_labels

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
