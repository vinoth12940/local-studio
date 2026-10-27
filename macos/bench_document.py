"""Optional synthetic four-page OCR benchmark; never reads personal documents."""
import json
import subprocess
import tempfile
import time
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

reader = Path(__file__).resolve().parent.parent / 'build/DocumentReader'
with tempfile.TemporaryDirectory(prefix='ls-four-page-ocr-', dir='/tmp') as folder:
    pages=[]
    font=ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc',42)
    for i in range(4):
        image=Image.new('RGB',(1600,900),'white')
        draw=ImageDraw.Draw(image)
        draw.text((60,100),f'Synthetic document page {i+1}',fill='black',font=font)
        draw.text((60,250),'Date of Expiry: 2030 October 18',fill='black',font=font)
        draw.text((60,400),'Test fixture only. No personal information.',fill='black',font=font)
        pages.append(image)
    path=Path(folder)/'scanned.pdf';pages[0].save(path,'PDF',save_all=True,append_images=pages[1:],resolution=144)
    timings=[]
    for _ in range(3):
        start=time.perf_counter()
        output=subprocess.check_output([str(reader),str(path),'1','4'],text=True)
        timings.append(round(time.perf_counter()-start,3))
        result=json.loads(output)
        assert len(result['pages'])==4 and all(page['method']=='on_device_ocr' for page in result['pages'])
        assert all('2030 October 18' in page['text'] for page in result['pages'])
    print(json.dumps({'fixture':'4 synthetic scanned pages (1600x900)','seconds':timings,'max_seconds':max(timings)},indent=1))
