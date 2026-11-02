"""Format conversions. Every function takes a source path and returns a list of
output paths written next to the source."""
from __future__ import annotations

import html
import io
import sys
from pathlib import Path

from PIL import Image, ImageOps

from .util import UserError, sibling

try:  # HEIC / HEIF support
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_OK = True
except Exception:  # pragma: no cover
    HEIF_OK = False

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

IMAGE_TARGETS = ["PNG", "JPG", "WEBP", "HEIC", "TIFF", "AVIF", "BMP", "PDF", "ICO", "GIF"]
PDF_TARGETS = ["JPG", "PNG", "DOCX", "TXT"]
DOCX_TARGETS = ["PDF", "TXT"]
TEXT_TARGETS = ["PDF", "DOCX"]
VIDEO_TARGETS = ["GIF"]

PIL_FORMAT = {"PNG": "PNG", "JPG": "JPEG", "WEBP": "WEBP", "HEIC": "HEIF", "TIFF": "TIFF",
              "AVIF": "AVIF", "BMP": "BMP", "PDF": "PDF", "ICO": "ICO", "GIF": "GIF"}
NO_ALPHA = {"JPG", "PDF", "BMP"}


# ---------------------------------------------------------------- images
def open_image(path: str | Path) -> Image.Image:
    im = Image.open(path)
    im.seek(0)
    im = ImageOps.exif_transpose(im)  # honour camera rotation
    if im.mode in ("P", "LA", "PA"):
        im = im.convert("RGBA")
    elif im.mode not in ("RGB", "RGBA", "L"):
        im = im.convert("RGB")
    return im


def open_preview(path: str | Path, max_side: int = 1400) -> Image.Image:
    """Fast, reduced-size decode for on-screen previews (JPEG decodes at 1/2, 1/4, 1/8 scale)."""
    im = Image.open(path)
    im.seek(0)
    if im.format == "JPEG":
        im.draft("RGB", (max_side, max_side))
    im = ImageOps.exif_transpose(im)
    if im.mode in ("P", "LA", "PA"):
        im = im.convert("RGBA")
    elif im.mode not in ("RGB", "RGBA", "L"):
        im = im.convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def open_pdf(path: str | Path):
    doc = fitz.open(path)
    if doc.needs_pass:
        doc.close()
        raise UserError("This PDF is password-protected. Remove the password first.")
    return doc


def flatten(im: Image.Image, bg=(255, 255, 255)) -> Image.Image:
    if im.mode == "RGBA":
        base = Image.new("RGB", im.size, bg)
        base.paste(im, mask=im.split()[3])
        return base
    return im.convert("RGB") if im.mode != "RGB" else im


def save_image(im: Image.Image, out: Path, target: str, quality: int | None = None, exif=None) -> None:
    from .util import CONFIG
    quality = quality or int(CONFIG.get("jpg_quality", 90))
    target = target.upper()
    fmt = PIL_FORMAT[target]
    if target in NO_ALPHA:
        im = flatten(im)
    kw: dict = {}
    if target in ("JPG", "WEBP", "AVIF", "HEIC"):
        kw["quality"] = quality
    if target == "JPG":
        kw.update(optimize=True, progressive=True)
    if target == "PNG":
        kw["optimize"] = True
    if target == "ICO":
        side = min(256, max(im.size))
        im = ImageOps.contain(im.convert("RGBA"), (side, side))
        kw["sizes"] = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256) if s <= side]
    if target == "PDF":
        kw["resolution"] = 150.0
    if exif and target in ("JPG", "WEBP", "PNG", "TIFF"):
        kw["exif"] = exif
    atomic_save(lambda dst: im.save(dst, fmt, **kw), out)


def atomic_save(write, out):
    """Write to a hidden temp file, then rename: Explorer never shows a half-written file."""
    import os
    if not isinstance(out, (str, Path)):
        write(out)  # in-memory buffer
        return
    out = Path(out)
    tmp = out.with_name(f".~{out.name}.part")
    try:
        write(tmp)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def image_to(src: str | Path, target: str) -> list[Path]:
    target = target.upper()
    im = open_image(src)
    out = sibling(src, ext="." + target.lower())
    save_image(im, out, target)
    return [out]


def images_to_single_pdf(srcs: list[str | Path]) -> list[Path]:
    pages = [flatten(open_image(s)) for s in srcs]
    out = sibling(srcs[0], "Combined", ".pdf")
    pages[0].save(out, "PDF", resolution=150.0, save_all=True, append_images=pages[1:])
    return [out]


