"""Evaluate predicted active regions against independently annotated examples.

Input JSON: {"examples": [{"id": "...", "category": "...", "expected":
{"x": 0, "y": 0, "w": 1, "h": 1}, "predicted": null}]}. Coordinates
are normalized to the source image; null prediction counts as a miss.
"""

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def _box(value):
    if not isinstance(value, dict) or set(value) != {"x", "y", "w", "h"}:
        raise ValueError("A region must contain x, y, w and h")
    vals = [value[k] for k in ("x", "y", "w", "h")]
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vals
    ):
        raise ValueError("Region coordinates must be finite numbers")
    x, y, w, h = vals
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1.000001 or y + h > 1.000001:
        raise ValueError("Regions must be positive and within normalized image bounds")
    return x, y, w, h


def evaluate(data):
    if not isinstance(data, dict):
        raise ValueError("Input must be an object containing examples")
    examples = data.get("examples")
    if not isinstance(examples, list) or not examples:
        raise ValueError("At least one independently annotated example is required")
    groups = defaultdict(list)
    seen = set()
    for example in examples:
        if not isinstance(example, dict):
            raise ValueError("Each example must be an object")
        key = example.get("id")
        category = example.get("category")
        if (
            not isinstance(key, str)
            or not key
            or key in seen
            or not isinstance(category, str)
            or not category
        ):
            raise ValueError("Each example needs a unique id and category")
        seen.add(key)
        x, y, w, h = _box(example.get("expected"))
        if "predicted" not in example:
            raise ValueError("Each example must explicitly provide predicted (or null)")
        prediction = example["predicted"]
        iou = None
        if prediction is not None:
            px, py, pw, ph = _box(prediction)
            intersection = max(0, min(x + w, px + pw) - max(x, px)) * max(
                0, min(y + h, py + ph) - max(y, py)
            )
            iou = intersection / (w * h + pw * ph - intersection)
        groups[category].append(iou)

    def summary(values):
        return {
            "examples": len(values),
            "predicted": sum(v is not None for v in values),
            "mean_iou_including_misses": sum(v or 0 for v in values) / len(values),
        }

    return {
        "overall": summary([v for values in groups.values() for v in values]),
        "by_category": {category: summary(values) for category, values in sorted(groups.items())},
        "limitations": (
            "IoU measures agreement with supplied boxes, not semantic importance or calibration. "
            "Results depend on independent labels and representative licensed media."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("annotations", type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate(json.loads(args.annotations.read_text(encoding="utf-8"))), indent=2))


if __name__ == "__main__":
    main()
