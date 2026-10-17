"""Exercise the bundled native reader with synthetic scanned, filled and multipage PDFs."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

reader = sys.argv[1]
def read(path, start=1, count=2, ok=True):
    result = subprocess.run([reader, str(path), str(start), str(count)], capture_output=True, text=True, timeout=45)
    assert (result.returncode == 0) == ok, result.stdout or result.stderr
    return json.loads(result.stdout)

with tempfile.TemporaryDirectory(prefix="ls-doc-check-", dir="/tmp") as folder:
    root = Path(folder)
    image = Image.new("RGB", (1600, 800), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 42)
    for y, line in enumerate(("For: ALEX TEST", "Most Recent I-94", "Arrival/Issued Date: 2029 June 29", "Admit Until Date: 2030 April 10", "OMB No. 1651-0111", "Expiration Date: 01/31/2024")):
        draw.text((50, 60 + y * 100), line, fill="black", font=font)
    scan = root / "scan.pdf"
    pdf = canvas.Canvas(str(scan), pagesize=(800, 400))
    pdf.drawImage(ImageReader(image), 0, 0, width=800, height=400)
    pdf.save()
    result = read(scan)
    assert result["pages_total"] == 1 and result["pages"][0]["method"] == "on_device_ocr"
    text = result["pages"][0]["text"]
    assert "Admit Until Date" in text and "2030 April 10" in text and "01/31/2024" in text, text
    print("   ok native_reader_scanned_i94_dates")

    filled = root / "filled.pdf"
    pdf = canvas.Canvas(str(filled))
    pdf.drawString(50, 760, "Passport expiration date")
    pdf.acroForm.textfield(name="passport_expiration", tooltip="Passport expiration date", x=50, y=710, width=220, height=24, value="10/18/2030")
    pdf.save()
    result = read(filled)
    assert result["pages"][0]["filled_fields"] > 0 and "10/18/2030" in result["pages"][0]["text"]
    assert "Filled PDF fields" in result["pages"][0]["text"]
    print("   ok native_reader_filled_values_are_not_blank")

    pages = root / "pages.pdf"
    pdf = canvas.Canvas(str(pages))
    for page in range(5):
        pdf.drawString(50, 760, f"Page {page+1}: " + "This is searchable source text with enough characters to use its native PDF text layer. " * 2)
        pdf.showPage()
    pdf.save()
    result = read(pages, 2, 1)
    assert result["pages_total"] == 5 and result["has_more_pages"] and [p["page"] for p in result["pages"]] == [2]
    assert "Page 2" in result["pages"][0]["text"]
    assert read(pages, 9, 1, ok=False)["error"]
    print("   ok native_reader_pagination_and_errors")

print("   document checks OK")
