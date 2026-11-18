# Screen-region evaluation

Run `python tools/evaluate_screen_regions.py annotations.json` on independently
annotated, licensed recordings. Each example corresponds to one timestamp and
contains a unique `id`, a `category` (such as `screencast`, `talking_head`, or
`slides`), an independently labelled `expected` rectangle, and the analyzer's
`active_roi` as `predicted` (or `null` when unavailable). Rectangles use
normalized source coordinates: `{ "x": 0.1, "y": 0.2, "w": 0.4, "h": 0.3 }`.

The report provides counts and mean intersection over union, with missing
predictions scored as zero, overall and per category. Label provenance, media
licenses, sampling protocol, inter-annotator agreement and coverage must be
recorded separately. An empty dataset is rejected. This is an evaluation aid,
not a calibrated model, and agreement with boxes does not establish semantic
importance. Synthetic examples do not establish performance on real media.
