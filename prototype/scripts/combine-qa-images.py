from pathlib import Path
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[2]
source = Image.open(root / "screenshots/reference-fullscreen-v1.png").convert("RGB")
implementation = Image.open(root / "screenshots/product-home-fullscreen.png").convert("RGB")
target_size = (720, 450)
source.thumbnail(target_size)
implementation.thumbnail(target_size)
canvas = Image.new("RGB", (1460, 500), "#eef1f4")
draw = ImageDraw.Draw(canvas)
draw.text((20, 14), "REFERENCE", fill="#202733")
draw.text((750, 14), "IMPLEMENTATION", fill="#202733")
canvas.paste(source, (20, 40))
canvas.paste(implementation, (750, 40))
canvas.save(root / "screenshots/design-qa-comparison.png")
