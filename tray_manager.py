import os
import sys
import threading
from tkinter import messagebox
from PIL import Image
import pystray
from pystray import MenuItem as Item

APP_NAME = "OnCodes Dev Service Manager"


class TrayManager:
    def __init__(self, root, service_manager, ui_manager=None, app_name=APP_NAME):
        self.root = root
        self.service_manager = service_manager
        self.ui_manager = ui_manager  # used for the tray actions (reads form values)
        if ui_manager is not None:
            # So the UI can have the menu rebuilt after a service is added or removed.
            ui_manager.tray_manager = self
        self.app_name = app_name
        self.tray_icon = None
        # Bundled next to the .exe when frozen, next to this file when run as a script.
        base = os.path.dirname(getattr(sys, '_MEIPASS', os.path.abspath(__file__)))
        self.icon_path = os.path.join(base, 'icon.ico')
        if not os.path.exists(self.icon_path):
            self.icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.ico')
        self.set_window_icon()

    def set_window_icon(self):
        """Set the window icon to the same .ico file"""
        if os.path.exists(self.icon_path):
            self.root.iconbitmap(self.icon_path)  # Set the Tkinter window icon
        else:
            print(f"Error: icon file not found at {self.icon_path}")

    def setup_tray(self):
        """Sets up tray functionality."""
        # Capture minimize event by binding the 'iconify' event
        self.root.bind("<Unmap>", self.on_minimize)
        # Handle the close button separately, ensuring it stops services and quits
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def on_close(self, event=None):
        """[X]: either hide to the tray or exit, depending on the settings page."""
        if self.service_manager.settings.get("minimize_to_tray_on_close", False):
            self.minimize_to_tray()
        else:
            self.exit_application()

    def on_minimize(self, event=None):
        """Handle minimize to tray when the window is minimized."""
        if self.root.state() == 'iconic':  # Only minimize to tray when minimized
            self.minimize_to_tray()

    def minimize_to_tray(self):
        """Minimizes the window and starts the system tray icon."""
        self.root.withdraw()  # Hide the window

        # If a tray icon is already running, don't spawn another one (which would leak
        # icons and threads each time the window is minimized).
        if self.tray_icon is not None:
            return

        if not os.path.exists(self.icon_path):
            print(f"Error: icon file not found at {self.icon_path}")
            return

        image = Image.open(self.icon_path)
        self.tray_icon = pystray.Icon(self.app_name, image, self._tooltip(), self._build_menu())
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def refresh_menu(self):
        """Rebuild the tray menu after the service or project list changes.

        Labels and checkmarks are callables and re-evaluate themselves, but the set of items
        is fixed when the menu is built — so a service added while the icon is live wouldn't
        appear without this."""
        if self.tray_icon is None:
            return  # nothing live; the menu is rebuilt next time it's minimised anyway
        try:
            self.tray_icon.menu = self._build_menu()
            self.tray_icon.update_menu()
        except Exception:
            pass  # a tray that's mid-teardown isn't worth taking the UI down for

    # ------------------------------------------------------------------ menu

    def _running_count(self, app_id):
        sm = self.service_manager
        return sum(1 for n in sm.services_for_app(app_id)
                   if not sm.is_task(n) and not sm.is_logview(n) and sm.is_running(n))

    def _tooltip(self):
        parts = [f"{a.get('name', a['id'])}: {self._running_count(a['id'])}"
                 for a in self.service_manager.apps]
        return f"{self.app_name}\n" + "  ".join(parts)

    def _controllable(self, app_id):
        """Services in this app that the tray can start/stop directly."""
        sm = self.service_manager
        return [n for n in sm.services_for_app(app_id)
                if not sm.is_task(n) and not sm.is_logview(n)]

    def _build_menu(self):
        """One submenu per app, mirroring the app rail: live running counts, scoped bulk
        actions, per-service toggles and one-shot tasks.

        Labels and checkmarks are callables so pystray re-evaluates them each time the menu
        is opened — the menu is only constructed once, when the window is first minimised."""
        sm = self.service_manager
        items = [
            Item(lambda i: self._tooltip().replace("\n", " — "), self._noop, enabled=False),
            pystray.Menu.SEPARATOR,
            Item('Show', self.restore_window, default=True),
            pystray.Menu.SEPARATOR,
        ]

        for app in sm.apps:
            app_id = app["id"]
            app_name = app.get("name", app_id)

            sub = [
                Item('Open', self._action(self._tray_open, app_id)),
                pystray.Menu.SEPARATOR,
                Item('Start All', self._action(self._bulk, 'start_all', app_id)),
                Item('Stop All', self._action(self._bulk, 'stop_all', app_id)),
                Item('Restart All', self._action(self._bulk, 'restart_all', app_id)),
            ]

            # Per-service toggles: the checkmark is the live run state, clicking flips it.
            services = self._controllable(app_id)
            if services:
                toggles = [
                    Item(self._service_label(n),
                         self._action(self._toggle_service, n),
                         checked=lambda it, n=n: sm.is_running(n))
                    for n in services
                ]
                sub += [pystray.Menu.SEPARATOR, Item('Services', pystray.Menu(*toggles))]

            tasks = [n for n in sm.services_for_app(app_id) if sm.is_task(n)]
            if tasks:
                task_items = [Item(n, self._action(self._tray_run_task, n)) for n in tasks]
                sub.append(Item('Tasks', pystray.Menu(*task_items)))

            if app.get("build"):
                label = app["build"].get("label", "Build")
                sub += [pystray.Menu.SEPARATOR,
                        Item(f'Rebuild {label}', self._action(self._tray_build, app_id))]

            items.append(Item(lambda i, a=app_id, n=app_name: f"{n} — {self._running_count(a)} running",
                              pystray.Menu(*sub)))

        items += [pystray.Menu.SEPARATOR, Item('Exit', self.exit_application)]
        return pystray.Menu(*items)

    @staticmethod
    def _action(fn, *args):
        """Wrap a call as a pystray action.

        pystray rejects any action taking more than two parameters, so extra context is
        bound into the closure instead of being carried as default arguments."""
        return lambda icon, item: fn(*args)

    def _bulk(self, method, app_id):
        self._on_ui(lambda ui: getattr(ui, method)(app_id))

    def _toggle_service(self, name):
        self._on_ui(lambda ui: ui.toggle_service(name))

    def _service_label(self, name):
        """Mark shared services in the tray the same way the sidebar does."""
        return f"{name}  (shared)" if self.service_manager.is_shared(name) else name

    def restore_window(self, icon=None, item=None):
        """Restore the window from the system tray."""
        if self.tray_icon:
            self.tray_icon.stop()  # Stop the tray icon
            self.tray_icon = None  # Allow a fresh icon to be created on next minimize
        # Called from the pystray thread, so touch Tkinter on the main thread only.
        self.root.after(0, self._show_window)

    def _show_window(self):
        self.root.deiconify()      # Show the main window
        self.root.state('normal')  # Restore the window to normal state

    # --- Tray actions (the menu fires on the tray thread; run on the main thread) ---

    def _noop(self, icon=None, item=None):
        pass

    def _on_ui(self, fn):
        if self.ui_manager:
            self.root.after(0, lambda: fn(self.ui_manager))

    def _tray_open(self, app_id):
        """Bring the window forward showing that app's dashboard."""
        self._on_ui(lambda ui: ui.switch_app(app_id))
        self.restore_window()

    def _tray_run_task(self, name):
        """Run a one-shot task and surface its page, so its output is actually visible."""
        self._on_ui(lambda ui: ui.run_task(name))
        self.restore_window()

    def _tray_build(self, app_id):
        self._on_ui(lambda ui: ui.run_build(app_id))
        self.restore_window()  # bring the window forward so build output is visible

    def exit_application(self, icon=None, item=None):
        """Exit by stopping the processes we started and removing the tray icon.

        Windows services are left alone — they were most likely running before this app
        started, and the other stack may still be using them."""
        # The tray menu fires on pystray's thread; the confirmation dialog and Tk's teardown
        # both have to run on the main one.
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, self.exit_application)
            return

        if self.service_manager.settings.get("confirm_on_exit", False):
            running = sum(1 for n in self.service_manager.services
                          if not self.service_manager.is_winservice(n)
                          and self.service_manager.is_running(n))
            detail = (f"\n\n{running} running service(s) will be stopped."
                      if running else "")
            if not messagebox.askyesno("Exit", f"Exit {self.app_name}?{detail}",
                                       parent=self.root):
                return
        self.service_manager.stop_all_services()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.quit()
        os._exit(0)  # Forcefully exit without raising SystemExit through Tk's mainloop
