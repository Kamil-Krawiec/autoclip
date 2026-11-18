import pytest

from tools.evaluate_screen_regions import evaluate


def test_evaluation_counts_missing_predictions_and_categories():
    box = {"x": 0, "y": 0, "w": 0.5, "h": 0.5}
    result = evaluate(
        {
            "examples": [
                {"id": "1", "category": "slides", "expected": box, "predicted": box},
                {"id": "2", "category": "slides", "expected": box, "predicted": None},
            ]
        }
    )
    assert result["overall"] == {"examples": 2, "predicted": 1, "mean_iou_including_misses": 0.5}
    assert result["by_category"]["slides"] == result["overall"]


def test_evaluation_rejects_invalid_or_duplicate_examples():
    box = {"x": 0, "y": 0, "w": 0.5, "h": 0.5}
    item = {"id": "1", "category": "screen", "expected": box, "predicted": None}
    with pytest.raises(ValueError):
        evaluate({"examples": [item, item]})
    with pytest.raises(ValueError):
        evaluate({"examples": [{**item, "predicted": {**box, "x": 0.9}}]})


def test_overlap_and_category_aggregation():
    box = {"x": 0, "y": 0, "w": 0.5, "h": 0.5}
    partial = {"x": 0.25, "y": 0, "w": 0.5, "h": 0.5}
    disjoint = {"x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5}
    result = evaluate(
        {
            "examples": [
                {"id": "1", "category": "slides", "expected": box, "predicted": partial},
                {"id": "2", "category": "screen", "expected": box, "predicted": disjoint},
            ]
        }
    )
    assert result["by_category"]["slides"]["mean_iou_including_misses"] == pytest.approx(1 / 3)
    assert result["by_category"]["screen"]["mean_iou_including_misses"] == 0
    assert result["overall"]["mean_iou_including_misses"] == pytest.approx(1 / 6)


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"examples": []},
        {"examples": [None]},
        {
            "examples": [
                {"id": "1", "category": "screen", "expected": {"x": 0, "y": 0, "w": 1, "h": 1}}
            ]
        },
    ],
)
def test_invalid_input_is_rejected(data):
    with pytest.raises(ValueError):
        evaluate(data)


def test_cli_reads_annotations(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    box = {"x": 0, "y": 0, "w": 1, "h": 1}
    path = tmp_path / "annotations.json"
    path.write_text(
        json.dumps(
            {"examples": [{"id": "1", "category": "screen", "expected": box, "predicted": None}]}
        )
    )
    script = Path(__file__).resolve().parents[1] / "tools" / "evaluate_screen_regions.py"
    result = subprocess.run(
        [sys.executable, str(script), str(path)], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout)["overall"]["mean_iou_including_misses"] == 0
