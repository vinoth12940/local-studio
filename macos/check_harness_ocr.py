"""Read a synthetic scanned PDF through the actual harness/native-reader boundary."""
import os
import sys
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix='ls-harness-ocr-', dir='/tmp') as folder:
    root = Path(folder)
    os.environ['LOCAL_STUDIO_HOME'] = str(root / 'state')
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from PIL import Image, ImageDraw, ImageFont
    from studio.services.harness import _chat_read_tool
    from studio.config import DOCUMENT_READER
    assert DOCUMENT_READER.is_file(), 'Build DocumentReader before running this check'
    image = Image.new('RGB', (1600, 600), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 42)
    draw.text((60, 100), 'Synthetic Passport', fill='black', font=font)
    draw.text((60, 230), 'Date of Expiry: 2030 October 18', fill='black', font=font)
    path = root / 'scan.pdf'
    image.save(path, 'PDF', resolution=144)
    result = _chat_read_tool('read_file', {'path': str(path), 'start_page': 0, 'max_pages': 9})
    assert not result.get('error'), result
    assert result['start_page'] == 1 and result['pages_read'] == 1
    assert 'on_device_ocr' in result['text'] and '2030 October 18' in result['text'], result
    print('   ok harness_read_file_native_ocr')
