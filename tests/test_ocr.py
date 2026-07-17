from __future__ import annotations

from types import SimpleNamespace

from schriftlotse.ocr import OrliLineDetector


def test_orli_boundaries_become_clamped_line_boxes() -> None:
    lines = [
        SimpleNamespace(boundary=[(-4, 30), (120, 28), (125, 55), (0, 57)]),
        SimpleNamespace(boundary=None, baseline=[(20, 90), (180, 92)]),
    ]
    boxes = OrliLineDetector._boxes(lines, width=200, height=120)
    assert boxes[0][0] == 0
    assert boxes[0][2] <= 200
    assert boxes[1][1] < 90 < boxes[1][3]
