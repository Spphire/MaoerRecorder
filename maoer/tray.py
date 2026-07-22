"""Windows system-tray integration for the dashboard."""
from __future__ import annotations

import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

from .process_manager import resource_dir


def _tray_image():
    from PIL import Image, ImageDraw

    branded = resource_dir() / "assets" / (
        "missevan-tray-light.png" if _system_uses_light_theme() else "missevan-tray-dark.png"
    )
    if branded.exists():
        return Image.open(branded).convert("RGBA")

    # Fallback used only when the packaged asset is unavailable.
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((3, 3, 61, 61), radius=12, fill="#17191c")
    draw.ellipse((14, 25, 20, 39), fill="#e6534e")
    draw.rounded_rectangle((25, 16, 31, 48), radius=3, fill="#f4f5f6")
    draw.rounded_rectangle((36, 22, 42, 42), radius=3, fill="#f4f5f6")
    draw.rounded_rectangle((47, 28, 53, 36), radius=3, fill="#f4f5f6")
    return image


def _system_uses_light_theme() -> bool:
    """Read the Windows taskbar theme; default to light on other systems."""
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        return bool(value)
    except (ImportError, OSError):
        return True


def run_tray(
    url: str,
    recordings_dir: Path,
    stop_all: Callable[[], int],
    on_exit: Callable[[], None],
) -> None:
    """Run the tray loop. Double-clicking the icon opens the dashboard."""
    import pystray

    closing = threading.Event()

    def open_panel(_icon=None, _item=None) -> None:
        webbrowser.open(url)

    def open_recordings(_icon=None, _item=None) -> None:
        if hasattr(recordings_dir, "startfile"):
            recordings_dir.startfile()  # pragma: no cover
        else:
            import os

            os.startfile(str(recordings_dir))  # type: ignore[attr-defined]

    def stop_everything(_icon=None, _item=None) -> None:
        stop_all()

    def exit_panel(icon, _item=None) -> None:
        if closing.is_set():
            return
        closing.set()
        on_exit()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开控制面板", open_panel, default=True),
        pystray.MenuItem("打开录制目录", open_recordings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("停止全部录制", stop_everything),
        pystray.MenuItem("退出控制面板（录制继续）", exit_panel),
    )
    icon = pystray.Icon("MaoerRecorder", _tray_image(), "MaoerRecorder", menu)
    def watch_theme() -> None:
        light_theme = _system_uses_light_theme()
        while not closing.wait(2.0):
            current = _system_uses_light_theme()
            if current != light_theme:
                light_theme = current
                icon.icon = _tray_image()

    threading.Thread(target=watch_theme, name="tray-theme-watcher", daemon=True).start()
    icon.run()
