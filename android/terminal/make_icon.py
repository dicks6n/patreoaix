"""Draws the PX Terminal launcher icon (an original design: candles on a dark tile, last candle in brand gold).
   python3 android/terminal/make_icon.py  ->  store/static/store/icons/terminal-512.png and terminal-192.png"""
import os
from PIL import Image, ImageDraw, ImageFilter

S = 1024
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, 'store', 'static', 'store', 'icons')

im = Image.new('RGBA', (S, S), (0, 0, 0, 0))
grad = Image.new('RGBA', (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / S
    gd.line([(0, y), (S, y)], fill=(int(22 - 10 * t), int(26 - 12 * t), int(36 - 16 * t), 255))
mask = Image.new('L', (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=230, fill=255)
im.paste(grad, (0, 0), mask)

glow = Image.new('RGBA', (S, S), (0, 0, 0, 0))
ImageDraw.Draw(glow).ellipse([690, 170, 970, 570], fill=(245, 179, 1, 90))
glow = glow.filter(ImageFilter.GaussianBlur(70))
glow.putalpha(Image.composite(glow.getchannel('A'), Image.new('L', (S, S), 0), mask))
im = Image.alpha_composite(im, glow)

d = ImageDraw.Draw(im)
GOLD, GREEN, RED, GRID = (245, 179, 1, 255), (34, 197, 94, 255), (239, 68, 68, 255), (44, 50, 66, 255)
for y in (300, 460, 620, 780):
    d.line([(150, y), (874, y)], fill=GRID, width=4)
# x, open, close, high, low
for x, o, c, h, l, col in ((230, 700, 600, 560, 760, GREEN), (350, 610, 660, 570, 720, RED),
                           (470, 650, 500, 450, 690, GREEN), (590, 520, 430, 380, 560, GREEN),
                           (710, 440, 480, 400, 540, RED), (830, 470, 300, 240, 500, GOLD)):
    d.line([(x, h), (x, l)], fill=col, width=14)
    d.rounded_rectangle([x - 42, min(o, c), x + 42, max(o, c)], radius=12, fill=col)
d.line([(150, 860), (874, 860)], fill=GOLD, width=10)

out = im.resize((512, 512), Image.LANCZOS)
out.save(os.path.join(OUT, 'terminal-512.png'), optimize=True)
out.resize((192, 192), Image.LANCZOS).save(os.path.join(OUT, 'terminal-192.png'), optimize=True)