# ---------------------------------------------------------------- PDF
def pdf_to_images(src: str | Path, target: str, dpi: int | None = None) -> list[Path]:
    from .util import CONFIG
    dpi = dpi or int(CONFIG.get("pdf_dpi", 200))
    target = target.upper()
    outs = []
    with open_pdf(src) as doc:
        n = doc.page_count
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            label = f"page {i + 1}" if n > 1 else ""
            out = sibling(src, label, "." + target.lower())
            save_image(im, out, target)
            outs.append(out)
    return outs


def pdf_to_txt(src: str | Path) -> list[Path]:
    with open_pdf(src) as doc:
        text = "\n\f\n".join(page.get_text("text") for page in doc)
    out = sibling(src, ext=".txt")
    out.write_text(text, encoding="utf-8")
    return [out]


def pdf_to_docx(src: str | Path) -> list[Path]:
    """Text blocks become paragraphs, embedded images are kept, one Word page per PDF page."""
    from docx import Document
    from docx.enum.text import WD_BREAK
    from docx.shared import Inches

    d = Document()
    with open_pdf(src) as doc:
        for pno, page in enumerate(doc):
            if pno:
                d.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
            items = []
            for b in page.get_text("dict")["blocks"]:
                y = b["bbox"][1]
                if b.get("type") == 0:
                    lines = []
                    size = 11.0
                    bold = False
                    for ln in b["lines"]:
                        spans = ln["spans"]
                        if spans:
                            size = max(size, max(s["size"] for s in spans)) if not lines else size
                            bold = bold or any("Bold" in s["font"] for s in spans)
                        lines.append("".join(s["text"] for s in spans))
                    txt = " ".join(l.strip() for l in lines).strip()
                    if txt:
                        items.append((y, "text", (txt, size, bold)))
                elif b.get("type") == 1 and b.get("image"):
                    items.append((y, "img", (b["image"], b["bbox"])))
            items.sort(key=lambda t: t[0])
            for _, kind, data in items:
                if kind == "text":
                    txt, size, bold = data
                    if size >= 16:
                        d.add_heading(txt, level=1 if size >= 20 else 2)
                    else:
                        p = d.add_paragraph()
                        r = p.add_run(txt)
                        r.bold = bold and len(txt) < 120
                else:
                    raw, bbox = data
                    width_in = min(6.5, max(0.5, (bbox[2] - bbox[0]) / 72))
                    try:
                        d.add_picture(io.BytesIO(raw), width=Inches(width_in))
                    except Exception:
                        pass
    out = sibling(src, ext=".docx")
    d.save(out)
    return [out]


# ---------------------------------------------------------------- DOCX
def _docx_paragraph_html(p) -> str:
    parts = []
    for r in p.runs:
        t = html.escape(r.text)
        if not t:
            continue
        if r.bold:
            t = f"<b>{t}</b>"
        if r.italic:
            t = f"<i>{t}</i>"
        if r.underline:
            t = f"<u>{t}</u>"
        parts.append(t)
    body = "".join(parts) or "&nbsp;"
    style = (p.style.name or "").lower() if p.style is not None else ""
    if style.startswith("heading"):
        lvl = "".join(c for c in style if c.isdigit()) or "1"
        lvl = str(min(int(lvl), 6))
        return f"<h{lvl}>{body}</h{lvl}>"
    if style == "title":
        return f"<h1>{body}</h1>"
    if "list" in style:
        return f"<p>&bull; {body}</p>"
    return f"<p>{body}</p>"


def docx_to_html(src: str | Path) -> str:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = Document(src)
    out = []
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            out.append(_docx_paragraph_html(Paragraph(child, d)))
        elif tag == "tbl":
            t = Table(child, d)
            rows = []
            for row in t.rows:
                cells = "".join(f"<td>{html.escape(c.text)}</td>" for c in row.cells)
                rows.append(f"<tr>{cells}</tr>")
            out.append("<table>" + "".join(rows) + "</table>")
    return "\n".join(out)


def docx_to_txt(src: str | Path) -> list[Path]:
    from docx import Document
    d = Document(src)
    lines = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            lines.append("\t".join(c.text for c in row.cells))
    out = sibling(src, ext=".txt")
    out.write_text("\n".join(lines), encoding="utf-8")
    return [out]


