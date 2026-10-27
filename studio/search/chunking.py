"""Search service: chunking. Moved verbatim from search.py (P6)."""
import re



# ---------------------------------------------------------------- chunking

CHUNK_CHARS = 1200                # ~300 tokens
MAX_CHUNKS = 200                  # per file
_SENT = re.compile(r"(?<=[.!?])\s+")


def chunk_text(text, limit=CHUNK_CHARS):
    """Pack paragraphs into chunks of at most `limit` characters. A paragraph longer than the limit
    is split at sentence ends, and a sentence longer than the limit is cut hard."""
    pieces = []                                           # (text, separator that joins it to the previous piece)
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= limit:
            pieces.append((para, "\n\n"))
            continue
        sep = "\n\n"
        for sent in _SENT.split(para):
            while len(sent) > limit:
                pieces.append((sent[:limit], sep))
                sent, sep = sent[limit:], " "
            if sent:
                pieces.append((sent, sep))
                sep = " "
    chunks, cur = [], ""
    for piece, sep in pieces:
        if cur and len(cur) + len(sep) + len(piece) > limit:
            chunks.append(cur)
            cur = piece
        else:
            cur = cur + sep + piece if cur else piece
    if cur:
        chunks.append(cur)
    return chunks
