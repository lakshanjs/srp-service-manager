import ctypes
import tkinter as tk
import sv_ttk
from service import ServiceManager
from tray_manager import TrayManager, APP_NAME
from ui_manager import UIManager, apply_dark_titlebar
from update import check_for_updates


def enable_hidpi():
    """Opt into high-DPI rendering *before* the Tk root is created.

    Without this, Windows draws the window at 96 DPI and bitmap-stretches it up on displays
    scaled above 100% (125%, 150%, ...), which makes all text look blurry. Declaring the
    process DPI-aware lets Tk render natively at the real DPI. Returns the display scale
    factor (1.0 at 100%, 1.25 at 125%, ...) so the caller can size the window to match."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # older fallback
        except Exception:
            return 1.0
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def set_taskbar_identity(app_id="OnCodes.DevServiceManager"):
    """Give the process its own taskbar identity.

    Without an explicit AppUserModelID, Windows groups a Tk app under the interpreter that
    launched it and shows *python.exe*'s icon on the taskbar, no matter what iconbitmap()
    sets. Frozen builds get their identity from the .exe, so this only matters when running
    from source — it's harmless either way."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


if __name__ == "__main__":
    # Detect this display's scaling (dynamically — works on any machine/monitor, not a
    # hard-coded value) and render crisply instead of letting Windows blur-stretch us.
    scale = enable_hidpi()
    set_taskbar_identity()  # must happen before the first window is created

    root = tk.Tk()
    # Draw point-sized fonts at the correct physical size for this DPI.
    root.tk.call("tk", "scaling", scale * 96 / 72.0)

    # Service manager first: it loads the config the window size comes from.
    service_manager = ServiceManager()
    window_cfg = service_manager.settings.get("window", {})

    root.title(APP_NAME)
    root.geometry(f"{int(window_cfg.get('width', 1120) * scale)}x{int(window_cfg.get('height', 680) * scale)}")
    root.minsize(int(880 * scale), int(560 * scale))

    # Apply the modern Sun Valley dark theme
    sv_ttk.set_theme("dark")
    root.configure(background="#1c1c1c")

    ui_manager = UIManager(root, service_manager, ui_scale=scale)
    tray_manager = TrayManager(root, service_manager, ui_manager)

    # Build the app rail + sidebar + content layout
    ui_manager.build()

    # Set up the tray functionality
    tray_manager.setup_tray()

    # Darken the title bar once the window exists
    apply_dark_titlebar(root)

    # Check for updates on startup (runs in the background; alerts on the main thread).
    # A failed check reports to the activity log rather than interrupting launch with a
    # dialog about GitHub being unreachable.
    if service_manager.settings.get("check_updates", True):
        check_for_updates(root, service_manager.settings, log=ui_manager.home_log)

    # Straight to the tray, if that's what the settings page says. Deferred so the window
    # is fully mapped first — withdrawing a half-built window leaves it un-restorable.
    if service_manager.settings.get("start_minimized", False):
        root.after(300, tray_manager.minimize_to_tray)

    root.mainloop()
