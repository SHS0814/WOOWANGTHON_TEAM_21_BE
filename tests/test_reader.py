import pytest

from app.config import Settings
from app.reader import Layout, MockReader, ReaderUnavailable, build_reader
from app.reader.base import CellReading, normalize_cells


def test_layout_fills_cells():
    layout = Layout(rows=2, cols=7)
    assert len(layout.cells) == 14
    assert layout.cell_ids()[0] == "r0c0" and layout.cell_ids()[-1] == "r1c6"


def test_layout_rejects_cell_outside_grid():
    with pytest.raises(ValueError):
        Layout(rows=1, cols=7, cells=[{"id": "r1c0", "row": 1, "col": 0}])


def test_mock_reader_is_marked_mock_and_deterministic():
    reader = MockReader()
    layout = Layout(rows=4, cols=7)
    a = reader.read_cells(b"photo-bytes", layout)
    b = reader.read_cells(b"photo-bytes", layout)
    assert a.is_mock is True
    assert [c.cell_id for c in a.cells] == layout.cell_ids()
    assert a.cells == b.cells
    assert reader.detect_layout(b"x").is_mock is True


def test_normalize_cells_marks_missing_as_unclear():
    layout = Layout(rows=1, cols=2)
    cells = [
        CellReading(cell_id="r0c1", state="full", confidence=0.9),
        CellReading(cell_id="zzz", state="empty", confidence=0.9),
    ]
    out = normalize_cells(layout, cells)
    assert [(c.cell_id, c.state) for c in out] == [("r0c0", "unclear"), ("r0c1", "full")]


def test_claude_reader_unavailable_without_key():
    with pytest.raises(ReaderUnavailable):
        build_reader("claude", Settings(_env_file=None, anthropic_api_key=None))


def test_ollama_reader_unavailable_without_server():
    settings = Settings(_env_file=None, ollama_url="http://127.0.0.1:9")
    with pytest.raises(ReaderUnavailable):
        build_reader("ollama", settings)


def test_mock_reader_baseline_is_all_full():
    """R-015 C1: 등록·리필 기준선 판독은 모두 full (매일 판독의 무작위성은 그대로)."""
    reader = MockReader(empty_ratio=0.9, unclear_ratio=0.1)
    layout = Layout(rows=2, cols=7)
    base = reader.read_cells(b"photo", layout, purpose="baseline")
    assert base.is_mock is True
    assert {c.state for c in base.cells} == {"full"}
    daily = reader.read_cells(b"photo", layout)
    assert {c.state for c in daily.cells} != {"full"}
