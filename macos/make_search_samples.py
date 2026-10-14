"""Builds a small mixed folder (documents, photos, speech) in <out>/files and <out>/queries.json for
`search.py --eval`. The output is generated on this Mac from system files and is never committed.

    uv run -q --no-project --python 3.12 --with-requirements search-requirements.txt python macos/make_search_samples.py build/search_samples
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

TEXTS = {
    "invoice_acme.txt": "Invoice 2291 from Acme Plumbing. Replaced the kitchen sink trap and a leaking shut-off valve. Labour 2 hours at 95 dollars, parts 48 dollars. Total due 238 dollars by 30 November. Pay by bank transfer.",
    "lasagna.txt": "Grandma's lasagna: brown 500 g of beef mince with onion and garlic, add crushed tomatoes and simmer 30 minutes. Layer with pasta sheets, bechamel and mozzarella. Bake at 180 C for 45 minutes until golden.",
    "standup_notes.txt": "Team sync 12 Oct. Priya will finish the login redesign by Friday. The payments bug is blocked on the bank sandbox. We agreed to move the launch to the first week of December.",
    "tokyo_trip.txt": "Japan itinerary: land at Haneda on 3 April, four nights in Shinjuku, day trip to Nikko, then the bullet train to Kyoto for the cherry blossoms. Return flight leaves Osaka on 12 April.",
    "blood_test.txt": "Lab results: fasting glucose 5.4 mmol/L, total cholesterol 6.1 mmol/L (slightly high), vitamin D 42 nmol/L (low). Doctor recommends a vitamin D supplement and a repeat lipid panel in six months.",
    "lease.txt": "Residential tenancy agreement for Unit 4, 18 Harbour Street. Rent is 2,450 dollars per month, due on the first. Bond of four weeks' rent. The lease runs 12 months from 1 February and pets need written consent.",
    "debounce.py": 'import time\ndef debounce(wait):\n    """Decorator: ignore calls that arrive less than wait seconds after the previous one."""\n    def deco(fn):\n        last = [0.0]\n        def inner(*a, **k):\n            now = time.monotonic()\n            if now - last[0] >= wait:\n                last[0] = now\n                return fn(*a, **k)\n        return inner\n    return deco\n',
    "car_insurance.txt": "Policy renewal notice: comprehensive cover for the 2021 Mazda CX-5 renews on 14 January. Annual premium 1,180 dollars, excess 750 dollars. Windscreen cover and a hire car after theft are included.",
}
SPEECH = {
    "voice_memo_1.aiff": "Reminder to self. Call the dentist tomorrow morning and move my appointment to next Thursday afternoon.",
    "voice_memo_2.m4a": "Idea for the garden. Plant tomatoes and basil along the back fence, and build a raised bed for the strawberries.",
    "voice_memo_3.aiff": "The quarterly sales numbers are up twelve percent, mostly from the new subscription plan in Europe.",
}
WALLPAPERS = Path("/System/Library/Desktop Pictures/.thumbnails")
PHOTOS = {"IMG_2001.jpg": "Big Sur Night Succulents.heic", "IMG_2002.jpg": "Big Sur Road.heic",
          "IMG_2003.heic": "Catalina Sunset.heic", "IMG_2004.jpg": "Big Sur Waters Edge.heic"}
# The first 13 were verified in the feasibility test (13 Oct 2026). The last 4 cover file types that test did not.
QUERIES = [
    ("how much do I owe the plumber", "invoice_acme.txt"), ("when does my lease start and what is the rent", "lease.txt"),
    ("recipe with mince and cheese", "lasagna.txt"), ("when are we launching", "standup_notes.txt"),
    ("my cholesterol results", "blood_test.txt"), ("function that rate limits repeated calls", "debounce.py"),
    ("flights to Japan in spring", "tokyo_trip.txt"), ("car insurance excess", "car_insurance.txt"),
    ("photo of a desert plant at night", "IMG_2001.jpg"), ("road winding along the coast", "IMG_2002.jpg"),
    ("voice note about the dentist", "voice_memo_1.aiff"), ("recording about growing vegetables", "voice_memo_2.m4a"),
    ("what did I say about sales in Europe", "voice_memo_3.aiff"),
    ("warranty period for the washing machine", "warranty.pdf"), ("monthly grocery and electricity spending", "budget.xlsx"),
    ("cover letter for the nursing job", "cover_letter.docx"), ("scanned parking fine", "scan_0042.pdf"),
    ("closing balance on my savings account", "bank_statement.pdf"),
    ("misty blue mountain ridges", "MOV_0031.mp4"), ("when do the swimming lessons start", "MOV_0031.mp4"),
]


def text_pdf(path, lines):
    """A minimal one-page PDF with real (extractable) text, written by hand to avoid a PDF library."""
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")              # noqa: E731
    stream = "BT /F1 12 Tf 50 760 Td 16 TL " + " ".join(f"({esc(l)}) Tj T*" for l in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


def video(path, images, speech, seconds_each=6, fps=5):
    """A short H.264 clip: each image held for a few seconds, with a spoken soundtrack."""
    from fractions import Fraction
    import av
    import soundfile as sf
    from PIL import Image
    samples, rate = sf.read(str(speech), dtype="float32")
    out = av.open(str(path), "w")
    vs = out.add_stream("libx264", rate=fps)               # both streams must exist before the first packet is written
    # One keyframe for the whole clip, as in many generated clips and screen recordings: sampling has to decode
    # forward to each moment, not just jump to keyframes.
    vs.width, vs.height, vs.pix_fmt, vs.options = 640, 360, "yuv420p", {"g": "1000", "keyint_min": "1000", "sc_threshold": "0"}
    sound = out.add_stream("aac", rate=rate)
    sound.layout, sound.codec_context.time_base = "mono", Fraction(1, rate)
    n = 0
    for img in images:
        im = Image.open(img).convert("RGB").resize((640, 360))
        for _ in range(seconds_each * fps):
            frame = av.VideoFrame.from_image(im)
            frame.pts = n
            n += 1
            for pkt in vs.encode(frame):
                out.mux(pkt)
    for pkt in vs.encode():
        out.mux(pkt)
    for i in range(0, len(samples) - 1023, 1024):          # whole AAC frames only
        frame = av.AudioFrame.from_ndarray(samples[i:i + 1024].reshape(1, -1).copy(), format="fltp", layout="mono")
        frame.sample_rate, frame.pts, frame.time_base = rate, i, Fraction(1, rate)
        for pkt in sound.encode(frame):
            out.mux(pkt)
    for pkt in sound.encode():
        out.mux(pkt)
    out.close()


def main(out):
    import docx
    import openpyxl
    from PIL import Image, ImageDraw, ImageFont

    shutil.rmtree(out, ignore_errors=True)
    top, out = out, out / "files"                          # queries.json stays outside the indexed folder
    out.mkdir(parents=True)
    for name, text in TEXTS.items():
        (out / name).write_text(text)

    for name, src in PHOTOS.items():                       # real photos, converted from this Mac's own wallpapers
        if name.endswith(".heic"):
            shutil.copy(WALLPAPERS / src, out / name)
        else:
            subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "1600", str(WALLPAPERS / src), "--out", str(out / name)],
                           check=True, capture_output=True)

    for name, words in SPEECH.items():
        aiff = out / (Path(name).stem + ".aiff")
        subprocess.run(["say", "-o", str(aiff), words], check=True)
        if name.endswith(".m4a"):
            subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(aiff), str(out / name)], check=True)
            aiff.unlink()

    scratch = top / "scratch"                               # inputs for the clip; not part of the indexed folder
    scratch.mkdir()
    for i, src in enumerate(("Big Sur Mountains.heic", "Dome.heic")):
        subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "1280", str(WALLPAPERS / src), "--out", str(scratch / f"{i}.jpg")],
                       check=True, capture_output=True)
    subprocess.run(["say", "-o", str(scratch / "speech.aiff"), "The swimming lessons start next Tuesday at four thirty."], check=True)
    video(out / "MOV_0031.mp4", [scratch / "0.jpg", scratch / "1.jpg"], scratch / "speech.aiff")
    shutil.rmtree(scratch)

    text_pdf(out / "warranty.pdf", ["Warranty certificate", "Product: Bosch Series 6 washing machine, model WAU28T64.",
                                    "The warranty period is 24 months from the date of purchase, 3 March 2026.",
                                    "It covers parts and labour for manufacturing faults. Keep this certificate with your receipt."])

    # Protected against editing but openable without a password, as many bank and government PDFs are.
    from pypdf import PdfWriter
    plain = out / "statement_plain.pdf"
    text_pdf(plain, ["Savings account statement for September", "Opening balance 4,120 dollars. Interest paid 11 dollars.",
                     "Closing balance 5,890 dollars."])
    writer = PdfWriter(clone_from=str(plain))
    writer.encrypt("", owner_password="owner", algorithm="AES-256")
    writer.write(str(out / "bank_statement.pdf"))
    plain.unlink()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Household budget"
    for row in [("Month", "Groceries", "Electricity", "Internet"), ("July", 812, 143, 79), ("August", 790, 151, 79), ("September", 845, 128, 79)]:
        ws.append(row)
    wb.save(out / "budget.xlsx")

    d = docx.Document()
    d.add_heading("Cover letter", 1)
    d.add_paragraph("Dear Hiring Manager, I am applying for the registered nurse position in the cardiac ward at St Mary's Hospital.")
    d.add_paragraph("I have six years of experience in acute care nursing, including two years as a charge nurse on night shift.")
    d.save(out / "cover_letter.docx")

    page = Image.new("RGB", (1240, 1754), "white")         # an image-only PDF, like a phone scan
    draw, font = ImageDraw.Draw(page), ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 44)
    for i, line in enumerate(["CITY COUNCIL", "PARKING INFRINGEMENT NOTICE", "", "Vehicle: Mazda CX-5", "Offence: parked in a no-stopping zone",
                              "Location: Harbour Street", "Amount due: 96 dollars", "Pay within 28 days"]):
        draw.text((110, 160 + i * 90), line, fill="black", font=font)
    page.save(out / "scan_0042.pdf")

    (top / "queries.json").write_text(json.dumps([{"query": q, "expect": e} for q, e in QUERIES], indent=2))
    print(f"wrote {len(list(out.iterdir()))} files and {len(QUERIES)} queries to {top}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
