"""Build taskbar and executable icons from Missevan's official favicon."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageFilter


ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SOURCE = ASSETS / "missevan.ico"


def source_mask(size: int) -> Image.Image:
    source = Image.open(SOURCE).convert("RGBA")
    alpha = source.getchannel("A")
    bounds = alpha.getbbox()
    if bounds:
        alpha = alpha.crop(bounds)
    padding = max(2, round(size * 0.14))
    target = size - padding * 2
    alpha.thumbnail((target, target), Image.Resampling.LANCZOS)

    mask = Image.new("L", (size, size), 0)
    x = (size - alpha.width) // 2
    y = (size - alpha.height) // 2
    mask.paste(alpha, (x, y))
    return mask


def silhouette(size: int, color: tuple[int, int, int, int]) -> Image.Image:
    mask = source_mask(size)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(Image.new("RGBA", (size, size), color), mask=mask)
    return canvas


def adaptive_exe_icon(size: int) -> Image.Image:
    mask = source_mask(size)
    outline_size = max(3, round(size * 0.025))
    if outline_size % 2 == 0:
        outline_size += 1
    outline = mask.filter(ImageFilter.MaxFilter(outline_size))
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(Image.new("RGBA", (size, size), (255, 255, 255, 230)), mask=outline)
    canvas.paste(Image.new("RGBA", (size, size), (16, 17, 19, 255)), mask=mask)
    return canvas


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    silhouette(64, (16, 17, 19, 255)).save(ASSETS / "missevan-tray-light.png")
    silhouette(64, (255, 255, 255, 255)).save(ASSETS / "missevan-tray-dark.png")
    icon = adaptive_exe_icon(256)
    icon.save(
        ASSETS / "MaoerRecorder.ico",
        format="ICO",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


if __name__ == "__main__":
    main()
