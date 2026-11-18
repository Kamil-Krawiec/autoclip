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
