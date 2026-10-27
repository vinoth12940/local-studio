"""Upload type from the file's own bytes (flag security.upload_sniff, off by default; refactor P3).

When on, an upload whose name says image, PDF or Office document must start with that format's magic bytes, and a text upload
must not be binary, or it is refused with 415 instead of being parsed by the wrong reader.
"""

_IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}
_OFFICE = {".docx", ".xlsx", ".xlsm"}


def sniff(data: bytes):
    """The format the bytes are, by magic number: png, jpeg, gif, webp, bmp, tiff, heic, pdf, zip, or None."""
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc", b"heim", b"heis"):
        return "heic"
    if data.lstrip()[:5] == b"%PDF-":
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    return None


_EXPECT = {".png": {"png"}, ".jpg": {"jpeg"}, ".jpeg": {"jpeg"}, ".gif": {"gif"}, ".webp": {"webp"}, ".bmp": {"bmp"},
           ".tif": {"tiff"}, ".tiff": {"tiff"}, ".heic": {"heic"}, ".pdf": {"pdf"}, ".docx": {"zip"}, ".xlsx": {"zip"}, ".xlsm": {"zip"}}


def mismatch(data: bytes, ext: str):
    """A reason to refuse the upload, or None when its bytes match what its name says."""
    ext = ext.lower()
    if ext in _EXPECT:
        kind = sniff(data)
        if kind not in _EXPECT[ext]:
            return f"The file's content is {kind or 'not a known format'}, not {ext[1:].upper()}."
        return None
    if b"\x00" in data[:8192]:
        return "The file looks binary, not text."
    return None
