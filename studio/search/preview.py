"""Search service: preview. Moved verbatim from search.py (P6)."""
import re
from pathlib import Path

from studio.search.extractors import PDFIUM, _load_image

# ---------------------------------------------------------------- preview
# Shows a result at the place that matched: the PDF page with the passage marked, the passage in
# context for other documents, the picture, or the audio from the matching moment.

PREVIEW_WIDTH = 1100              # pixels
NEEDLE_CHARS = 200                # the longest line searched for whole
NEEDLE_START = 60                 # if the whole line isn't found (the page's text can differ slightly), look for its start


def _needles(text, limit=24):
    """What to look for on a PDF page to find where a passage sits: each of its lines."""
    out = []
    for line in text.split("\n"):
        line = " ".join(line.split())
        if len(line) >= 8:
            out.append(line[:NEEDLE_CHARS])
        if len(out) == limit:
            break
    return out


def preview_info(store, file_id, chunk_id=None):
    """What the UI needs to preview a result. Without a passage (a match on the file name) it
    describes the beginning of the file."""
    f = store.file_info([file_id]).get(file_id)
    if f is None:
        return None
    c = store.context(chunk_id) if chunk_id else None
    if c is None or c["file_id"] != file_id:              # an id from another file must not bring that file's text
        chunk_id = store.first_chunk(file_id)
        c = store.context(chunk_id) if chunk_id else None
    out = {"file_id": file_id, "name": f["name"], "path": f["path"], "kind": f["kind"], "type": "none",
           "chunk_id": chunk_id, "locator": c["locator"] if c else ""}
    if f["kind"] == "image":
        out["type"] = "image"
    elif f["kind"] in ("audio", "video"):
        m = re.fullmatch(r"(\d+)s", out["locator"])
        out.update(type=f["kind"], start=int(m.group(1)) if m else 0)
    elif f["kind"] == "document" and f["name"].lower().endswith(".pdf"):
        m = re.fullmatch(r"p(\d+)", out["locator"])
        out.update(type="page", page=int(m.group(1)) if m else 1, match=c["match"] if c else "")
    elif c and c["kind"] == "text":
        out.update(type="text", before=c["before"], match=c["match"], after=c["after"])
    return out


def render_page(path, page, needles=()):
    """One PDF page as a JPEG with the lines in `needles` marked. Returns (bytes, pages in the file,
    lines found). A passage that can't be located still gets its page, unmarked."""
    import io
    import pypdfium2 as pdfium
    from PIL import Image, ImageDraw
    with PDFIUM:
        doc = pdfium.PdfDocument(str(path))
        total = len(doc)
        pg = doc[max(1, min(page, total)) - 1]
        width, height = pg.get_size()
        scale = min(PREVIEW_WIDTH / width, 2.5)
        im = pg.render(scale=scale).to_pil().convert("RGB")
        rects = []
        if needles:
            tp = pg.get_textpage()
            for needle in needles:
                hit = tp.search(needle, match_case=False).get_next() or \
                    (len(needle) > NEEDLE_START and tp.search(needle[:NEEDLE_START], match_case=False).get_next())
                if hit:
                    rects += [tp.get_rect(i) for i in range(tp.count_rects(*hit))]
    if rects:
        overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        for left, bottom, right, top in rects:            # PDF points, origin bottom-left
            draw.rectangle([left * scale - 2, (height - top) * scale - 2, right * scale + 2, (height - bottom) * scale + 2],
                           fill=(255, 214, 0, 96))
        im = Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue(), total, len(rects)


def large_image(path, side=1600):
    import io
    im = _load_image(Path(path), side)
    if im is None:
        return None
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue()
