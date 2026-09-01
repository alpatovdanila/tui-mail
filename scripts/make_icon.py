"""Draw the tuimail app icon (rounded tokyo-night square + envelope) as PNG.

Used by CI to build the macOS .icns; needs Pillow. Usage: make_icon.py out.png
"""
import sys

from PIL import Image, ImageDraw

S = 1024
img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([64, 64, S - 64, S - 64], radius=180, fill=(26, 27, 38, 255))
d.rounded_rectangle([224, 336, 800, 688], radius=36,
                    outline=(187, 154, 247, 255), width=30)
d.line([252, 366, 512, 556, 772, 366], fill=(187, 154, 247, 255),
       width=30, joint='curve')
d.ellipse([700, 260, 820, 380], fill=(158, 206, 106, 255))  # unread dot
img.save(sys.argv[1] if len(sys.argv) > 1 else 'icon_1024.png')
print('icon written')