def _word_to_pdf(src: Path, out: Path) -> bool:
    """Use Microsoft Word (if installed) for a perfect-fidelity export."""
    if sys.platform != "win32":
        return False
    try:
        import win32com.client  # type: ignore
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        try:
            doc = word.Documents.Open(str(src.resolve()), ReadOnly=True)
            doc.SaveAs(str(out.resolve()), FileFormat=17)  # wdFormatPDF
            doc.Close(False)
        finally:
            word.Quit()
        return out.exists()
    except Exception:
        return False


CSS = """
* { font-family: sans-serif; }
body { font-size: 11pt; line-height: 1.35; }
h1 { font-size: 20pt; } h2 { font-size: 16pt; } h3 { font-size: 13pt; }
p { margin: 0 0 6pt 0; }
pre { font-family: monospace; font-size: 9.5pt; white-space: pre-wrap; }
table { border-collapse: collapse; margin: 6pt 0; }
td { border: 1px solid #888; padding: 3pt 5pt; font-size: 10pt; }
"""


def html_to_pdf(body_html: str, out: Path) -> None:
    story = fitz.Story(html=body_html, user_css=CSS)
    writer = fitz.DocumentWriter(str(out))
    mediabox = fitz.paper_rect("letter")
    where = mediabox + (54, 54, -54, -54)
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()


def docx_to_pdf(src: str | Path) -> list[Path]:
    src = Path(src)
    out = sibling(src, ext=".pdf")
    if not _word_to_pdf(src, out):
        html_to_pdf(docx_to_html(src), out)
    return [out]


# ---------------------------------------------------------------- text
def read_text(src: str | Path) -> str:
    raw = Path(src).read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def text_to_pdf(src: str | Path) -> list[Path]:
    out = sibling(src, ext=".pdf")
    html_to_pdf(f"<pre>{html.escape(read_text(src))}</pre>", out)
    return [out]


def text_to_docx(src: str | Path) -> list[Path]:
    from docx import Document
    d = Document()
    for line in read_text(src).splitlines():
        d.add_paragraph(line)
    out = sibling(src, ext=".docx")
    d.save(out)
    return [out]


# ---------------------------------------------------------------- video
GIF_FPS = 15
GIF_MAX_SIDE = 640
GIF_MAX_FRAMES = 300  # long clips get a lower frame rate instead of a gigantic GIF


def open_video(path: str | Path):
    import av
    try:
        c = av.open(str(path))
    except av.FFmpegError:
        raise UserError("This video can't be read. It may be damaged or use an unusual codec.")
    if not c.streams.video:
        c.close()
        raise UserError("This file has no video track.")
    c.streams.video[0].thread_type = "AUTO"
    return c


def _turns(frame, rot: int = 0) -> int:
    """Quarter turns (counter-clockwise, for np.rot90) that show `frame` upright, then turn it
    `rot`° clockwise. Phones store portrait clips sideways plus a rotation flag."""
    return (round((frame.rotation or 0) / 90) - rot // 90) % 4


def video_frames(path: str | Path, fps: float | None = None):
    """Yield (seconds, upright RGB array), thinned to at most `fps` frames a second."""
    import numpy as np
    with open_video(path) as c:
        nxt = 0.0
        for f in c.decode(c.streams.video[0]):
            t = f.time if f.time is not None else nxt
            if fps:
                if t + 1e-6 < nxt:
                    continue
                while nxt <= t + 1e-6:
                    nxt += 1 / fps
            yield t, np.rot90(f.to_ndarray(format="rgb24"), _turns(f))


def video_preview(path: str | Path, max_side: int = 1400) -> tuple[Image.Image, tuple[int, int]]:
    """First frame (reduced for on-screen use) and the video's upright full size."""
    for _, arr in video_frames(path):
        im = Image.fromarray(arr)
        full = im.size
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        return im, full
    raise UserError("This video has no frames.")


def video_to_gif(src: str | Path) -> list[Path]:
    with open_video(src) as c:
        secs = (c.duration or 0) / 1_000_000
    fps = min(GIF_FPS, GIF_MAX_FRAMES / secs) if secs else GIF_FPS

    def frames():
        for i, (t, arr) in enumerate(video_frames(src, fps)):
            if i >= GIF_MAX_FRAMES:
                return
            im = Image.fromarray(arr)
            im.thumbnail((GIF_MAX_SIDE, GIF_MAX_SIDE), Image.LANCZOS)
            yield t, im

    # pass 1: one palette for the whole clip (per-frame palettes flicker)
    samples = [im for i, (_, im) in enumerate(frames()) if i % 10 == 0]
    if not samples:
        raise UserError("This video has no frames.")
    w, h = samples[0].size
    sheet = Image.new("RGB", (w, h * len(samples)))
    for i, im in enumerate(samples):
        sheet.paste(im, (0, h * i))
    pal = sheet.quantize(256, method=Image.Quantize.MEDIANCUT)
    # pass 2: map every frame onto it; frame times come from the video, so slow clips stay slow
    times, out_frames = [], []
    for t, im in frames():
        times.append(t)
        out_frames.append(im.quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG))
    durs = [max(20, round((b - a) * 1000)) for a, b in zip(times, times[1:])] + [round(1000 / fps)]
    out = sibling(src, ext=".gif")
    atomic_save(lambda dst: out_frames[0].save(dst, "GIF", save_all=True, append_images=out_frames[1:],
                                               duration=durs, loop=0), out)
    return [out]


