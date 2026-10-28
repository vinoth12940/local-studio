"""Attachments: images and documents processed once on upload.

Moved from app.py in the refactor's P1 split.
"""

import base64
import json
import re
from pathlib import Path

from fastapi import HTTPException

from studio.config import UPLOADS


# ---------------------------------------------------------------- attachments
# Files are processed once on upload: images are downscaled, documents become plain text
# (scanned PDFs become page images). Chats store only attachment ids.

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml",
            ".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx", ".py", ".rb", ".go", ".rs", ".java",
            ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".sh", ".zsh", ".sql", ".toml",
            ".ini", ".cfg", ".log", ".r", ".m", ".scala", ".lua", ".pl", ".dart", ".vue", ".svelte"}
MAX_DOC_CHARS = 80_000            # ~20k tokens: leaves room in the 32k context for the reply
MAX_IMAGE_SIDE = 1536
MAX_SCAN_PAGES = 4
_AID = re.compile(r"^[a-f0-9]{16}$")


def _save_image(img, path):
    from PIL import Image
    img = img.convert("RGB")
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.LANCZOS)
    img.save(path, "JPEG", quality=90)
    return img.size


def _extract(path: Path, ext: str, aid: str) -> dict:
    """Return {kind, text?, images?, pages?} for an uploaded file."""
    if ext in IMAGE_EXT:
        from PIL import Image
        if ext == ".heic":
            raise HTTPException(415, "HEIC isn't supported yet; export the photo as JPEG or PNG.")
        w, h = _save_image(Image.open(path), UPLOADS / f"{aid}-0.jpg")
        return {"kind": "image", "images": [f"{aid}-0.jpg"], "width": w, "height": h}
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
        text_chars = sum(len(t) for t in pages)
        if text_chars < 80 * max(1, len(pages)) and len(pages) and text_chars < 2000:
            # scanned / image-only PDF: hand the first pages to a vision engine
            import pypdfium2 as pdfium
            doc = pdfium.PdfDocument(str(path))
            imgs = []
            for i in range(min(MAX_SCAN_PAGES, len(doc))):
                name = f"{aid}-{i}.jpg"
                _save_image(doc[i].render(scale=2).to_pil(), UPLOADS / name)
                imgs.append(name)
            return {"kind": "image", "images": imgs, "pages": len(pages), "scanned": True}
        text = "\n\n".join(f"--- Page {i + 1} ---\n{t}" for i, t in enumerate(pages) if t)
        return {"kind": "document", "text": text, "pages": len(pages)}
    if ext == ".docx":
        import docx
        d = docx.Document(str(path))
        parts = []
        for p in d.paragraphs:
            if p.text.strip():
                style = (p.style.name or "").lower() if p.style is not None else ""
                parts.append(("#" * int(style[-1]) + " " if style.startswith("heading") and style[-1].isdigit() else "") + p.text)
        for t in d.tables:
            rows = [" | ".join(c.text.strip() for c in r.cells) for r in t.rows]
            parts.append("\n".join(rows))
        return {"kind": "document", "text": "\n\n".join(parts)}
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                if any(v is not None for v in row):
                    rows.append(" | ".join("" if v is None else str(v) for v in row))
                if len(rows) >= 2000:
                    rows.append("… (more rows not shown)")
                    break
            parts.append(f"## Sheet: {ws.title}\n" + "\n".join(rows))
        return {"kind": "document", "text": "\n\n".join(parts), "sheets": len(wb.worksheets)}
    raw = path.read_bytes()
    if ext in TEXT_EXT or b"\x00" not in raw[:4096]:
        return {"kind": "document", "text": raw.decode("utf-8", errors="replace")}
    raise HTTPException(415, f"Unsupported file type: {ext or 'unknown'}")


def _attachment_meta(aid):
    if not isinstance(aid, str) or not _AID.match(aid):
        return None
    p = UPLOADS / f"{aid}.json"
    return json.loads(p.read_text()) if p.exists() else None


def build_messages(messages):
    """Expand attachment ids into model input. Documents become <document> blocks in the
    text; images become image_url parts (which also means a vision engine is needed)."""
    out, needs_vision = [], False
    for m in messages:
        atts = [a for a in (m.get("attachments") or [])]
        msg = {k: v for k, v in m.items() if k != "attachments"}
        if not atts:
            out.append(msg)
            continue
        docs, images = [], []
        for aid in atts:
            meta = _attachment_meta(aid)
            if meta is None:
                docs.append("[An attached file is no longer available.]")
                continue
            if meta["kind"] == "document":
                txt = (UPLOADS / f"{aid}.txt")
                body = txt.read_text() if txt.exists() else "[file content missing]"
                note = " (truncated)" if meta.get("truncated") else ""
                docs.append(f'<document name="{meta["name"]}"{note}>\n{body}\n</document>')
            else:
                for name in meta.get("images", []):
                    p = UPLOADS / name
                    if p.exists():
                        images.append("data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode())
                if meta.get("scanned"):
                    docs.append(f'[Scanned PDF "{meta["name"]}": first {len(meta.get("images", []))} '
                                f'of {meta.get("pages")} pages attached as images.]')
        text = "\n\n".join(docs + [msg.get("content") or ""]).strip()
        if images:
            needs_vision = True
            msg["content"] = [{"type": "image_url", "image_url": {"url": u}} for u in images] + \
                             [{"type": "text", "text": text or "Describe this image."}]
        else:
            msg["content"] = text
        out.append(msg)
    return out, needs_vision
