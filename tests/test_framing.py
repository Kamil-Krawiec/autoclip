from dataclasses import replace

import pytest
from autoclip.db.models import Clip, ClipEdit, Job, Source
from autoclip.pipeline.reframe.croppath import centre_crop


@pytest.mark.parametrize("ratio", ["9:16", "1:1", "16:9"])
def test_effective_framing_preserves_only_matching_cache(ratio):
    from autoclip.pipeline.framing import resolve_crop_path

    source = Source(id="s", type="upload", path="unused", width=1920, height=1080)
    clip = Clip(id="c", job_id="j", start_s=10, end_s=15)
    job = Job(id="j", source_id="s", settings={"export": {"ratio": ratio}})
    edit = ClipEdit(clip_id="c", ratio=ratio)
    w, h = map(int, ratio.split(":"))
    cached = centre_crop(1920, 1080, 5, aspect_w=w, aspect_h=h)
    assert resolve_crop_path(source, clip, job, edit, cached) is cached
    for changed_clip, changed_source, changed_cache in [
        (replace(clip, user_trimmed=True), source, cached),
        (clip, replace(source, width=1280), cached),
        (replace(clip, end_s=16), source, cached),
        (clip, source, None),
    ]:
        result = resolve_crop_path(changed_source, changed_clip, job, edit, changed_cache)
        assert result is not cached and result.duration_s == changed_clip.duration_s
    other = replace(edit, ratio="1:1" if ratio != "1:1" else "16:9")
    assert resolve_crop_path(source, clip, job, other, cached) is not cached
    fit = resolve_crop_path(source, clip, job, replace(edit, framing={"mode": "fit"}), cached)
    assert fit.duration_s == 5 and fit.segments[0].fit
    assert (fit.segments[0].width, fit.segments[0].height) == (1920, 1080)