def crop_video(src: str | Path, box: tuple[int, int, int, int], rot: int = 0) -> Path:
    """Crop to `box` (upright pixels, after turning `rot`° clockwise). Sound is copied as-is."""
    import av
    import numpy as np
    x0, y0, x1, y1 = box
    w, h = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2  # H.264 needs even sizes
    ext = ".mp4" if Path(src).suffix.lower() == ".mp4" else ".mov"
    codec = next(c for c in ("libx264", "h264_videotoolbox", "h264") if c in av.codecs_available)

    def write(dst):
        with open_video(src) as inp, av.open(str(dst), "w", format=ext[1:]) as out:
            vin = inp.streams.video[0]
            ain = inp.streams.audio[0] if inp.streams.audio else None
            vout = out.add_stream(codec, rate=vin.average_rate or 30)
            vout.width, vout.height, vout.pix_fmt = w, h, "yuv420p"
            # keep the source clock: screen recordings are variable-rate, and rounding their
            # timestamps to the average rate gives two frames the same time
            vout.time_base = vout.codec_context.time_base = vin.time_base
            if codec == "libx264":
                vout.options = {"crf": "18", "preset": "medium"}
            aout = out.add_stream_from_template(ain) if ain else None
            for pkt in inp.demux([vin] + ([ain] if ain else [])):
                if pkt.stream is ain:
                    if pkt.dts is not None:
                        pkt.stream = aout
                        out.mux(pkt)
                    continue
                for f in pkt.decode():
                    arr = np.rot90(f.to_ndarray(format="rgb24"), _turns(f, rot))[y0:y0 + h, x0:x0 + w]
                    nf = av.VideoFrame.from_ndarray(np.ascontiguousarray(arr), format="rgb24")
                    nf.pts, nf.time_base = f.pts, f.time_base
                    out.mux(vout.encode(nf))
            out.mux(vout.encode())

    out = sibling(src, "Cropped", ext)
    atomic_save(write, out)
    return out


# ---------------------------------------------------------------- dispatch
def targets_for(cat: str, src_ext: str) -> list[str]:
    src_ext = src_ext.lower().lstrip(".")
    same = {"jpeg": "JPG", "jpg": "JPG", "tif": "TIFF", "tiff": "TIFF", "heif": "HEIC"}
    cur = same.get(src_ext, src_ext.upper())
    if cat == "image":
        t = [x for x in IMAGE_TARGETS if x != cur]
        if not HEIF_OK:
            t = [x for x in t if x != "HEIC"]
        return t
    return {"pdf": PDF_TARGETS, "docx": DOCX_TARGETS, "text": TEXT_TARGETS,
            "video": VIDEO_TARGETS}.get(cat, [])


def convert(src: str | Path, cat: str, target: str) -> list[Path]:
    target = target.upper()
    if cat == "image":
        return image_to(src, target)
    if cat == "pdf":
        if target in ("JPG", "PNG"):
            return pdf_to_images(src, target)
        if target == "TXT":
            return pdf_to_txt(src)
        if target == "DOCX":
            return pdf_to_docx(src)
    if cat == "docx":
        return docx_to_pdf(src) if target == "PDF" else docx_to_txt(src)
    if cat == "text":
        return text_to_pdf(src) if target == "PDF" else text_to_docx(src)
    if cat == "video" and target == "GIF":
        return video_to_gif(src)
    raise ValueError(f"Can't convert {cat} to {target}")
