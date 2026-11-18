"""Lightweight CPU screen-scene, scroll, and ROI analyzer for MCP framing hints.

Advisory only: never modifies AutoClip's highlight selection or global auto
reframe behavior. Recommends existing framing modes (auto, manual, split,
slices) and falls back to ``fit`` (blurred background) whenever uncertain or
when active screen content exceeds the target crop width.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np
from scenedetect import FrameTimecode
from scenedetect.detectors import AdaptiveDetector, HashDetector

from .reframe.croppath import CropPath, Strategy, target_crop_size
from .visual_inspection import InspectionError, InspectionValidationError

SceneKind = Literal["speaker", "terminal_or_code", "scroll", "mixed_screen_pip", "uncertain"]

_MIN_SEGMENT_S = 1.0
_MAX_FRAMING_SLICES = 8
_DIFF_THRESHOLD = 18
_SCROLL_MIN_RESPONSE = 0.25
_SCROLL_MIN_DY_PX = 2.0


@dataclass(frozen=True)
class ScreenSegmentHint:
    source_start_s: float
    source_end_s: float
    clip_start_s: float
    clip_end_s: float
    scene_type: SceneKind
    is_scroll: bool
    scroll_direction: Literal["up", "down", "none"]
    active_roi: dict[str, float] | None
    recommended_mode: Literal["auto", "fit", "manual", "split"]
    recommended_framing: dict[str, Any]
    confidence: float
    reason: str
    caption_clearance_y: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ScreenAnalysisResult:
    source_width: int
    source_height: int
    target_ratio: str
    elapsed_ms: float
    sampled_frames: int
    cuts_detected: list[float]
    segments: list[ScreenSegmentHint]
    scenes: list[dict[str, Any]]
    cut_frame_pairs: list[dict[str, float]]
    recommended_framing: dict[str, Any]
    overall_confidence: float
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_dimensions": {"width": self.source_width, "height": self.source_height},
            "target_ratio": self.target_ratio,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "sampled_frames": self.sampled_frames,
            "cuts_detected": [round(t, 3) for t in self.cuts_detected],
            "segments": [s.to_dict() for s in self.segments],
            "scenes": self.scenes,
            "cut_frame_pairs": self.cut_frame_pairs,
            "recommended_framing": self.recommended_framing,
            "overall_confidence": round(self.overall_confidence, 3),
            "notes": self.notes,
        }


@dataclass
class _PairSample:
    prev_t_rel: float
    t_rel: float
    diff_mask: np.ndarray
    changed_ratio: float
    hist_corr: float
    is_scroll: bool
    dy: float
    response: float


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _crop_box_norm_at(
    crop_path: CropPath | None, t_rel: float
) -> tuple[Strategy | None, tuple[float, float, float, float] | None]:
    if crop_path is None or not crop_path.segments or crop_path.source_width <= 0:
        return None, None
    seg = crop_path.segments[-1]
    for candidate in crop_path.segments:
        if candidate.start_s - 1e-6 <= t_rel <= candidate.end_s + 1e-6:
            seg = candidate
            if t_rel < candidate.end_s - 1e-6:
                break
    if seg.fit or seg.split or not seg.keyframes:
        return seg.strategy, None
    kf = seg.keyframes[0]
    w_norm = seg.width / float(crop_path.source_width)
    h_norm = seg.height / float(crop_path.source_height)
    x_norm = _clamp(kf.x / float(crop_path.source_width), 0.0, max(0.0, 1.0 - w_norm))
    y_norm = _clamp(kf.y / float(crop_path.source_height), 0.0, max(0.0, 1.0 - h_norm))
    return seg.strategy, (x_norm, y_norm, w_norm, h_norm)


def _extract_roi_from_mask(
    mask: np.ndarray,
) -> tuple[tuple[float, float, float, float] | None, float]:
    h, w = mask.shape[:2]
    total_area = float(w * h)
    if total_area <= 0:
        return None, 0.0

    kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 7))
    kernel_dilate = cv2.getStructuringElement(cv2.MORPH_RECT, (13, 9))
    cleaned = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel_close)
    cleaned = cv2.dilate(cleaned, kernel_dilate)

    num_labels, _, stats, _ = cv2.connectedComponentsWithStats(cleaned)
    min_area = max(120, int(0.003 * total_area))
    boxes: list[tuple[int, int, int, int, int]] = []
    for label in range(1, num_labels):
        x, y, bw, bh, area = (int(v) for v in stats[label])
        if area < min_area or bw < 16 or bh < 12:
            continue
        boxes.append((x, y, bw, bh, area))

    if not boxes:
        return None, 0.0

    # Drop tiny distant cursor-like outliers if a dominant cluster is >= 4x larger.
    max_area = max(b[4] for b in boxes)
    kept = [b for b in boxes if b[4] >= max_area * 0.12]
    x0 = min(b[0] for b in kept)
    y0 = min(b[1] for b in kept)
    x1 = max(b[0] + b[2] for b in kept)
    y1 = max(b[1] + b[3] for b in kept)

    roi = (
        round(x0 / w, 4),
        round(y0 / h, 4),
        round((x1 - x0) / w, 4),
        round((y1 - y0) / h, 4),
    )
    active_frac = sum(b[4] for b in kept) / total_area
    return roi, float(active_frac)


def _make_split_panels(
    screen_roi: tuple[float, float, float, float],
    speaker_box: tuple[float, float, float, float],
) -> tuple[dict[str, float], dict[str, float]] | None:
    sx, sy, sw, sh = screen_roi
    px, py, pw, ph = speaker_box
    # Ensure horizontal separation or non-trivial overlap check
    ix0, iy0 = max(sx, px), max(sy, py)
    ix1, iy1 = min(sx + sw, px + pw), min(sy + sh, py + ph)
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    min_box = min(sw * sh, pw * ph)
    if min_box > 0 and inter / min_box > 0.35:
        return None

    def _norm_panel(x: float, y: float, w: float, h: float) -> dict[str, float]:
        pad_x = min(0.04, x, max(0.0, 1.0 - (x + w)))
        nx = round(_clamp(x - pad_x, 0.0, 0.95), 4)
        ny = round(_clamp(y, 0.0, 0.95), 4)
        nw = round(_clamp(w + 2 * pad_x, 0.05, max(0.05, 1.0 - nx)), 4)
        nh = round(_clamp(h, 0.05, max(0.05, 1.0 - ny)), 4)
        return {
            "x": nx,
            "y": ny,
            "width": min(nw, round(1.0 - nx, 4)),
            "height": min(nh, round(1.0 - ny, 4)),
        }

    return _norm_panel(sx, sy, sw, sh), _norm_panel(px, py, pw, ph)


def _merge_cut_times(cuts: list[float], duration_s: float) -> list[float]:
    if not cuts:
        return []
    return sorted({c for c in cuts if 0 < c < duration_s})


def _build_segment_hint(
    *,
    seg_clip_start: float,
    seg_clip_end: float,
    clip_start_abs: float,
    clip_end_abs: float,
    pairs: list[_PairSample],
    src_w: int,
    src_h: int,
    ratio: str,
    crop_path: CropPath | None,
) -> ScreenSegmentHint:
    src_start = clip_start_abs + seg_clip_start
    src_end = min(clip_end_abs, clip_start_abs + seg_clip_end)
    mid_t = (seg_clip_start + seg_clip_end) / 2.0
    strategy, speaker_box = _crop_box_norm_at(crop_path, mid_t)

    if not pairs:
        return ScreenSegmentHint(
            source_start_s=src_start,
            source_end_s=src_end,
            clip_start_s=round(seg_clip_start, 4),
            clip_end_s=round(seg_clip_end, 4),
            scene_type="uncertain",
            is_scroll=False,
            scroll_direction="none",
            active_roi=None,
            recommended_mode="fit",
            recommended_framing={"mode": "fit"},
            confidence=0.2,
            reason="Insufficient sampled frames; using fit with blurred background.",
            caption_clearance_y=0.84,
        )

    scroll_pairs = [p for p in pairs if p.is_scroll]
    scroll_frac = len(scroll_pairs) / len(pairs)
    is_scroll = scroll_frac >= 0.35 and len(scroll_pairs) >= 2
    mean_dy = float(np.mean([p.dy for p in scroll_pairs])) if scroll_pairs else 0.0
    scroll_dir: Literal["up", "down", "none"] = (
        ("down" if mean_dy < 0 else "up") if is_scroll else "none"
    )

    union_mask = np.zeros_like(pairs[0].diff_mask, dtype=np.uint8)
    for p in pairs:
        union_mask = np.maximum(union_mask, p.diff_mask)

    roi, active_frac = _extract_roi_from_mask(union_mask)
    roi_dict = {"x": roi[0], "y": roi[1], "w": roi[2], "h": roi[3]} if roi is not None else None

    aspect_w, aspect_h = map(int, ratio.split(":"))
    crop_px_w, crop_px_h = target_crop_size(src_w, src_h, aspect_w, aspect_h)
    target_w_norm = crop_px_w / float(src_w)
    target_h_norm = crop_px_h / float(src_h)

    # Check speaker / PIP vs non-face screen activity
    if strategy in (Strategy.TRACK, Strategy.WIDE) and speaker_box is not None:
        sx, sy, sw, sh = speaker_box
        outside_mask = union_mask.copy()
        mh, mw = outside_mask.shape[:2]
        ox0 = max(0, int(sx * mw))
        oy0 = max(0, int(sy * mh))
        ox1 = min(mw, int((sx + sw) * mw))
        oy1 = min(mh, int((sy + sh) * mh))
        outside_mask[oy0:oy1, ox0:ox1] = 0
        outside_roi, outside_frac = _extract_roi_from_mask(outside_mask)

        if outside_roi is None or outside_frac < 0.015:
            return ScreenSegmentHint(
                source_start_s=src_start,
                source_end_s=src_end,
                clip_start_s=round(seg_clip_start, 4),
                clip_end_s=round(seg_clip_end, 4),
                scene_type="speaker",
                is_scroll=False,
                scroll_direction="none",
                active_roi=roi_dict,
                recommended_mode="auto",
                recommended_framing={"mode": "auto"},
                confidence=0.85,
                reason="Tracked speaker dominates motion without separate screen activity.",
                caption_clearance_y=0.86,
            )

        # Mixed screen + speaker PIP: try split panels if clearly separated
        panels = _make_split_panels(outside_roi, speaker_box)
        if panels is not None:
            return ScreenSegmentHint(
                source_start_s=src_start,
                source_end_s=src_end,
                clip_start_s=round(seg_clip_start, 4),
                clip_end_s=round(seg_clip_end, 4),
                scene_type="mixed_screen_pip",
                is_scroll=is_scroll,
                scroll_direction=scroll_dir,
                active_roi={
                    "x": outside_roi[0],
                    "y": outside_roi[1],
                    "w": outside_roi[2],
                    "h": outside_roi[3],
                },
                recommended_mode="split",
                recommended_framing={"mode": "split", "panels": list(panels)},
                confidence=0.74,
                reason=(
                    "Active screen region and speaker region are separated; "
                    "split keeps both visible."
                ),
                caption_clearance_y=None,
            )

    if roi is None or active_frac < 0.005:
        return ScreenSegmentHint(
            source_start_s=src_start,
            source_end_s=src_end,
            clip_start_s=round(seg_clip_start, 4),
            clip_end_s=round(seg_clip_end, 4),
            scene_type="uncertain",
            is_scroll=is_scroll,
            scroll_direction=scroll_dir,
            active_roi=None,
            recommended_mode="fit",
            recommended_framing={"mode": "fit"},
            confidence=0.25,
            reason="Low or ambiguous visual activity; falling back to fit blur.",
            caption_clearance_y=0.84,
        )

    rx, ry, rw, rh = roi
    if is_scroll and rw > target_w_norm * 1.15:
        return ScreenSegmentHint(
            source_start_s=src_start,
            source_end_s=src_end,
            clip_start_s=round(seg_clip_start, 4),
            clip_end_s=round(seg_clip_end, 4),
            scene_type="scroll",
            is_scroll=True,
            scroll_direction=scroll_dir,
            active_roi=roi_dict,
            recommended_mode="fit",
            recommended_framing={"mode": "fit"},
            confidence=0.78,
            reason="Vertical scroll spans wider than target crop; fit preserves full lines.",
            caption_clearance_y=0.84,
        )

    if rw <= target_w_norm * 1.18 and rh <= target_h_norm * 1.05:
        half_w = target_w_norm / 2.0
        half_h = target_h_norm / 2.0
        cx = _clamp(rx + rw / 2.0, half_w, 1.0 - half_w) if half_w < 0.5 else 0.5
        cy = _clamp(ry + rh / 2.0, half_h, 1.0 - half_h) if half_h < 0.5 else 0.5
        zoom = 1.0
        if rw < target_w_norm * 0.65:
            zoom = round(_clamp(target_w_norm / max(rw * 1.25, 1e-3), 1.0, 1.6), 2)
        clearance = 0.16 if (ry + rh > 0.72 and zoom >= 1.0) else 0.86
        return ScreenSegmentHint(
            source_start_s=src_start,
            source_end_s=src_end,
            clip_start_s=round(seg_clip_start, 4),
            clip_end_s=round(seg_clip_end, 4),
            scene_type="scroll" if is_scroll else "uncertain",
            is_scroll=is_scroll,
            scroll_direction=scroll_dir,
            active_roi=roi_dict,
            recommended_mode="manual",
            recommended_framing={
                "mode": "manual",
                "center_x": round(cx, 4),
                "center_y": round(cy, 4),
                "zoom": zoom,
            },
            confidence=0.82,
            reason="Localized motion fits inside target crop; content type is unknown.",
            caption_clearance_y=clearance,
        )

    return ScreenSegmentHint(
        source_start_s=src_start,
        source_end_s=src_end,
        clip_start_s=round(seg_clip_start, 4),
        clip_end_s=round(seg_clip_end, 4),
        scene_type="scroll" if is_scroll else "uncertain",
        is_scroll=is_scroll,
        scroll_direction=scroll_dir,
        active_roi=roi_dict,
        recommended_mode="fit",
        recommended_framing={"mode": "fit"},
        confidence=0.76,
        reason=(
            "Active screen ROI is wider than target aspect crop; "
            "using fit blur to prevent clipping."
        ),
        caption_clearance_y=0.84,
    )


def analyze_clip_screens(
    video_path: Path,
    *,
    start_s: float = 0.0,
    end_s: float | None = None,
    ratio: str = "9:16",
    crop_path: CropPath | None = None,
    sample_fps: float = 3.0,
    analysis_width: int = 640,
) -> ScreenAnalysisResult:
    """Run a single-pass CPU analysis for cuts, scroll, ROI, and framing hints."""
    if not video_path.is_file():
        raise InspectionError("Source video file is missing for screen analysis.")
    if sample_fps <= 0 or analysis_width < 160:
        raise InspectionValidationError("Invalid screen analysis sampling parameters.")

    t0 = time.perf_counter()
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise InspectionError(f"Could not open video for screen analysis: {video_path.name}")

    try:
        native_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if native_fps <= 0 or native_fps > 240:
            native_fps = 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        total_dur = (total_frames / native_fps) if total_frames > 0 else 0.0

        clip_end = end_s if end_s is not None else total_dur
        duration_s = max(0.0, clip_end - start_s)
        if duration_s <= 0:
            raise InspectionValidationError("Analysis window duration must be positive.")

        if start_s > 0:
            cap.set(cv2.CAP_PROP_POS_MSEC, start_s * 1000.0)

        step_frames = max(1, int(round(native_fps / sample_fps)))
        eff_fps = native_fps / step_frames

        adaptive = AdaptiveDetector(
            adaptive_threshold=2.6,
            min_scene_len=1,
            min_content_val=12.0,
        )
        hash_det = HashDetector(
            threshold=0.35,
            min_scene_len=1,
        )

        raw_cuts: list[float] = []
        pairs: list[_PairSample] = []
        prev_t_rel: float = 0.0
        prev_gray: np.ndarray | None = None
        prev_win: np.ndarray | None = None
        prev_hist: np.ndarray | None = None
        hann: np.ndarray | None = None

        frame_idx = 0
        sampled_count = 0
        max_frames = int(round(duration_s * native_fps)) + step_frames

        while frame_idx <= max_frames:
            if not cap.grab():
                break
            if frame_idx % step_frames != 0:
                frame_idx += 1
                continue

            ok, frame = cap.retrieve()
            if not ok or frame is None:
                break

            t_rel = frame_idx / native_fps
            if t_rel > duration_s + 0.05:
                break

            fh, fw = frame.shape[:2]
            if src_w <= 0:
                src_w, src_h = fw, fh
            if fw > analysis_width:
                scale = analysis_width / float(fw)
                small = cv2.resize(
                    frame,
                    (analysis_width, max(2, int(round(fh * scale)))),
                    interpolation=cv2.INTER_AREA,
                )
            else:
                small = frame

            tc = FrameTimecode(sampled_count, fps=eff_fps)
            for cut_tc in adaptive.process_frame(tc, small):
                raw_cuts.append(cut_tc.get_seconds())
            for cut_tc in hash_det.process_frame(tc, small):
                raw_cuts.append(cut_tc.get_seconds())

            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            hist = cv2.calcHist([gray], [0], None, [32], [0, 256])
            cv2.normalize(hist, hist)
            if hann is None or hann.shape != gray.shape:
                hann = cv2.createHanningWindow((gray.shape[1], gray.shape[0]), cv2.CV_32F)
            win = cv2.GaussianBlur(gray, (3, 3), 0).astype(np.float32) * hann

            if prev_gray is not None and prev_win is not None and prev_hist is not None:
                diff = cv2.absdiff(prev_gray, gray)
                diff_mask = (diff > _DIFF_THRESHOLD).astype(np.uint8) * 255
                changed_ratio = float(np.count_nonzero(diff_mask)) / float(diff_mask.size)
                hist_corr = float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL))
                (_, dy), response = cv2.phaseCorrelate(prev_win, win)
                # Scroll check: strong coherent vertical translation
                (dx, dy_val) = (_, dy)
                is_scroll = (
                    response >= _SCROLL_MIN_RESPONSE
                    and abs(dy_val) >= _SCROLL_MIN_DY_PX
                    and abs(dx) <= abs(dy_val) * 0.7
                )
                pairs.append(
                    _PairSample(
                        prev_t_rel=prev_t_rel,
                        t_rel=t_rel,
                        diff_mask=diff_mask,
                        changed_ratio=changed_ratio,
                        hist_corr=hist_corr,
                        is_scroll=is_scroll,
                        dy=float(dy_val),
                        response=float(response),
                    )
                )

            prev_t_rel = t_rel
            prev_gray = gray
            prev_win = win
            prev_hist = hist
            sampled_count += 1
            frame_idx += 1

        end_tc = FrameTimecode(max(0, sampled_count), fps=eff_fps)
        for cut_tc in adaptive.post_process(end_tc):
            raw_cuts.append(cut_tc.get_seconds())
    finally:
        cap.release()

    # Suppress false cuts caused by smooth scrolling
    filtered_cuts: list[float] = []
    for cut_t in raw_cuts:
        near = [p for p in pairs if abs(p.t_rel - cut_t) <= 0.7]
        if near:
            scroll_count = sum(1 for p in near if p.is_scroll)
            min_corr = min(p.hist_corr for p in near)
            if scroll_count >= max(1, len(near) // 2) and min_corr > 0.72:
                continue
        filtered_cuts.append(cut_t)

    # Also detect strong structural jump if AdaptiveDetector missed a dark->dark cut
    for p in pairs:
        if not p.is_scroll and p.response < 0.12 and p.hist_corr < 0.65 and p.changed_ratio > 0.20:
            filtered_cuts.append(p.t_rel)

    cuts = _merge_cut_times(filtered_cuts, duration_s)
    boundaries = [0.0, *cuts, duration_s]

    segments: list[ScreenSegmentHint] = []
    for seg_start, seg_end in zip(boundaries, boundaries[1:], strict=False):
        seg_pairs = [
            p
            for p in pairs
            if p.prev_t_rel >= seg_start - 1e-6
            and (
                p.t_rel < seg_end - 0.05
                if seg_end < duration_s - 1e-6
                else p.t_rel <= seg_end + 1e-6
            )
        ]
        if not seg_pairs:
            seg_pairs = [p for p in pairs if seg_start <= p.t_rel <= seg_end]
        hint = _build_segment_hint(
            seg_clip_start=seg_start,
            seg_clip_end=seg_end,
            clip_start_abs=start_s,
            clip_end_abs=clip_end,
            pairs=seg_pairs,
            src_w=src_w,
            src_h=src_h,
            ratio=ratio,
            crop_path=crop_path,
        )
        segments.append(hint)

    # Collapse adjacent segments that share identical framing
    collapsed: list[ScreenSegmentHint] = []
    for seg in segments:
        if collapsed and collapsed[-1].recommended_framing == seg.recommended_framing:
            prev = collapsed[-1]
            collapsed[-1] = ScreenSegmentHint(
                source_start_s=prev.source_start_s,
                source_end_s=seg.source_end_s,
                clip_start_s=prev.clip_start_s,
                clip_end_s=seg.clip_end_s,
                scene_type=prev.scene_type,
                is_scroll=prev.is_scroll or seg.is_scroll,
                scroll_direction=prev.scroll_direction if prev.is_scroll else seg.scroll_direction,
                active_roi=prev.active_roi or seg.active_roi,
                recommended_mode=prev.recommended_mode,
                recommended_framing=prev.recommended_framing,
                confidence=round((prev.confidence + seg.confidence) / 2.0, 3),
                reason=prev.reason,
                caption_clearance_y=(
                    min(prev.caption_clearance_y, seg.caption_clearance_y)
                    if prev.caption_clearance_y is not None and seg.caption_clearance_y is not None
                    else None
                ),
            )
        else:
            collapsed.append(seg)

    if len(collapsed) == 1:
        top_framing = collapsed[0].recommended_framing
    elif len(collapsed) <= _MAX_FRAMING_SLICES:
        top_framing = {
            "mode": "slices",
            "segments": [
                {
                    "start_s": s.source_start_s,
                    "end_s": s.source_end_s,
                    "framing": s.recommended_framing,
                }
                for s in collapsed
            ],
        }
    else:
        top_framing = {"mode": "fit"}

    overall_conf = float(np.mean([s.confidence for s in collapsed])) if collapsed else 0.25
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    notes: list[str] = [
        "Cuts are sampled estimates and may be missed; inspect the surrounding video.",
        "Motion does not identify semantic content. Verify framing and captions visually.",
        "Split caption clearance is unknown in output coordinates; inspect the composed output.",
    ]
    if overall_conf < 0.6:
        notes.append("Low confidence; defaulted uncertain regions to fit blur.")

    return ScreenAnalysisResult(
        source_width=src_w,
        source_height=src_h,
        target_ratio=ratio,
        elapsed_ms=elapsed_ms,
        sampled_frames=sampled_count,
        cuts_detected=[round(start_s + c, 3) for c in cuts],
        segments=collapsed,
        scenes=[
            {
                **segment.to_dict(),
                "short_scene": segment.clip_end_s - segment.clip_start_s < _MIN_SEGMENT_S,
            }
            for segment in segments
        ],
        cut_frame_pairs=[
            {
                "cut_source_s": round(start_s + cut, 4),
                "before_source_s": round(
                    start_s + max((boundaries[i] + cut) / 2, cut - 1 / eff_fps), 4
                ),
                "after_source_s": round(
                    start_s + min((cut + boundaries[i + 2]) / 2, cut + 1 / eff_fps), 4
                ),
            }
            for i, cut in enumerate(cuts)
        ],
        recommended_framing=top_framing,
        overall_confidence=overall_conf,
        notes=notes,
    )
