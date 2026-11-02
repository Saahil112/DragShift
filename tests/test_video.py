"""Tests for video: MOV → GIF and video crop."""
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pytest
from PIL import Image

from dragshift import convert
from dragshift.util import UserError, category


def clip(tmp_path, name="Clip.mov", size=(320, 240), frames=30, rotation=0, audio=False) -> Path:
    """A red square moving over a blue frame; the top-left corner is always green."""
    p = tmp_path / name
    w, h = size
    with av.open(str(p), "w") as c:
        v = c.add_stream("libx264", rate=30)
        v.width, v.height, v.pix_fmt = w, h, "yuv420p"
        if rotation:
            v.set_display_rotation(rotation)
        a = c.add_stream("aac", rate=44100) if audio else None
        for i in range(frames):
            arr = np.zeros((h, w, 3), np.uint8)
            arr[:] = (0, 0, 255)
            arr[:40, :40] = (0, 255, 0)
            x = 40 + i * 6
            arr[100:140, x:x + 40] = (255, 0, 0)
            c.mux(v.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")))
        c.mux(v.encode())
        if a:
            samples = 1024
            for i in range(44):  # ~1 s of a quiet tone
                t = (np.arange(samples) + i * samples) / 44100
                tone = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)[None, :]
                af = av.AudioFrame.from_ndarray(tone, format="fltp", layout="mono")
                af.sample_rate, af.pts, af.time_base = 44100, i * samples, Fraction(1, 44100)
                c.mux(a.encode(af))
            c.mux(a.encode())
    return p


def first_frame(path) -> np.ndarray:
    return next(convert.video_frames(path))[1]


def test_video_category_and_targets():
    assert category("a.MOV") == "video" and category("b.mp4") == "video"
    assert convert.targets_for("video", ".mov") == ["GIF"]


def test_mov_to_gif(tmp_path):
    out = convert.convert(clip(tmp_path), "video", "GIF")[0]
    assert out.name == "Clip.gif"
    with Image.open(out) as im:
        assert im.size == (320, 240)
        assert im.n_frames == 15  # 30 frames at 30 fps, thinned to 15 fps
        assert im.info["loop"] == 0
        assert 60 <= im.info["duration"] <= 70
        im.seek(5)
        assert im.convert("RGB").getpixel((10, 10))[1] > 200  # green corner survives the palette


def test_gif_is_scaled_down(tmp_path):
    with Image.open(convert.video_to_gif(clip(tmp_path, size=(1280, 720), frames=4))[0]) as im:
        assert im.size == (640, 360)


def test_crop_video_keeps_audio(tmp_path):
    src = clip(tmp_path, audio=True)
    out = convert.crop_video(src, (0, 0, 161, 121))  # odd sizes round down to even
    assert out.name == "Clip Cropped.mov"
    with av.open(str(out)) as c:
        v = c.streams.video[0]
        assert (v.codec_context.width, v.codec_context.height) == (160, 120)
        assert c.streams.audio
        n = sum(1 for _ in c.decode(v))
    assert n == 30
    arr = first_frame(out)
    assert arr[10, 10, 1] > 200 and arr[100, 150, 2] > 200  # green corner, blue background


def test_crop_portrait_phone_video(tmp_path):
    # stored landscape with a "turn 90° clockwise" flag, like an iPhone portrait clip
    src = clip(tmp_path, rotation=-90)
    pv, full = convert.video_preview(src)
    assert full == (240, 320) and pv.size == (240, 320)
    up = first_frame(src)
    assert up[10, 230, 1] > 200  # green corner is top-right once upright
    out = convert.crop_video(src, (140, 0, 240, 100))
    arr = first_frame(out)
    assert arr.shape[:2] == (100, 100)
    assert arr[10, 90, 1] > 200


def test_crop_with_extra_rotation(tmp_path):
    src = clip(tmp_path)
    out = convert.crop_video(src, (0, 0, 240, 320), rot=90)  # turned clockwise: portrait
    arr = first_frame(out)
    assert arr.shape[:2] == (320, 240)
    assert arr[10, 230, 1] > 200


def test_bad_video(tmp_path):
    p = tmp_path / "broken.mov"
    p.write_bytes(b"not a video")
    with pytest.raises(UserError):
        convert.video_to_gif(p)
