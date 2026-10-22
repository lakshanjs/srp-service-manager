import ctypes
import json
import math
import os
import re
import subprocess
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk, messagebox, colorchooser, filedialog
import tkinter.font as tkfont
from PIL import Image, ImageDraw, ImageFont, ImageTk
import config_manager
import redis_lists      # tiny Redis/Memurai client for the Lists panel
import update           # version string + the GitHub release check
import claude_setup     # registering the MCP server with Claude Code
from control_api import LogBuffer  # recent log lines, readable through the control API

# Tk's canvas draws shapes with no antialiasing, which makes a small circle badge look
# visibly stair-stepped. The rail tiles are therefore drawn with PIL at this multiple and
# downsampled with LANCZOS, which is what actually smooths the edges.
TILE_SUPERSAMPLE = 4

# Type. Windows 11's own Segoe UI Variable, in the optical size each role calls for, and
# Cascadia Mono for what is literally code: logs, command lines, pids. Resolved against the
# installed families once Tk is up (pick_fonts), falling back to plain Segoe UI / Consolas.
UI_FONT = "Segoe UI Variable Text"
UI_STRONG_FONT = "Segoe UI Variable Text Semibold"
SMALL_FONT = "Segoe UI Variable Small"
DISPLAY_FONT = "Segoe UI Variable Display Semib"
MONO_FONT = "Cascadia Mono"

# Dark "console" styling for the log areas (tk.Text isn't themed by ttk).
LOG_BG = "#202124"
LOG_FG = "#d4d4d1"
LOG_INSERT = "#ffffff"
LOG_SELECT_BG = "#2f4a66"
LOG_FIND_BG = "#5c4a1a"      # every match of the find box
LOG_FIND_CURRENT = "#9c7a1c"  # the match the find box is on
MUTED = "#8e9299"
FAINT = "#6b6f76"            # captions under the muted text: kinds, pids, uptimes
SMALL_BUTTON_BG = "#2c2e33"
SMALL_BUTTON_LINE = "#3a3d43"

# Log syntax colors: dim the bracketed [timestamp]/[pid]/[level] noise so the actual
# message stands out, and tint warnings/errors so they're scannable at a glance.
LOG_META = "#6b7681"   # bracketed metadata — de-emphasised
LOG_WARN = "#e3b341"   # amber
LOG_ERROR = "#f47067"  # soft red
LOG_MATCH = re.compile(r"\[[^\]\n]*\]")  # bracketed segments
LOG_ERROR_WORDS = ("error", "fatal", "exception", "critical", "[fail", "traceback")
LOG_WARN_WORDS = ("warn", "deprecat")
# A Text widget gets slower and heavier with every line it holds, so each log keeps only the
# most recent lines (the control API's LogBuffer has its own, separate copy). Output is
# drawn in batches rather than per line: a chatty server would otherwise flood the event loop.
LOG_MAX_LINES = 5000
LOG_TRIM_SLACK = 500   # trim in chunks, not a line at a time
LOG_FLUSH_MS = 100

# App rail (left-most icon strip) colors
RAIL_BG = "#1b1b1c"
RAIL_TILE_BG = "#323234"
RAIL_TILE_HOVER = "#3d3d40"
RAIL_TILE_FG = "#cccccc"
RAIL_INDICATOR = "#ffffff"

# Sidebar navigation colors
SIDEBAR_BG = "#252526"
NAV_FG = "#cccccc"
NAV_HOVER_BG = "#2a2d2e"
NAV_ACTIVE_BG = "#37373d"
NAV_SECTION_FG = "#7d8590"  # dim label for section headers (SERVICES / LOGS / ...)
SHARED_FG = "#7d8590"       # the "shared between apps" marker

# Status lamps. Each colour means one thing everywhere it appears; nothing else in the
# interface uses them, so a lit lamp always reads as state.
RUNNING_COLOR = "#46c37b"    # live
STOPPED_COLOR = "#5d626b"    # off
ATTENTION_COLOR = "#e0a43a"  # starting / pending / a Windows service that can't be found
FAULT_COLOR = "#ef6b5f"      # exited on its own with an error
LAMP_OFF = "#44474d"         # an unlit lamp: dimmer than the "Stopped" text beside it

# Content background (matches the root/theme dark), used for scroll canvases
PAGE_BG = "#1c1c1c"

# Marks a service more than one app uses. Spelled out rather than drawn as a glyph:
# arrow/link symbols collapse into an ambiguous "=" at sidebar font sizes.
SHARED_TEXT = "shared"
# A shared service's page belongs to no single project, so its header rule is neutral.
SHARED_RULE_COLOR = "#4a4d54"

# The settings page hangs off the rail rather than any one app, so it gets a fixed page id
# and a neutral (non app-coloured) active tile.
SETTINGS_PAGE = "@settings"
SETTINGS_TILE_ACTIVE = "#4d4d55"

# The service types the editor can create, as (key, label, description).
SERVICE_TYPES = [
    ("process", "Process",
     "A command this app launches and keeps running, with its output streamed here."),
    ("cron", "Cron (URL poll)",
     "Calls a URL every N seconds for as long as it's running."),
    ("winservice", "Windows service",
     "Starts and stops a service installed in Windows. Each action needs UAC."),
    ("logview", "Log viewer",
     "No controls — the page just tails a log file."),
    ("task", "One-shot task",
     "Runs once on demand and exits, e.g. a manage.py command."),
]

# Keys the service editor owns. Anything else on an existing entry is left untouched when
# it's edited, so a hand-added key isn't silently dropped by a round-trip through the form.
EDITOR_KEYS = {"app", "type", "dir", "command", "url", "interval", "service_name",
               "log_file", "redis", "section", "interactive", "include_in_start_all",
               "autostart"}

# Fields on an app definition the project editor manages itself; every other scalar key is
# offered as an editable ${token}.
APP_RESERVED_KEYS = config_manager.APP_RESERVED_KEYS


def apply_dark_titlebar(window):
    """Darken a window's native Windows title bar to match the dark theme.

    Lives here rather than in main so dialogs opened from the UI can use it too (a Toplevel
    gets a bright title bar by default, which looks broken against the dark theme).
    A no-op on errors or non-Windows."""
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:
        pass


def gear_points(cx, cy, r_out, r_in, teeth=8):
    """Outline of a cog centred on (cx, cy), as a polygon point list.

    Drawn geometrically rather than as a ⚙ glyph: the symbol fonts that carry it aren't
    guaranteed to be installed, and the ones that are render it at a different weight to
    everything else on the rail."""
    points = []
    step = 2 * math.pi / teeth
    for i in range(teeth):
        base = i * step
        # Tooth (outer radius) then valley (inner radius); the gaps between the two pairs
        # become the sloped flanks of each tooth.
        for radius, offset in ((r_out, -step * 0.20), (r_out, step * 0.20),
                               (r_in, step * 0.30), (r_in, step * 0.70)):
            angle = base + offset
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def _close_colors(a, b, threshold=120):
    """Whether two #rrggbb colours are too similar to read as distinct side by side.

    A plain channel-sum distance: crude next to a perceptual metric, but all this needs to
    decide is 'would one of these disappear on top of the other'."""
    ra, rb = [tuple(int(c.lstrip('#')[i:i + 2], 16) for i in (0, 2, 4)) for c in (a, b)]
    return sum(abs(x - y) for x, y in zip(ra, rb)) < threshold


def page_key(kind, app_id):
    """Page ids for the built-in per-app pages. The '@' prefix keeps them from colliding
    with service names, which are used as page ids directly."""
    return f"@{kind}:{app_id}"


def pick_fonts(root):
    """Point the font names at families this machine actually has.

    Tk substitutes a missing family silently and badly (a missing mono face comes out
    proportional), so a Windows 10 box gets the plain Segoe UI / Consolas it does have."""
    global UI_FONT, UI_STRONG_FONT, SMALL_FONT, DISPLAY_FONT, MONO_FONT
    families = set(tkfont.families(root))

    def first(*names):
        return next((n for n in names if n in families), names[-1])

    UI_FONT = first("Segoe UI Variable Text", "Segoe UI")
    UI_STRONG_FONT = first("Segoe UI Variable Text Semibold", "Segoe UI Semibold", "Segoe UI")
    SMALL_FONT = first("Segoe UI Variable Small", "Segoe UI")
    DISPLAY_FONT = first("Segoe UI Variable Display Semib", "Segoe UI Semibold", "Segoe UI")
    MONO_FONT = first("Cascadia Mono", "Consolas")


def show_if(widget, visible):
    """grid() or grid_remove() a widget, touching it only when that actually changes."""
    if widget is None or getattr(widget, "_shown", None) == visible:
        return
    widget._shown = visible
    if visible:
        widget.grid()
    else:
        widget.grid_remove()


def format_uptime(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def set_if_changed(widget, **options):
    """configure() only the options whose value actually differs.

    The status tick runs every second over every dot, pill and button; re-setting an
    unchanged option still makes Tk redraw the widget, which is most of the app's idle CPU.
    Compares against cget() rather than a cache, so a change made elsewhere is still seen."""
    changed = {k: v for k, v in options.items() if str(widget.cget(k)) != str(v)}
    if changed:
        widget.configure(**changed)


class UIManager:
    def __init__(self, root, service_manager, ui_scale=1.0):
        self.root = root
        self.service_manager = service_manager
        self.ui_scale = ui_scale  # display scale factor (1.0 = 100%, 1.25 = 125%, ...)

        self.apps = service_manager.apps
        self.current_app = None
        self.last_page_by_app = {}   # app id -> page id, so switching back restores context

        self.service_tabs = {}
        self.find_entries = {}  # page id -> the find box of the log on that page
        # Every line the logs show, kept so the control API can hand it to Claude Code & co.
        self.log_buffer = LogBuffer()
        self.pending_output = deque()    # (service name, text) awaiting _flush_output
        self.output_flush_scheduled = False
        self.redis_trees = {}         # service name -> Treeview listing Redis lists
        self.redis_count_labels = {}  # service name -> "N list(s)" label

        self.tray_manager = None  # set by TrayManager so the menu can follow config edits

        self.content = None
        self.rail_frame = None
        self.rail_apps = None    # the app tiles' container, rebuilt when projects change
        self.rail_tiles = {}     # app id -> {"canvas", "indicator", "badge_items"}
        self.settings_tile = None
        self.brand_label = None
        self.brand_sub = None
        self.nav_frame = None    # scrollable container that holds the nav rows/sections
        self.nav_canvas = None   # canvas backing the scrollable nav
        self.pages = {}          # page id -> content frame
        self.nav_items = {}      # page id -> {"widgets": [...], "text": label}
        self.status_dots = {}    # service name -> dot label in the *current* app's nav
        self.current_page = None

        # Per-app dashboard widgets
        self.home_logs = {}          # app id -> activity log Text
        self.dash_rows = {}          # app id -> {service name: dashboard row widgets}
        self.summary_labels = {}     # app id -> (running label, stopped label)
        self.startup_vars = {}       # app id -> {name: (start_all_var, autostart_var)}
        self.services_tables = {}    # app id -> frame holding the dashboard services grid
        self.home_scrolls = {}       # app id -> scroll inner frame (for re-binding the wheel)

        # Updates card widgets
        self.update_repo_var = None
        self.update_token_var = None
        self.update_status = None

        # Per-app build page widgets
        self.build_logs = {}     # app id -> Text
        self.build_entries = {}  # app id -> (path entry, command entry)
        self.build_buttons = {}  # app id -> Button

        # Settings page widgets
        self.settings_scroll = None      # scroll area, for re-binding the wheel
        self.paths_box = None            # container for the path-root rows
        self.path_entries = {}           # root name -> StringVar
        self.paths_note = None
        self.settings_vars = {}          # settings key -> BooleanVar
        self.settings_apps_box = None    # container for the project rows
        self.settings_summary = None     # "N services across M projects"
        self.settings_window_entries = None
        self.settings_note = None
        self.config_editor = None        # raw JSON editor Text
        self.config_editor_file = None   # StringVar: which file is loaded
        self.config_editor_status = None
        self.config_path_label = None
        self.config_editor_dirty = False
        self.config_editor_baseline = None  # editor text as last loaded/saved

        self._tooltip = None
        self._init_styles()

    def _px(self, n):
        """Scale a pixel measurement by the display scale so spacing stays proportional
        to the (DPI-scaled) fonts on high-DPI screens."""
        return int(round(n * self.ui_scale))

    def _init_styles(self):
        """Tweak the Sun Valley theme: the app's type scale, and roomier buttons."""
        pick_fonts(self.root)
        style = ttk.Style()
        bpad = (self._px(14), self._px(7))
        style.configure("TButton", padding=bpad, font=(UI_FONT, 10))
        style.configure("Accent.TButton", padding=bpad, font=(UI_FONT, 10))
        # Row-level controls on the dashboard: same buttons, sized to sit inside a list row.
        spad = (self._px(10), self._px(3))
        # Small buttons are drawn flat, by the parent (clam) theme's border element, instead
        # of from sv_ttk's bitmaps: a dashboard has dozens of them, and on screen the bitmap
        # ones took ~4x as long to paint (39 of them: 77 ms vs 20 ms), most of a page switch.
        # Solid relief so the outline is drawn, which is what shows focus and a press.
        style.layout("Small.TButton", [("Button.border", {"sticky": "nswe", "border": "1", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})])
        style.configure("Small.TButton", padding=spad, font=(UI_FONT, 9), relief="solid",
                        borderwidth=1, background=SMALL_BUTTON_BG, foreground="#e8e6e3",
                        bordercolor=SMALL_BUTTON_LINE, lightcolor=SMALL_BUTTON_BG,
                        darkcolor=SMALL_BUTTON_BG)
        fill = [("disabled", "#232427"), ("pressed", "#1d1e21"), ("active", "#3a3d43")]
        style.map("Small.TButton", background=fill, lightcolor=fill, darkcolor=fill,
                  foreground=[("disabled", FAINT), ("pressed", "#b9bcc1")],
                  bordercolor=[("focus", "#e8e6e3"), ("disabled", "#2b2c30"),
                               ("pressed", "#55585f"), ("active", "#4a4d54")])
        style.configure("Small.Accent.TButton", padding=spad, font=(UI_FONT, 9))
        style.configure("Title.TLabel", font=(DISPLAY_FONT, 20))
        style.configure("Header.TLabel", font=(DISPLAY_FONT, 17))
        style.configure("Section.TLabel", font=(UI_STRONG_FONT, 10))
        style.configure("State.TLabel", font=(UI_STRONG_FONT, 10))
        style.configure("Row.TLabel", font=(UI_FONT, 10))
        # sv_ttk maps TLabel's foreground per state, and a map beats configure(), so the
        # dimmed styles set theirs through map() as well.
        for name, font, color in (("Subtitle.TLabel", (UI_FONT, 10), MUTED),
                                  ("Mono.TLabel", (MONO_FONT, 9), MUTED),
                                  ("FaintMono.TLabel", (MONO_FONT, 9), FAINT),
                                  ("Faint.TLabel", (SMALL_FONT, 9), FAINT)):
            style.configure(name, font=font, foreground=color)
            style.map(name, foreground=[("disabled", FAINT), ("!disabled", color)])

    # ------------------------------------------------------------------ layout

    def build(self):
        """Build the window: app rail | sidebar nav | content area."""
        self._build_app_rail()

        # Sidebar (service nav for the selected app)
        sidebar = tk.Frame(self.root, bg=SIDEBAR_BG, width=self._px(210))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)  # keep a fixed width

        brand_box = tk.Frame(sidebar, bg=SIDEBAR_BG)
        brand_box.pack(fill="x", padx=self._px(16), pady=(self._px(16), self._px(10)))
        self.brand_label = tk.Label(brand_box, text="", bg=SIDEBAR_BG, fg="#ffffff",
                                    font=(DISPLAY_FONT, 13), anchor="w")
        self.brand_label.pack(fill="x")
        # The project's live count, so it's in view from any page, not just the dashboard.
        self.brand_sub = tk.Label(brand_box, text="", bg=SIDEBAR_BG, fg=NAV_SECTION_FG,
                                  font=(SMALL_FONT, 9), anchor="w")
        self.brand_sub.pack(fill="x")

        # Scrollable nav area, so a long service list doesn't overflow small windows.
        self.nav_frame = self._build_scrollable_nav(sidebar)

        # Content (right) — pages are stacked here and raised on selection
        self.content = ttk.Frame(self.root)
        self.content.pack(side="left", fill="both", expand=True)

        # Build every page up front; the nav is what changes when apps are switched.
        self._build_app_pages()

        for name in list(self.service_manager.services):
            self._add_service_page(name, tail_log=False)  # tails are started together below

        settings_page = ttk.Frame(self.content)
        self._build_settings_page(settings_page)
        self.pages[SETTINGS_PAGE] = settings_page

        start_app = self.service_manager.settings.get("last_app")
        if start_app not in self.service_manager.app_by_id:
            start_app = self.apps[0]["id"] if self.apps else None
        self.switch_app(start_app)

        self.root.bind_all("<Control-f>", self._focus_find)  # find in the open page's log
        self._refresh_status()   # begin polling running/stopped state
        self._start_log_tails()  # stream configured log files into their pages
        self.root.after(800, self.autostart_services)

    # -------------------------------------------------------------- app rail

    def _build_app_rail(self):
        """The left-most icon strip: one tile per app, plus a live running-count badge."""
        rail = tk.Frame(self.root, bg=RAIL_BG, width=self._px(60))
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        self.rail_frame = rail

        # The gear is packed first, against the bottom edge, so however many app tiles are
        # added above it, it stays pinned to the bottom of the rail.
        self._add_settings_tile(rail)

        self.rail_apps = tk.Frame(rail, bg=RAIL_BG)
        self.rail_apps.pack(side="top", fill="x", pady=(self._px(12), 0))
        for app in self.apps:
            self._add_rail_tile(self.rail_apps, app)

    def _rebuild_app_rail(self):
        """Redraw the app tiles after a project is added, edited or removed."""
        for child in self.rail_apps.winfo_children():
            child.destroy()
        self.rail_tiles.clear()
        for app in self.apps:
            self._add_rail_tile(self.rail_apps, app)
        self._update_rail()

    def _add_rail_tile(self, rail, app):
        app_id = app["id"]
        size = self._px(42)

        row = tk.Frame(rail, bg=RAIL_BG)
        row.pack(fill="x", pady=self._px(4))

        # Active-app indicator bar down the left edge (VS Code activity-bar style).
        indicator = tk.Frame(row, bg=RAIL_BG, width=self._px(3), height=size)
        indicator.pack(side="left", fill="y")
        indicator.pack_propagate(False)

        canvas = tk.Canvas(row, width=size, height=size, bg=RAIL_BG,
                           highlightthickness=0, borderwidth=0, cursor="hand2")
        canvas.pack(side="left", padx=(self._px(5), 0))
        item = canvas.create_image(0, 0, anchor="nw")

        self.rail_tiles[app_id] = {
            "canvas": canvas, "item": item, "indicator": indicator, "app": app,
            "size": size, "hovered": False, "key": None, "photo": None,
        }

        canvas.bind("<Button-1>", lambda e, a=app_id: self.switch_app(a))
        canvas.bind("<Enter>", lambda e, a=app_id, n=app.get("name", app_id): (
            self._rail_hover(a, True), self._show_tooltip(e, n)))
        canvas.bind("<Leave>", lambda e, a=app_id: (self._rail_hover(a, False), self._hide_tooltip()))

    def _add_settings_tile(self, rail):
        """The gear at the foot of the rail — opens the settings page."""
        size = self._px(42)

        row = tk.Frame(rail, bg=RAIL_BG)
        row.pack(side="bottom", fill="x", pady=(self._px(6), self._px(12)))

        indicator = tk.Frame(row, bg=RAIL_BG, width=self._px(3), height=size)
        indicator.pack(side="left", fill="y")
        indicator.pack_propagate(False)

        canvas = tk.Canvas(row, width=size, height=size, bg=RAIL_BG,
                           highlightthickness=0, borderwidth=0, cursor="hand2")
        canvas.pack(side="left", padx=(self._px(5), 0))
        item = canvas.create_image(0, 0, anchor="nw")

        self.settings_tile = {"canvas": canvas, "item": item, "indicator": indicator,
                              "size": size, "hovered": False, "key": None, "photo": None}

        canvas.bind("<Button-1>", lambda e: self.show_settings())
        canvas.bind("<Enter>", lambda e: (self._settings_hover(True), self._show_tooltip(e, "Settings")))
        canvas.bind("<Leave>", lambda e: (self._settings_hover(False), self._hide_tooltip()))

    def _pil_font(self, px):
        """A bold UI font at `px`, falling back through what Windows is likely to have."""
        for name in ("segoeuib.ttf", "arialbd.ttf", "seguisb.ttf"):
            try:
                return ImageFont.truetype(name, px)
            except OSError:
                continue
        return ImageFont.load_default()

    def _draw_centered(self, draw, cx, cy, text, font, fill):
        """Centre text on a point. textbbox rather than the `anchor` argument, which the
        bitmap fallback font doesn't support."""
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        draw.text((cx - (right + left) / 2, cy - (bottom + top) / 2), text, font=font, fill=fill)

    def _render_tile(self, app, size, active, hovered, running):
        """Draw one rail tile — rounded square, monogram, running badge — supersampled.

        Everything is drawn at TILE_SUPERSAMPLE× and reduced with LANCZOS, so the badge
        circle and the rounded corners come out smooth instead of stair-stepped."""
        ss = TILE_SUPERSAMPLE
        big = size * ss
        fill = app.get("color", "#4c9aff") if active else (RAIL_TILE_HOVER if hovered else RAIL_TILE_BG)
        fg = "#ffffff" if active else RAIL_TILE_FG

        # Painting the background with the rail colour lets the result sit on the canvas
        # without any alpha compositing, and keeps the rounded corners cleanly blended.
        img = Image.new("RGB", (big, big), RAIL_BG)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(size * 0.24) * ss, fill=fill)

        short = (app.get("short") or app["id"][:3]).upper()
        self._draw_centered(draw, big / 2, big / 2, short,
                            self._pil_font(int(size * 0.30) * ss), fg)

        if running:
            r = int(size * 0.21) * ss
            cx, cy = big - r - ss, r + ss
            # An app whose colour is itself green (Neobe is #3fb950, the same green) would
            # swallow a green badge on its active tile, so fall back to a dark chip whenever
            # the badge and the tile are too close to tell apart.
            if _close_colors(RUNNING_COLOR, fill):
                badge_fill, badge_fg = "#14161a", "#ffffff"
            else:
                badge_fill, badge_fg = RUNNING_COLOR, "#08260f"
            # The rail-coloured ring separates the badge from the tile underneath.
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=badge_fill,
                         outline=RAIL_BG, width=max(1, int(1.5 * ss)))
            text = str(running) if running < 10 else "9+"
            self._draw_centered(draw, cx, cy, text,
                                self._pil_font(int(size * 0.24) * ss), badge_fg)

        return ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))

    def _render_settings_tile(self, size, active, hovered):
        """Draw the gear tile, supersampled like the app tiles so its teeth stay smooth."""
        ss = TILE_SUPERSAMPLE
        big = size * ss
        fill = SETTINGS_TILE_ACTIVE if active else (RAIL_TILE_HOVER if hovered else RAIL_TILE_BG)
        fg = "#ffffff" if active else RAIL_TILE_FG

        img = Image.new("RGB", (big, big), RAIL_BG)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(size * 0.24) * ss, fill=fill)

        centre = big / 2
        draw.polygon(gear_points(centre, centre, big * 0.30, big * 0.225), fill=fg)
        hub = big * 0.105  # punched back out in the tile colour, so the cog reads as a ring
        draw.ellipse([centre - hub, centre - hub, centre + hub, centre + hub], fill=fill)

        return ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))

    def _rail_hover(self, app_id, entering):
        self.rail_tiles[app_id]["hovered"] = entering
        self._update_rail()

    def _settings_hover(self, entering):
        self.settings_tile["hovered"] = entering
        self._update_rail()

    def _update_rail(self):
        """Reflect the active app and each app's live running count on the rail."""
        # With the settings page open no app is the one being viewed, so its tile drops back
        # to inactive rather than lighting a second indicator alongside the gear.
        settings_open = self.current_page == SETTINGS_PAGE

        for app_id, t in self.rail_tiles.items():
            active = app_id == self.current_app and not settings_open
            set_if_changed(t["indicator"], bg=RAIL_INDICATOR if active else RAIL_BG)

            running = sum(1 for n in self.service_manager.services_for_app(app_id)
                          if not self.service_manager.is_task(n)
                          and not self.service_manager.is_logview(n)
                          and self.service_manager.is_running(n))

            # _update_rail runs on the 1s status tick, so only redraw when something a
            # viewer can actually see has changed.
            key = (active, t["hovered"] and not active, running)
            if key == t["key"]:
                continue
            photo = self._render_tile(t["app"], t["size"], active, key[1], running)
            t["canvas"].itemconfigure(t["item"], image=photo)
            t["photo"] = photo  # keep a reference or Tk garbage-collects the image
            t["key"] = key

        tile = self.settings_tile
        if tile is not None:
            set_if_changed(tile["indicator"], bg=RAIL_INDICATOR if settings_open else RAIL_BG)
            key = (settings_open, tile["hovered"] and not settings_open)
            if key != tile["key"]:
                photo = self._render_settings_tile(tile["size"], *key)
                tile["canvas"].itemconfigure(tile["item"], image=photo)
                tile["photo"] = photo
                tile["key"] = key

    def _show_tooltip(self, event, text):
        self._hide_tooltip()
        tip = tk.Toplevel(self.root)
        tip.wm_overrideredirect(True)  # borderless, no title bar
        tip.attributes("-topmost", True)
        tk.Label(tip, text=text, bg="#2b2b2c", fg="#e6e6e6", font=(UI_FONT, 9),
                 padx=self._px(8), pady=self._px(4), bd=0).pack()
        tip.wm_geometry(f"+{event.x_root + self._px(14)}+{event.y_root}")
        self._tooltip = tip

    def _hide_tooltip(self):
        if self._tooltip is not None:
            self._tooltip.destroy()
            self._tooltip = None

    def switch_app(self, app_id):
        """Select an app: rebuild the sidebar for its services and restore its last page."""
        if app_id is None or app_id not in self.service_manager.app_by_id:
            return
        self.current_app = app_id
        app = self.service_manager.app_by_id[app_id]

        self.brand_label.configure(text=app.get("name", app_id))
        self._build_nav_for_app(app_id)

        target = self.last_page_by_app.get(app_id, page_key("home", app_id))
        if target not in self.pages or (target in self.service_manager.services
                                        and app_id not in self.service_manager.apps_of(target)):
            target = page_key("home", app_id)
        self.show_page(target)

        self.service_manager.settings["last_app"] = app_id
        config_manager.save_settings(self.service_manager.settings)
        self._apply_status()

    # -------------------------------------------------------- page lifecycle

    def _build_app_pages(self):
        """(Re)create the dashboard and build pages for every configured app."""
        for key in [k for k in self.pages if k.startswith("@home:") or k.startswith("@build:")]:
            page = self.pages.pop(key)
            page.pack_forget()
            page.destroy()
            if key == self.current_page:
                self.current_page = None  # don't try to unpack a destroyed page later

        for store in (self.home_logs, self.dash_rows, self.summary_labels,
                      self.startup_vars, self.services_tables, self.home_scrolls,
                      self.build_logs, self.build_entries, self.build_buttons):
            store.clear()

        for app in self.apps:
            app_id = app["id"]
            home = ttk.Frame(self.content)
            self._build_home_page(home, app_id)
            self.pages[page_key("home", app_id)] = home

            if app.get("build"):
                build_page = ttk.Frame(self.content)
                self._build_build_page(build_page, app_id)
                self.pages[page_key("build", app_id)] = build_page

    def _add_service_page(self, name, tail_log=True):
        """Create the page for a service and start tailing its log file, if it has one."""
        page = ttk.Frame(self.content)
        self._build_service_page(page, name, self.service_manager.services[name])
        self.pages[name] = page
        if tail_log and self.service_manager.services[name].get("log_file"):
            self.service_manager.start_log_tail(name, self.append_output)

    def _drop_service_page(self, name):
        """Destroy a service's page and forget every widget reference keyed by its name.

        The log tail is stopped first: it writes into the very Text widget about to be
        destroyed, and would otherwise keep a dead service's thread alive for good."""
        self.service_manager.stop_log_tail(name)
        page = self.pages.pop(name, None)
        if page is not None:
            if self.current_page == name:
                self.current_page = None
            page.pack_forget()
            page.destroy()

        self.service_tabs.pop(name, None)
        self.redis_trees.pop(name, None)
        self.redis_count_labels.pop(name, None)
        self.status_dots.pop(name, None)
        for dots in self.dash_rows.values():
            dots.pop(name, None)
        for rows in self.startup_vars.values():
            rows.pop(name, None)

    def _after_services_changed(self, show=None):
        """Persist the config and bring every view of the service list back in sync."""
        self.save_config()
        for app in self.apps:
            self._fill_services_table(app["id"])
        if self.current_app:
            self._build_nav_for_app(self.current_app)
        self._refresh_settings_summary()
        self._fill_settings_apps()  # the per-project service counts it shows have moved

        target = show or self.current_page
        if target not in self.pages:
            target = page_key("home", self.current_app)
        self.show_page(target)

        self._apply_status()
        if self.tray_manager is not None:
            self.tray_manager.refresh_menu()

    def _after_apps_changed(self):
        """Rebuild everything that is drawn per app: the rail, the dashboards, the nav."""
        self.apps = self.service_manager.apps
        self.save_config()

        if self.current_app not in self.service_manager.app_by_id:
            self.current_app = self.apps[0]["id"] if self.apps else None

        self._rebuild_app_rail()
        self._build_app_pages()
        self._fill_settings_apps()
        self._refresh_settings_summary()

        # switch_app repaints the sidebar heading and nav from the (possibly renamed) app;
        # settings then comes back to the front if that's where the edit was made from.
        was_settings = self.current_page == SETTINGS_PAGE
        self.switch_app(self.current_app)
        if was_settings:
            self.show_page(SETTINGS_PAGE)
        if self.tray_manager is not None:
            self.tray_manager.refresh_menu()

    def reload_config(self):
        """Re-read services.json / appsettings.json from disk and rebuild the whole UI.

        Used after the JSON editor writes a file. Every service page is thrown away and
        recreated, because a hand-edit can change a service's type, name or app as freely
        as the form dialogs can."""
        sm = self.service_manager
        # Still the pre-reload service list, which is exactly the set of pages to throw away.
        for name in [n for n in list(self.pages) if n in sm.services]:
            self._drop_service_page(name)
        sm.reload_from_disk()

        self.apps = sm.apps
        self.last_page_by_app.clear()
        if self.current_app not in sm.app_by_id:
            self.current_app = self.apps[0]["id"] if self.apps else None

        self._rebuild_app_rail()
        self._build_app_pages()
        for name in list(sm.services):
            self._add_service_page(name)

        self._sync_settings_widgets()
        was_settings = self.current_page == SETTINGS_PAGE
        self.switch_app(self.current_app)
        if was_settings:
            self.show_page(SETTINGS_PAGE)
        self._apply_status()
        if self.tray_manager is not None:
            self.tray_manager.refresh_menu()

    # --------------------------------------------------------------- sidebar

    def _build_scrollable_nav(self, sidebar):
        """Put the nav items in a vertically-scrollable area; return the frame to fill.

        The scrollbar only appears when the items are taller than the viewport, so short
        lists look unchanged while long ones stay reachable."""
        canvas = tk.Canvas(sidebar, bg=SIDEBAR_BG, highlightthickness=0, borderwidth=0)
        self.nav_canvas = canvas
        scrollbar = ttk.Scrollbar(sidebar, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=SIDEBAR_BG)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window, width=canvas.winfo_width())  # inner tracks canvas width
            overflow = inner.winfo_reqheight() > canvas.winfo_height()
            if overflow and not scrollbar.winfo_ismapped():
                scrollbar.pack(side="right", fill="y")
            elif not overflow and scrollbar.winfo_ismapped():
                scrollbar.pack_forget()

        inner.bind("<Configure>", _sync)
        canvas.bind("<Configure>", _sync)
        canvas.bind("<MouseWheel>", self._on_nav_mousewheel)  # wheel over empty sidebar area
        return inner

    def _on_nav_mousewheel(self, event):
        # Bound per-widget (canvas + every nav row) so the wheel works wherever the pointer
        # is in the sidebar; a no-op when the content already fits.
        self.nav_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _build_nav_for_app(self, app_id):
        """Replace the sidebar contents with the selected app's services."""
        for child in self.nav_frame.winfo_children():
            child.destroy()
        self.nav_items.clear()
        self.status_dots.clear()

        sm = self.service_manager
        app = sm.app_by_id[app_id]
        names = sm.services_for_app(app_id)

        self._add_nav_item(page_key("home", app_id), "Dashboard", with_dot=False)
        if app.get("build"):
            label = (app["build"].get("label") or "Build")
            self._add_nav_item(page_key("build", app_id), label, with_dot=False)

        process_services = [n for n in names
                            if not sm.is_winservice(n) and not sm.is_logview(n) and not sm.is_task(n)]
        win_services = [n for n in names if sm.is_winservice(n)]
        log_views = [n for n in names if sm.is_logview(n)]
        tasks = [n for n in names if sm.is_task(n)]

        if process_services:
            self._add_nav_section("Services")
            for name in process_services:
                self._add_nav_item(name, name, with_dot=True, shared=sm.is_shared(name))
        if win_services:
            self._add_nav_section("Windows services")
            for name in win_services:
                self._add_nav_item(name, name, with_dot=True, shared=sm.is_shared(name))

        # Tasks are grouped by their optional `section` key, so migration commands can sit
        # apart from the general ones.
        if tasks:
            for section in dict.fromkeys(sm.services[n].get("section", "Tasks") for n in tasks):
                self._add_nav_section(section)
                for name in [n for n in tasks if sm.services[n].get("section", "Tasks") == section]:
                    self._add_nav_item(name, name, with_dot=True)

        if log_views:
            self._add_nav_section("Logs")
            for name in log_views:
                self._add_nav_item(name, name, with_dot=False, shared=sm.is_shared(name))

    def _add_nav_section(self, title):
        """A dim divider label separating groups of nav items."""
        label = tk.Label(self.nav_frame, text=title, bg=SIDEBAR_BG, fg=NAV_SECTION_FG,
                         font=(SMALL_FONT, 9), anchor="w")
        label.pack(fill="x", padx=self._px(20), pady=(self._px(12), self._px(2)))
        label.bind("<MouseWheel>", self._on_nav_mousewheel)

    def _start_log_tails(self):
        """Tail any service that has a configured `log_file` (Windows services, log views)."""
        for name in self.service_manager.services:
            if self.service_manager.services[name].get("log_file"):
                self.service_manager.start_log_tail(name, self.append_output)

    def _add_nav_item(self, page_id, text, with_dot, shared=False):
        """Add a clickable sidebar row: selection bar, status lamp, name, shared marker."""
        row = tk.Frame(self.nav_frame, bg=SIDEBAR_BG)
        row.pack(fill="x")
        # Lit in the project's colour on the page in view, so the sidebar says which
        # project you're in as well as which page.
        bar = tk.Frame(row, bg=SIDEBAR_BG, width=self._px(3))
        bar.pack(side="left", fill="y")
        inner = tk.Frame(row, bg=SIDEBAR_BG)
        inner.pack(side="left", fill="x", expand=True)

        # The lamp: a short bar in the state's colour (see _state). Rows without a run
        # state get the same-sized gap so every name lines up.
        lamp = tk.Frame(inner, bg=SIDEBAR_BG, width=self._px(3), height=self._px(14))
        lamp.pack(side="left", padx=(self._px(13), self._px(10)))
        if with_dot:
            self.status_dots[page_id] = lamp

        label = tk.Label(inner, text=text, bg=SIDEBAR_BG, fg=NAV_FG,
                         font=(UI_FONT, 10), anchor="w")
        label.pack(side="left", fill="x", expand=True, pady=self._px(5))

        widgets = [row, inner, label]
        if shared:
            mark = tk.Label(inner, text=SHARED_TEXT, bg=SIDEBAR_BG, fg=SHARED_FG,
                            font=(SMALL_FONT, 8))
            mark.pack(side="right", padx=(0, self._px(12)))
            widgets.append(mark)

        # lamp_color None = unlit: the lamp takes the row's background, so it disappears
        # into a hovered or selected row instead of leaving a dark notch.
        self.nav_items[page_id] = {"widgets": widgets, "text": label, "bar": bar,
                                   "lamp": lamp, "lamp_color": None}

        for w in widgets + [lamp]:
            w.bind("<Button-1>", lambda e, p=page_id: self.show_page(p))
            w.bind("<Enter>", lambda e, p=page_id: self._on_nav_hover(p, True))
            w.bind("<Leave>", lambda e, p=page_id: self._on_nav_hover(p, False))
            w.bind("<MouseWheel>", self._on_nav_mousewheel)

    def show_page(self, page_id):
        if page_id not in self.pages:
            return
        if self.current_page is not None and self.current_page in self.pages:
            self.pages[self.current_page].pack_forget()
        self.pages[page_id].pack(fill="both", expand=True)
        self.current_page = page_id
        # Settings isn't one of the app's pages, so it isn't remembered as the place to
        # return to when the app is selected again.
        if self.current_app and page_id != SETTINGS_PAGE:
            self.last_page_by_app[self.current_app] = page_id
        self._update_nav_highlight(page_id)
        self._update_rail()

    def show_settings(self):
        """Open the settings page (the gear at the foot of the app rail)."""
        # Re-read the file so an edit made outside the app shows up — but only when the
        # editor holds exactly what was last loaded. Comparing the text rather than trusting
        # a dirty flag means a paste made with the mouse can't be silently thrown away.
        if (self.config_editor is not None
                and self.config_editor.get('1.0', 'end-1c') == self.config_editor_baseline):
            self._load_config_editor()
        self._refresh_claude_status()  # it may have been changed from a terminal meanwhile
        self.show_page(SETTINGS_PAGE)

    def _set_nav_bg(self, page_id, color):
        item = self.nav_items[page_id]
        for w in item["widgets"]:
            w.configure(bg=color)
        item["bg"] = color
        if item["lamp_color"] is None:
            item["lamp"].configure(bg=color)

    def _set_lamp(self, page_id, color):
        """Light (or, with None, darken) a sidebar row's status lamp."""
        item = self.nav_items.get(page_id)
        if item is None:
            return
        item["lamp_color"] = color
        set_if_changed(item["lamp"], bg=color or item.get("bg", SIDEBAR_BG))

    def _on_nav_hover(self, page_id, entering):
        if page_id == self.current_page or page_id not in self.nav_items:
            return
        self._set_nav_bg(page_id, NAV_HOVER_BG if entering else SIDEBAR_BG)

    def _update_nav_highlight(self, active):
        accent = self._app_color()
        for page_id, item in self.nav_items.items():
            self._set_nav_bg(page_id, NAV_ACTIVE_BG if page_id == active else SIDEBAR_BG)
            item["text"].configure(fg="#ffffff" if page_id == active else NAV_FG)
            item["bar"].configure(bg=accent if page_id == active else SIDEBAR_BG)

    def _app_color(self, app_id=None):
        """The project's own colour (from its definition), used to mark where you are."""
        app = self.service_manager.app_by_id.get(app_id or self.current_app) or {}
        return app.get("color") or "#8e9299"

    # ----------------------------------------------------------- status dots

    def _is_winservice(self, name):
        return self.service_manager.is_winservice(name)

    def _is_logview(self, name):
        return self.service_manager.is_logview(name)

    def _is_task(self, name):
        return self.service_manager.is_task(name)

    def _is_running(self, name):
        return self.service_manager.is_running(name)

    def _refresh_status(self):
        self._apply_status()
        self.root.after(1000, self._refresh_status)

    def _state(self, name):
        """(label, colour) for a service's live state: the single source every lamp and
        state label reads, so the sidebar, dashboard and page can never disagree.
        A colour of None means the lamp stays unlit."""
        sm = self.service_manager
        if sm.is_logview(name):
            return "", None
        if sm.is_task(name):
            if sm.task_running(name):
                return "Running", RUNNING_COLOR
            code = sm.task_exit_codes.get(name)
            if code is None:
                return "Not run yet", None
            return ("Finished", None) if code == 0 else (f"Failed, exit {code}", FAULT_COLOR)
        if sm.is_winservice(name):
            state = sm.winservice_status.get(name)
            return {"running": ("Running", RUNNING_COLOR),
                    "stopped": ("Stopped", STOPPED_COLOR),
                    "pending": ("Changing state", ATTENTION_COLOR),
                    "not_found": ("Not installed", ATTENTION_COLOR),
                    }.get(state, ("Checking", STOPPED_COLOR))
        if name in sm.starting:
            return "Starting", ATTENTION_COLOR
        if sm.is_running(name):
            return "Running", RUNNING_COLOR
        code = sm.exit_codes.get(name)
        if code:
            return f"Crashed, exit {code}", FAULT_COLOR
        return ("Exited", STOPPED_COLOR) if code == 0 else ("Stopped", STOPPED_COLOR)

    def _detail(self, name):
        """pid and uptime of a running service, for the faint text beside its state."""
        sm = self.service_manager
        if sm.is_winservice(name):
            pid = sm.winservice_pids.get(name) if sm.is_winservice_running(name) else 0
            return f"pid {pid}" if pid else ""
        proc = sm.processes.get(name)
        if not hasattr(proc, "pid") or not sm.is_running(name):
            return ""
        started = sm.started_at.get(name)
        up = f"  up {format_uptime(time.time() - started)}" if started else ""
        return f"pid {proc.pid}{up}"

    def _apply_status(self):
        sm = self.service_manager
        for name in sm.services:
            running = self._is_running(name)
            label, color = self._state(name)
            detail = self._detail(name)

            lamp = LAMP_OFF if color == STOPPED_COLOR else color
            self._set_lamp(name, lamp)  # only the current app's nav has lamps

            for rows in self.dash_rows.values():
                row = rows.get(name)
                if row is not None:
                    set_if_changed(row["lamp"], bg=lamp or LAMP_OFF)
                    set_if_changed(row["state"], text=label, foreground=color or MUTED)
                    set_if_changed(row["detail"], text=detail)
                    self._show_run_controls(row, name, running)

            tab = self.service_tabs.get(name, {})
            pill = tab.get("status_pill")
            if pill is not None:  # live state beside the service page's title
                set_if_changed(pill, text=label, foreground=color or MUTED)
                set_if_changed(tab["status_detail"], text=detail)
            self._show_run_controls(tab, name, running)

        self._update_summary()
        self._update_rail()

    def _update_summary(self):
        """The current app's running count, on its dashboard and under the sidebar title."""
        if not self.current_app:
            return
        names = [n for n in self.service_manager.services_for_app(self.current_app)
                 if not self._is_logview(n) and not self._is_task(n)]
        running = sum(1 for n in names if self._is_running(n))
        crashed = sum(1 for n in names if self._state(n)[1] == FAULT_COLOR)
        text = f"{running} of {len(names)} running" + (f", {crashed} crashed" if crashed else "")
        label = self.summary_labels.get(self.current_app)
        if label is not None:
            set_if_changed(label, text=text)
        if self.brand_sub is not None:
            set_if_changed(self.brand_sub, text=text)

    def _show_run_controls(self, controls, name, running):
        """Show only the actions that make sense now: Start when it's stopped, Stop and
        Restart when it's running (and Run / Cancel for a task). A hidden button can't be
        clicked by mistake, and the one you want is always in the same place."""
        if self._is_task(name):
            show_if(controls.get("run_button"), not running)
            show_if(controls.get("cancel_button"), running)
            return
        starting = name in self.service_manager.starting
        show_if(controls.get("start_button"), not running and not starting)
        show_if(controls.get("stop_button"), running or starting)
        show_if(controls.get("restart_button"), running)

    def _sync_winservice_name(self, name):
        """Persist any edit to a winservice's service-name field before we act on it."""
        if not self._is_winservice(name):
            return
        entry = self.service_tabs.get(name, {}).get("service_entry")
        if entry is not None:
            self.service_manager.services[name]["service_name"] = entry.get().strip()
            self.save_config()

    def _do_start(self, name):
        self._sync_winservice_name(name)
        self.service_manager.start_service(name, self.service_tabs, self.append_output, self.save_config)
        self.root.after(300, self._apply_status)  # reflect new state once the process is up

    def _do_stop(self, name):
        self._sync_winservice_name(name)
        # An explicit Stop on a service's own page is unconditional: no requesting_app, so
        # the shared-service guard doesn't apply. Bulk Stop All is the scoped one.
        self.service_manager.stop_service(name, self.append_output)
        self._apply_status()  # stop is synchronous, so update immediately

    def _do_restart(self, name):
        self._sync_winservice_name(name)
        self.service_manager.restart_service(name, self.service_tabs, self.append_output, self.save_config)
        self.root.after(300, self._apply_status)

    # ------------------------------------------------------------------ logs

    def _make_log(self, parent, height=14):
        """Create a flat dark Text + themed scrollbar; packs itself and returns the Text."""
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True)

        text = tk.Text(
            frame, wrap=tk.WORD, height=height,
            bg=LOG_BG, fg=LOG_FG, insertbackground=LOG_INSERT,
            selectbackground=LOG_SELECT_BG, relief="flat", borderwidth=0,
            highlightthickness=0, font=(MONO_FONT, 10),
            padx=self._px(14), pady=self._px(10),
            # Breathing room between entries so the log reads as separated blocks.
            spacing1=self._px(2), spacing2=self._px(1), spacing3=self._px(4),
        )
        # Colour tags. Created in this order so 'meta' has the highest priority and keeps
        # the bracketed noise dim even on an otherwise red/amber error line.
        text.tag_configure("error", foreground=LOG_ERROR)
        text.tag_configure("warn", foreground=LOG_WARN)
        text.tag_configure("meta", foreground=LOG_META)
        # Indent wrapped continuation lines (lmargin is a tag-only option) so a long
        # entry reads as one indented block instead of clutter.
        text.tag_configure("body", lmargin2=self._px(22))
        # Find matches; raised above the colour tags so a match is visible on any line.
        text.tag_configure("find", background=LOG_FIND_BG)
        text.tag_configure("find_current", background=LOG_FIND_CURRENT, foreground="#ffffff")
        text.tag_raise("find")
        text.tag_raise("find_current")

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scrollbar.pack(side=tk.RIGHT, fill='y')
        text.pack(side=tk.LEFT, fill='both', expand=True)

        # Whether the log sticks to its newest line. Scrolling up to read something turns
        # it off until you scroll back to the bottom. A log on a page that isn't showing
        # isn't scrolled at all (see() is the expensive part of appending); it catches up
        # once its page is shown.
        text.follow = True
        text.on_follow = None  # set by _add_log_tools to show / hide "Jump to latest"
        text.find_pattern = ""

        def on_scroll(first, last):
            scrollbar.set(first, last)
            if text.winfo_ismapped():
                following = float(last) >= 0.999
                if following != text.follow:
                    text.follow = following
                    if text.on_follow:
                        text.on_follow(following)

        text.configure(yscrollcommand=on_scroll)
        text.bind("<Map>", lambda e: text.follow and text.see("end"), add="+")
        return text

    def _add_log_tools(self, bar, text, page_id, clear):
        """Find, Jump to latest and Clear for a log, packed right-aligned into `bar`.

        Find highlights every match as you type; Enter / Shift+Enter step through them and
        Escape clears. Ctrl+F jumps here from anywhere on the page."""
        ttk.Button(bar, text="Clear", style="Small.TButton", command=clear).pack(side='right')
        jump = ttk.Button(bar, text="Jump to latest", style="Small.Accent.TButton",
                          command=lambda: (text.see("end"), setattr(text, "follow", True),
                                           jump.pack_forget()))

        def on_follow(following):
            if following:
                jump.pack_forget()
            elif not jump.winfo_ismapped():
                jump.pack(side='right', padx=(0, self._px(8)), before=find_box)

        text.on_follow = on_follow

        find_box = ttk.Frame(bar)
        find_box.pack(side='right', padx=(0, self._px(12)))
        count = ttk.Label(find_box, text="", style="Faint.TLabel", width=12, anchor='e')
        count.pack(side='left', padx=(0, self._px(8)))
        entry = ttk.Entry(find_box, width=24, font=(MONO_FONT, 9))
        entry.pack(side='left')
        placeholder = "Find in output"

        def show_placeholder(_e=None):
            if not entry.get():
                entry.insert(0, placeholder)
                entry.configure(foreground=FAINT)

        def hide_placeholder(_e=None):
            if entry.get() == placeholder and str(entry.cget("foreground")) == FAINT:
                entry.delete(0, "end")
                entry.configure(foreground=LOG_FG)

        def query():
            value = entry.get()
            return "" if value == placeholder and str(entry.cget("foreground")) == FAINT else value

        def update_count():
            n = len(text.tag_ranges("find")) // 2
            if not text.find_pattern:
                count.configure(text="")
            else:
                cur = text.tag_ranges("find_current")
                if n and cur:
                    pos = [str(r) for r in text.tag_ranges("find")[::2]].index(str(cur[0])) + 1
                    count.configure(text=f"{pos} of {n}")
                else:
                    count.configure(text=f"{n} match{'es' if n != 1 else ''}" if n else "No matches")

        def search(_e=None):
            pattern = query()
            if pattern == text.find_pattern:
                return
            text.find_pattern = pattern
            text.tag_remove("find", "1.0", "end")
            text.tag_remove("find_current", "1.0", "end")
            if pattern:
                self._highlight_matches(text, "1.0")
            update_count()

        def step(backwards=False):
            search()
            ranges = text.tag_ranges("find")
            if not ranges:
                return "break"
            cur = text.tag_ranges("find_current")
            starts = ranges[::2]
            if cur:
                text.tag_remove("find_current", "1.0", "end")
                idx = [str(r) for r in starts].index(str(cur[0]))
                idx = (idx - 1) % len(starts) if backwards else (idx + 1) % len(starts)
            else:
                idx = len(starts) - 1 if backwards else 0
            start = starts[idx]
            end = text.index(f"{start}+{len(text.find_pattern)}c")
            text.tag_add("find_current", start, end)
            text.see(start)
            update_count()
            return "break"

        def clear_find(_e=None):
            entry.delete(0, "end")
            search()
            text.focus_set()
            return "break"

        entry.bind("<FocusIn>", hide_placeholder)
        entry.bind("<FocusOut>", show_placeholder)
        entry.bind("<KeyRelease>", lambda e: e.keysym in ("Return", "Escape") or search())
        entry.bind("<Return>", lambda e: step())
        entry.bind("<Shift-Return>", lambda e: step(backwards=True))
        entry.bind("<Escape>", clear_find)
        text.find_count = update_count
        show_placeholder()
        self.find_entries[page_id] = entry

    def _highlight_matches(self, text, start):
        """Tag every case-insensitive match of the log's find pattern from `start` on."""
        pattern = text.find_pattern
        if not pattern:
            return
        length = tk.IntVar()
        pos = start
        while True:
            pos = text.search(pattern, pos, stopindex="end", nocase=True, count=length)
            if not pos or not length.get():
                break
            end = f"{pos}+{length.get()}c"
            text.tag_add("find", pos, end)
            pos = end

    def _focus_find(self, _event=None):
        entry = self.find_entries.get(self.current_page)
        if entry is not None:
            entry.focus_set()
            entry.select_range(0, "end")
            return "break"

    def _append_log(self, widget, line):
        """Append one entry to a log Text (see _append_lines)."""
        self._append_lines(widget, [line])

    def _append_lines(self, widget, entries):
        """Append entries to a log Text, colour-coding each line: bracketed metadata dimmed,
        and the whole line tinted amber/red if it looks like a warning/error. Then trim the
        log to LOG_MAX_LINES and scroll to the end once, if it's following."""
        mapped = widget.winfo_ismapped()
        if mapped:
            widget.follow = widget.yview()[1] >= 0.999
        lineno = int(widget.index("end-1c").split(".")[0])  # the next line to be written
        first = lineno
        for entry in entries:
            for line in str(entry).split("\n"):
                low = line.lower()
                tags = ["body"]  # wrapped-line indent
                if any(w in low for w in LOG_ERROR_WORDS):
                    tags.append("error")
                elif any(w in low for w in LOG_WARN_WORDS):
                    tags.append("warn")
                widget.insert("end", line + "\n", tuple(tags))
                for m in LOG_MATCH.finditer(line):
                    widget.tag_add("meta", f"{lineno}.{m.start()}", f"{lineno}.{m.end()}")
                lineno += 1
        if getattr(widget, "find_pattern", ""):  # keep an open search covering new output
            self._highlight_matches(widget, f"{first}.0")
        excess = lineno - 1 - LOG_MAX_LINES
        if excess > LOG_TRIM_SLACK:
            widget.delete("1.0", f"{excess + 1}.0")
        if getattr(widget, "find_pattern", ""):
            widget.find_count()
        if mapped and widget.follow:
            widget.see("end")

    # -------------------------------------------------------- layout helpers

    def _make_scroll_area(self, parent):
        """Return an inner frame inside a vertical scroll area filling `parent`.

        Lets a page grow taller than the window without clipping; the scrollbar only
        appears when the content overflows. Wheel scrolling is wired up by the caller via
        _bind_wheel_recursive once the content is built."""
        canvas = tk.Canvas(parent, bg=PAGE_BG, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)

        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window, width=canvas.winfo_width())
            overflow = inner.winfo_reqheight() > canvas.winfo_height()
            if overflow and not scrollbar.winfo_ismapped():
                scrollbar.pack(side="right", fill="y")
            elif not overflow and scrollbar.winfo_ismapped():
                scrollbar.pack_forget()

        inner.bind("<Configure>", _sync)
        canvas.bind("<Configure>", _sync)
        canvas._wheel = lambda e: canvas.yview_scroll(int(-e.delta / 120), "units")
        canvas.bind("<MouseWheel>", canvas._wheel)
        inner._scroll_canvas = canvas  # so _bind_wheel_recursive can find the handler
        return inner

    def _bind_wheel_recursive(self, widget):
        """Route the mouse wheel to the page's scroll canvas for every child widget, so
        scrolling works wherever the pointer is — except over log boxes, which keep their
        own wheel behaviour."""
        canvas = getattr(widget, "_scroll_canvas", None)
        if canvas is None:
            return

        def apply(w):
            if isinstance(w, tk.Text):
                return  # let the log scroll itself
            w.bind("<MouseWheel>", canvas._wheel)
            for child in w.winfo_children():
                apply(child)

        apply(widget)

    def _card(self, parent, title=None):
        """A raised 'card' surface for grouping a section; returns its body frame."""
        card = ttk.Frame(parent, style="Card.TFrame", padding=self._px(16))
        card.pack(fill='x', pady=(0, self._px(14)))
        if title:
            ttk.Label(card, text=title, style="Section.TLabel").pack(anchor='w', pady=(0, self._px(10)))
        return card

    # ------------------------------------------------------------ dashboard

    def _page_rule(self, parent, color):
        """The 2px rule under a page header, in the project's colour: where you are."""
        tk.Frame(parent, height=self._px(2), bg=color).pack(fill="x", pady=(0, self._px(16)))

    def _link(self, parent, text, command, danger=False):
        """A quiet text button for secondary actions (Edit, Remove), kept visually apart
        from the run controls. Focusable, and Enter/Space activate it like a button."""
        hover = FAULT_COLOR if danger else "#ffffff"
        link = tk.Label(parent, text=text, bg=PAGE_BG, fg=MUTED, font=(UI_FONT, 10),
                        cursor="hand2", takefocus=True, padx=self._px(6), pady=self._px(4),
                        highlightthickness=1, highlightbackground=PAGE_BG,
                        highlightcolor=MUTED)
        link.bind("<Enter>", lambda e: link.configure(fg=hover))
        link.bind("<Leave>", lambda e: link.configure(fg=MUTED))
        for seq in ("<Button-1>", "<Return>", "<space>"):
            link.bind(seq, lambda e: command())
        return link

    def _tooltip_on(self, widget, text):
        widget.bind("<Enter>", lambda e: self._show_tooltip(e, text), add="+")
        widget.bind("<Leave>", lambda e: self._hide_tooltip(), add="+")

    def _build_home_page(self, page, app_id):
        """Per-app dashboard: the switchboard of services, bulk controls and activity."""
        app = self.service_manager.app_by_id[app_id]
        scroll = self._make_scroll_area(page)
        self.home_scrolls[app_id] = scroll
        container = ttk.Frame(scroll, padding=(self._px(28), self._px(22)))
        container.pack(expand=True, fill='both')

        # --- Header: project, its live count, and the bulk controls ---
        header = ttk.Frame(container)
        header.pack(fill='x', pady=(0, self._px(12)))
        titles = ttk.Frame(header)
        titles.pack(side='left', anchor='w')
        ttk.Label(titles, text=app.get("name", app_id), style="Title.TLabel").pack(anchor='w')
        summary = ttk.Label(titles, text="", style="Subtitle.TLabel")
        summary.pack(anchor='w', pady=(self._px(2), 0))
        self.summary_labels[app_id] = summary

        bulk = ttk.Frame(header)
        bulk.pack(side='right', anchor='s')
        ttk.Button(bulk, text="Start all", style="Accent.TButton",
                   command=lambda: self.start_all(app_id)).pack(side=tk.LEFT, padx=(0, self._px(8)))
        ttk.Button(bulk, text="Restart all",
                   command=lambda: self.restart_all(app_id)).pack(side=tk.LEFT, padx=(0, self._px(8)))
        stop_all = ttk.Button(bulk, text="Stop all", command=lambda: self.stop_all(app_id))
        stop_all.pack(side=tk.LEFT)
        self._tooltip_on(stop_all, "Shared services stay up while another project still uses them.")
        self._page_rule(container, self._app_color(app_id))

        # --- Services: one row per service, with its state and its own controls ---
        services = ttk.Frame(container, style="Card.TFrame", padding=(self._px(18), self._px(14)))
        services.pack(fill='x', pady=(0, self._px(16)))
        head = ttk.Frame(services)
        head.pack(fill='x', pady=(0, self._px(8)))
        ttk.Label(head, text="Services", style="Section.TLabel").pack(side='left')
        ttk.Button(head, text="Add service",
                   command=lambda: self.add_service_dialog(app_id)).pack(side='right')
        self._build_services_table(services, app_id)

        # --- Activity card ---
        act = ttk.Frame(container, style="Card.TFrame", padding=(self._px(18), self._px(14)))
        act.pack(fill='both', expand=True)
        act_head = ttk.Frame(act)
        act_head.pack(fill='x', pady=(0, self._px(8)))
        ttk.Label(act_head, text="Activity", style="Section.TLabel").pack(side='left')
        ttk.Button(act_head, text="Clear",
                   command=lambda: self._clear_home_log(app_id)).pack(side='right')
        self.home_logs[app_id] = self._make_log(act, height=6)

        # Wheel-scroll anywhere on the page (built after all widgets exist).
        self._bind_wheel_recursive(scroll)

    def _clear_home_log(self, app_id):
        log = self.home_logs.get(app_id)
        if log is not None:
            log.delete('1.0', 'end')

    def _build_services_table(self, parent, app_id):
        """Create the dashboard services grid; its rows are (re)filled separately so adding
        or removing a service doesn't mean rebuilding the whole dashboard."""
        box = ttk.Frame(parent)
        box.pack(fill='x')
        self.services_tables[app_id] = box
        self._fill_services_table(app_id)

    def _fill_services_table(self, app_id):
        """Rows of: lamp | service | kind | state | pid, uptime | controls | the two
        startup toggles. The state is the loudest thing in a row; the startup toggles,
        which are configuration, sit quietly at the end."""
        box = self.services_tables.get(app_id)
        if box is None:
            return
        for child in box.winfo_children():
            child.destroy()
        self.dash_rows[app_id] = {}
        self.startup_vars[app_id] = {}

        sm = self.service_manager
        # Log-only entries and one-shot tasks have nothing to start.
        rows = [n for n in sm.services_for_app(app_id)
                if not self._is_logview(n) and not self._is_task(n)]

        if not rows:
            ttk.Label(box, text="No services yet. Add one to start it from here.",
                      style="Subtitle.TLabel").grid(row=0, column=0, sticky='w')
            return

        box.columnconfigure(4, weight=1)  # the gap that pushes controls to the right edge
        pad = self._px(5)
        for col, text in ((1, "Service"), (3, "State"), (6, "In Start all"), (7, "Auto-start")):
            ttk.Label(box, text=text, style="Faint.TLabel").grid(
                row=0, column=col, sticky='w' if col < 6 else '', padx=(0, self._px(18)),
                pady=(0, self._px(4)))

        for i, name in enumerate(rows, start=1):
            info = sm.services[name]

            lamp = tk.Frame(box, bg=LAMP_OFF, width=self._px(4), height=self._px(22))
            lamp.grid(row=i, column=0, sticky='w', padx=(0, self._px(12)), pady=pad)

            # Clicking a name opens that service's page.
            name_lbl = ttk.Label(box, text=name, style="Row.TLabel", cursor="hand2")
            name_lbl.grid(row=i, column=1, sticky='w', padx=(0, self._px(18)), pady=pad)
            name_lbl.bind("<Button-1>", lambda e, n=name: self.show_page(n))
            kind = service_kind_label(info)
            if sm.is_shared(name):
                kind += ", shared"
            ttk.Label(box, text=kind, style="Faint.TLabel").grid(
                row=i, column=2, sticky='w', padx=(0, self._px(18)), pady=pad)

            state = ttk.Label(box, text="", style="State.TLabel")
            state.grid(row=i, column=3, sticky='w', padx=(0, self._px(14)), pady=pad)
            detail = ttk.Label(box, text="", style="FaintMono.TLabel")
            detail.grid(row=i, column=4, sticky='w', pady=pad)

            # Start, or Stop + Restart, in a fixed slot so the button you want doesn't move.
            controls = ttk.Frame(box)
            controls.grid(row=i, column=5, sticky='e', padx=(self._px(12), self._px(22)), pady=pad)
            start = ttk.Button(controls, text="Start", style="Small.TButton", width=7,
                               command=lambda n=name: self._do_start(n))
            stop = ttk.Button(controls, text="Stop", style="Small.TButton", width=7,
                              command=lambda n=name: self._do_stop(n))
            restart = ttk.Button(controls, text="Restart", style="Small.TButton", width=7,
                                 command=lambda n=name: self._do_restart(n))
            start.grid(row=0, column=0)
            stop.grid(row=0, column=1)
            restart.grid(row=0, column=2, padx=(self._px(6), 0))

            sa_var = tk.BooleanVar(value=sm.includes_in_start_all(name, app_id))
            ttk.Checkbutton(box, variable=sa_var,
                            command=lambda n=name, a=app_id: self._on_toggle_start_all(n, a)).grid(
                                row=i, column=6, padx=(0, self._px(18)), pady=pad)

            as_var = tk.BooleanVar(value=info.get("autostart", False))
            ttk.Checkbutton(box, variable=as_var,
                            command=lambda n=name, a=app_id: self._on_toggle_autostart(n, a)).grid(
                                row=i, column=7, padx=(0, self._px(18)), pady=pad)

            self.startup_vars[app_id][name] = (sa_var, as_var)
            self.dash_rows[app_id][name] = {  # updated live by _apply_status
                "lamp": lamp, "state": state, "detail": detail,
                "start_button": start, "stop_button": stop, "restart_button": restart}

        # Rows built after the page was first assembled need the wheel handler wiring up
        # again, or scrolling stalls whenever the pointer is over the new grid.
        self._bind_wheel_recursive(self.home_scrolls.get(app_id))

    def _on_toggle_start_all(self, name, app_id):
        value = self.startup_vars[app_id][name][0].get()
        self.service_manager.set_include_in_start_all(name, app_id, value)
        self.save_config()

    def _on_toggle_autostart(self, name, app_id):
        value = self.startup_vars[app_id][name][1].get()
        self.service_manager.services[name]["autostart"] = value
        # A shared service appears in more than one dashboard; keep the other checkbox in
        # sync so the two views can't disagree about a single stored flag.
        for other_app, rows in self.startup_vars.items():
            if other_app != app_id and name in rows:
                rows[name][1].set(value)
        self.save_config()

    def autostart_services(self):
        """Start every service flagged for auto-start (called shortly after launch)."""
        sm = self.service_manager
        if not sm.settings.get("autostart_enabled", True):
            return  # master switch on the settings page
        autos = [n for n in sm.services
                 if sm.services[n].get("autostart", False)
                 and not self._is_logview(n) and not self._is_task(n)]
        if not autos:
            return
        self.home_log("Auto-starting services on launch...")
        for name in autos:
            # A Windows service that's already running needs no action (and starting it
            # would pop a pointless UAC prompt), so skip those.
            if self._is_winservice(name) and sm.is_winservice_running(name):
                self.home_log(f"  - {name}: already running")
                continue
            sm.start_service(name, self.service_tabs, self.append_output, self.save_config)
            self.home_log(f"  - {name}: auto-start requested")
        self.home_log("Auto-start complete.\n")
        self.root.after(300, self._apply_status)

    # ----------------------------------------------------------- build page

    def _build_build_page(self, page, app_id):
        """Front-end build page for an app that defines a `build` block."""
        app = self.service_manager.app_by_id[app_id]
        cfg = app.get("build", {})
        label = cfg.get("label", "Build")

        container = ttk.Frame(page, padding=self._px(24))
        container.pack(expand=True, fill='both')

        ttk.Label(container, text=label, style="Header.TLabel").pack(anchor='w', pady=(0, self._px(8)))
        ttk.Label(container, text="Rebuild the front-end after pulling new changes.",
                  style="Subtitle.TLabel").pack(anchor='w', pady=(0, self._px(12)))

        form = ttk.Frame(container)
        form.pack(fill='x', pady=(0, self._px(8)))
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Project path").grid(row=0, column=0, sticky='w',
                                                  padx=(0, self._px(10)), pady=self._px(4))
        path_entry = ttk.Entry(form)
        path_entry.insert(0, cfg.get("path", ""))
        path_entry.grid(row=0, column=1, sticky='ew', pady=self._px(4))

        ttk.Label(form, text="Build command").grid(row=1, column=0, sticky='w',
                                                   padx=(0, self._px(10)), pady=self._px(4))
        cmd_entry = ttk.Entry(form)
        cmd_entry.insert(0, cfg.get("command", "npm run build"))
        cmd_entry.grid(row=1, column=1, sticky='ew', pady=self._px(4))
        self.build_entries[app_id] = (path_entry, cmd_entry)

        actions = ttk.Frame(container)
        actions.pack(fill='x', pady=(self._px(4), self._px(8)))
        button = ttk.Button(actions, text=f"Rebuild {label}", style="Accent.TButton",
                            command=lambda: self.run_build(app_id))
        button.pack(side=tk.LEFT, padx=(0, self._px(8)))
        self.build_buttons[app_id] = button
        ttk.Button(actions, text="Save Settings",
                   command=lambda: self._save_build_settings(app_id)).pack(side=tk.LEFT)

        ttk.Label(container, text="Build output", style="Section.TLabel").pack(
            anchor='w', pady=(self._px(8), self._px(6)))
        self.build_logs[app_id] = self._make_log(container, height=16)

    def _save_build_settings(self, app_id):
        app = self.service_manager.app_by_id[app_id]
        cfg = app.setdefault("build", {})
        path_entry, cmd_entry = self.build_entries[app_id]
        cfg["path"] = path_entry.get().strip()
        cfg["command"] = cmd_entry.get().strip() or "npm run build"
        self.save_config()  # app definitions live in services.json alongside the services
        self._build_log(app_id, "Settings saved.")

    def _build_log(self, app_id, message):
        if self.build_logs.get(app_id) is not None:
            self.root.after(0, lambda: self._append_log(self.build_logs[app_id], message))

    def run_build(self, app_id=None, *args):
        """Persist the path/command, switch to the build page, and run the build."""
        app_id = app_id or self.current_app
        if app_id not in self.build_logs:
            return
        self._save_build_settings(app_id)
        if app_id != self.current_app:
            self.switch_app(app_id)
        self.show_page(page_key("build", app_id))
        button = self.build_buttons.get(app_id)
        if button:
            button.configure(state="disabled")
        self.service_manager.run_build(
            app_id,
            lambda line: self._build_log(app_id, line),
            on_done=lambda ok: self._on_build_done(app_id, ok),
        )

    def _on_build_done(self, app_id, success):
        button = self.build_buttons.get(app_id)
        if button:
            self.root.after(0, lambda: button.configure(state="normal"))

    # -------------------------------------------------------- service pages

    def _build_service_page(self, page, service_name, service_info):
        sm = self.service_manager
        is_task = sm.is_task(service_name)
        is_logview = self._is_logview(service_name)

        container = ttk.Frame(page, padding=(self._px(28), self._px(22)))
        container.pack(expand=True, fill='both')
        self.service_tabs[service_name] = {}
        controls = {}

        # --- Header: name, live state, then the run controls; Edit / Remove at the end ---
        header = ttk.Frame(container)
        header.pack(fill='x')
        ttk.Label(header, text=service_name, style="Header.TLabel").pack(side='left', anchor='w')
        if not is_logview:  # log-only pages have no run state
            status_pill = ttk.Label(header, text="", style="State.TLabel")
            status_pill.pack(side='left', padx=(self._px(16), 0), pady=(self._px(4), 0))
            status_detail = ttk.Label(header, text="", style="FaintMono.TLabel")
            status_detail.pack(side='left', padx=(self._px(10), 0), pady=(self._px(5), 0))
            controls.update({"status_pill": status_pill, "status_detail": status_detail})

        remove = self._link(header, "Remove", lambda: self.remove_service(service_name), danger=True)
        remove.pack(side='right')
        self._link(header, "Edit", lambda: self.edit_service_dialog(service_name)).pack(
            side='right', padx=(self._px(14), self._px(2)))

        # Start, or Stop + Restart (Run / Cancel for a task), in one fixed slot.
        run = ttk.Frame(header)
        run.pack(side='right')
        if is_task:
            run_button = ttk.Button(run, text="Run", style="Accent.TButton", width=8,
                                    command=lambda: self._do_run_task(service_name))
            cancel_button = ttk.Button(run, text="Cancel", width=8,
                                       command=lambda: self.service_manager.stop_task(
                                           service_name, self.append_output))
            run_button.grid(row=0, column=0)
            cancel_button.grid(row=0, column=1)
            controls.update({"run_button": run_button, "cancel_button": cancel_button})
        elif not is_logview:
            start_button = ttk.Button(run, text="Start", style="Accent.TButton", width=8,
                                      command=lambda: self._do_start(service_name))
            stop_button = ttk.Button(run, text="Stop", width=8,
                                     command=lambda: self._do_stop(service_name))
            restart_button = ttk.Button(run, text="Restart", width=8,
                                        command=lambda: self._do_restart(service_name))
            start_button.grid(row=0, column=0)
            stop_button.grid(row=0, column=1)
            restart_button.grid(row=0, column=2, padx=(self._px(8), 0))
            controls.update({"start_button": start_button, "stop_button": stop_button,
                             "restart_button": restart_button})

        # --- What it runs, in one quiet line; changing it is what Edit is for ---
        what = ttk.Frame(container)
        what.pack(fill='x', anchor='w', pady=(self._px(8), self._px(12)))
        what.columnconfigure(1, weight=1)
        for row, (label, value, mono) in enumerate(self._describe_service(service_name)):
            ttk.Label(what, text=label, style="Faint.TLabel").grid(
                row=row, column=0, sticky='nw', padx=(0, self._px(12)), pady=(0, self._px(2)))
            cell = ttk.Label(what, text=value, style="Mono.TLabel" if mono else "Subtitle.TLabel",
                             justify="left")
            cell.grid(row=row, column=1, sticky='w', pady=(0, self._px(2)))
            cell.bind("<Configure>", lambda e, c=cell: c.configure(wraplength=max(e.width, 200)))
        if sm.is_shared(service_name):
            others = ", ".join(sm.app_by_id[a].get("name", a) for a in sm.apps_of(service_name))
            rule = SHARED_RULE_COLOR
            ttk.Label(container, text=f"Shared by {others}", style="Faint.TLabel").pack(
                anchor='w', pady=(0, self._px(8)))
        else:
            rule = self._app_color((sm.primary_app(service_name) or {}).get("id"))
        self._page_rule(container, rule)

        # --- Redis "Lists" card (e.g. for Memurai): see / delete / empty lists ---
        if service_info.get("redis"):
            self._build_redis_panel(container, service_name)

        # --- Output: the reason you opened the page, so it gets the rest of it ---
        out_card = ttk.Frame(container, style="Card.TFrame", padding=(self._px(16), self._px(12)))
        out_card.pack(fill='both', expand=True)
        out_head = ttk.Frame(out_card)
        out_head.pack(fill='x', pady=(0, self._px(8)))
        ttk.Label(out_head, text="Output", style="Section.TLabel").pack(side='left')
        output_area = self._make_log(out_card, height=16)
        self._add_log_tools(out_head, output_area, service_name,
                            lambda: self.service_manager.clear_log(service_name, self.service_tabs))

        # An interactive task (createsuperuser, makemigrations questions) needs a way to
        # answer prompts, since there's no console attached to the child process.
        if is_task and service_info.get("interactive"):
            reply = ttk.Frame(out_card)
            reply.pack(fill='x', pady=(self._px(10), 0))
            ttk.Label(reply, text="Reply").pack(side=tk.LEFT, padx=(0, self._px(8)))
            input_entry = ttk.Entry(reply, font=(MONO_FONT, 10))
            input_entry.pack(side=tk.LEFT, fill='x', expand=True, padx=(0, self._px(8)))
            input_entry.bind("<Return>", lambda e: self._send_task_input(service_name))
            ttk.Button(reply, text="Send",
                       command=lambda: self._send_task_input(service_name)).pack(side=tk.LEFT)
            controls["input_entry"] = input_entry

        controls["output_area"] = output_area
        self.service_tabs[service_name].update(controls)

    def _describe_service(self, name):
        """(label, value, is_code) rows saying what a service runs, ${tokens} expanded."""
        sm = self.service_manager
        info = sm.services[name]
        kind = service_type_of(info)
        if kind in ("process", "task"):
            rows = [("Runs", " ".join(sm.resolve(name, "command", [])), True)]
            directory = sm.resolve(name, "dir", "")
            return rows + [("In", directory, True)] if directory else rows
        if kind == "cron":
            return [("Calls", info.get("url", ""), True),
                    ("Every", f"{info.get('interval', 60)} seconds", False)]
        if kind == "winservice":
            return [("Service", info.get("service_name", ""), True),
                    ("Note", "Starting and stopping it asks for administrator rights.", False)]
        return [("Tails", sm.resolve(name, "log_file", ""), True)]

    # ------------------------------------------------- adding/removing services

    def add_service_dialog(self, app_id=None):
        """Open the editor on a blank service, pre-assigned to the app in view."""
        ServiceDialog(self, preselect_app=app_id or self.current_app)

    def edit_service_dialog(self, name):
        if name in self.service_manager.services:
            ServiceDialog(self, name=name)

    def commit_service(self, old_name, new_name, info):
        """Apply the editor's result: create or replace the entry, then rebuild its page."""
        sm = self.service_manager
        # Everything this app tracks about a running service is keyed by its name, so a
        # rename can't carry the process with it — say so rather than letting it look like
        # the service stopped on its own.
        stopped_by_rename = (old_name is not None and old_name != new_name
                             and not sm.is_winservice(old_name) and sm.is_running(old_name))

        if old_name is None:
            sm.add_service(new_name, info)
        else:
            self._drop_service_page(old_name)
            sm.update_service(old_name, new_name, info)
        self._add_service_page(new_name)
        self._after_services_changed(show=new_name)

        self.home_log(f"{'Updated' if old_name else 'Added'} service '{new_name}'.")
        if stopped_by_rename:
            self.home_log(f"  - '{old_name}' was stopped as part of the rename.")

    def remove_service(self, name):
        """Delete a service after confirming — stopping it first if it's still running."""
        if name not in self.service_manager.services:
            return
        if not messagebox.askyesno("Remove service",
                                   f"Remove '{name}' from the configuration?"
                                   f"{self._removal_detail(name)}",
                                   parent=self.root):
            return
        self.remove_service_now(name)

    def _removal_detail(self, name):
        """What else removing a service does, for the confirmation."""
        sm = self.service_manager
        detail = ""
        if sm.is_shared(name):
            others = ", ".join(sm.app_by_id[a].get("name", a) for a in sm.apps_of(name))
            detail = f"\n\nIt is shared with {others} and will disappear from both."
        if sm.is_running(name) and not sm.is_winservice(name):
            detail += "\n\nIt is running and will be stopped first."
        if sm.is_winservice(name):
            detail += ("\n\nThe Windows service itself is left alone — only this app's entry "
                       "for it is removed.")
        return detail

    def remove_service_now(self, name):
        """Remove a service without asking (the caller has confirmed)."""
        self._drop_service_page(name)
        self.service_manager.remove_service(name)
        self._after_services_changed(show=page_key("home", self.current_app))
        self.home_log(f"Removed service '{name}'.")

    def ask_to_remove(self, name, requester, on_answer):
        """Ask in the window whether `requester` (e.g. Claude Code) may remove a service.

        Doesn't block: on_answer(True/False) is called once the user picks, and the returned
        dialog can be closed by the caller (counts as No) if nobody answers in time. The
        window is brought forward first, since it may be minimised to the tray."""
        if self.tray_manager is not None and not self.root.winfo_viewable():
            self.tray_manager.restore_window()
        self.root.deiconify()
        self.root.lift()
        return ConfirmDialog(
            self, "Remove service",
            f"{requester} wants to remove '{name}' from the configuration.",
            self._removal_detail(name).strip(), confirm="Remove", on_answer=on_answer)

    def restore_default_services(self):
        """Re-add anything shipped by default that isn't in the config any more.

        Services, and also a project's `build` block: a config written before a project
        gained a build page has no way of growing one otherwise short of the project
        editor, and a missing default is exactly what this button is for."""
        sm = self.service_manager
        defaults = config_manager.default_services()
        missing = {n: i for n, i in defaults.items() if n not in sm.services}

        # Only offer entries whose app still exists, or they'd land in nobody's sidebar.
        known = set(sm.app_by_id)
        skipped = [n for n, i in missing.items()
                   if not known.intersection(i["app"] if isinstance(i["app"], list) else [i["app"]])]
        for name in skipped:
            missing.pop(name)

        missing_builds = {a["id"]: a["build"] for a in config_manager.default_apps()
                          if a.get("build") and a["id"] in known
                          and not sm.app_by_id[a["id"]].get("build")}

        if not missing and not missing_builds:
            messagebox.showinfo("Restore defaults",
                                "Everything this app ships with is already in the "
                                "configuration.",
                                parent=self.root)
            return

        lines = [f"  • {n}" for n in missing]
        lines += [f"  • {sm.app_by_id[a].get('name', a)}: "
                  f"{b.get('label', 'Build')} build page" for a, b in missing_builds.items()]
        listing = "\n".join(lines)
        if not messagebox.askyesno("Restore defaults",
                                   f"Add {len(lines)} missing default(s)?\n\n{listing}\n\n"
                                   "Entries already in the configuration are left untouched.",
                                   parent=self.root):
            return

        for name, info in missing.items():
            sm.add_service(name, info)
            self._add_service_page(name)
        if missing:
            self._after_services_changed()

        for app_id, build in missing_builds.items():
            sm.app_by_id[app_id]["build"] = build
        if missing_builds:
            self._after_apps_changed()  # the build page and its nav item are drawn per app

        self.home_log(f"Restored {len(lines)} default(s).")

    def run_task(self, name):
        """Run a task and bring its page up — the entry point used by the tray menu."""
        apps = self.service_manager.apps_of(name)
        if apps and apps[0] != self.current_app:
            self.switch_app(apps[0])
        self.show_page(name)
        self._do_run_task(name)

    def toggle_service(self, name):
        """Start or stop a service from the tray. Unconditional, like the page's own Stop
        button — the shared-service guard applies to bulk Stop All, not a deliberate click."""
        if self._is_running(name):
            self._do_stop(name)
        else:
            self._do_start(name)

    def _do_run_task(self, name):
        """Persist any edits on the task's page, then run it once."""
        tab = self.service_tabs.get(name, {})
        info = self.service_manager.services[name]
        if tab.get("dir_entry") is not None:
            info["dir"] = tab["dir_entry"].get()
        if tab.get("command_entry") is not None:
            typed = tab["command_entry"].get().strip()
            if typed:
                info["command"] = typed.split()
        self.save_config()
        self.service_manager.run_task(name, self.append_output,
                                      on_done=lambda code: self.root.after(0, self._apply_status))
        self.root.after(200, self._apply_status)

    def _send_task_input(self, name):
        entry = self.service_tabs.get(name, {}).get("input_entry")
        if entry is None:
            return
        text = entry.get()
        entry.delete(0, "end")
        self.service_manager.send_task_input(name, text, self.append_output)

    # ------------------------------------------------------- Redis Lists panel

    def _redis_cfg(self, name):
        cfg = self.service_manager.services.get(name, {}).get("redis", {}) or {}
        return cfg.get("host", "127.0.0.1"), int(cfg.get("port", 6379)), cfg.get("password")

    def _build_redis_panel(self, container, name):
        """A panel to view Redis lists in this datastore and delete / empty them."""
        card = ttk.Frame(container, style="Card.TFrame", padding=self._px(16))
        card.pack(fill='x', pady=(0, self._px(14)))

        head = ttk.Frame(card)
        head.pack(fill='x', pady=(0, self._px(10)))
        ttk.Label(head, text="Redis Lists", style="Section.TLabel").pack(side='left')
        count = ttk.Label(head, text="", style="Subtitle.TLabel")
        count.pack(side='left', padx=(self._px(10), 0))
        self.redis_count_labels[name] = count

        btns = ttk.Frame(card)
        btns.pack(fill='x', pady=(0, self._px(10)))
        ttk.Button(btns, text="Refresh", style="Accent.TButton",
                   command=lambda: self._redis_refresh(name)).pack(side='left', padx=(0, self._px(8)))
        ttk.Button(btns, text="Delete Selected",
                   command=lambda: self._redis_delete_selected(name)).pack(side='left', padx=(0, self._px(8)))
        ttk.Button(btns, text="Empty All Lists",
                   command=lambda: self._redis_empty_all(name)).pack(side='left')

        tree_wrap = ttk.Frame(card)
        tree_wrap.pack(fill='x')
        tree = ttk.Treeview(tree_wrap, columns=("key", "len"), show="headings",
                            height=8, selectmode="extended")
        tree.heading("key", text="List key")
        tree.heading("len", text="Length")
        tree.column("key", anchor="w", width=self._px(380))
        tree.column("len", anchor="e", width=self._px(90), stretch=False)
        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        self.redis_trees[name] = tree

        self.root.after(700, lambda: self._redis_refresh(name))  # initial load

    def _redis_refresh(self, name):
        host, port, pw = self._redis_cfg(name)
        self.append_output(name, f"Loading Redis lists from {host}:{port} ...")

        def work():
            try:
                lists = redis_lists.list_lists(host, port, pw)
                self.root.after(0, self._redis_populate, name, lists)
            except Exception as e:
                self.append_output(name, f"Redis error: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _redis_populate(self, name, lists):
        tree = self.redis_trees.get(name)
        if tree is None:
            return
        tree.delete(*tree.get_children())
        for item in lists:
            tree.insert("", "end", values=(item["key"], item["len"]))
        lbl = self.redis_count_labels.get(name)
        if lbl is not None:
            lbl.configure(text=f"{len(lists)} list(s)")
        self.append_output(name, f"Found {len(lists)} list(s).")

    def _redis_selected_keys(self, name):
        tree = self.redis_trees.get(name)
        if tree is None:
            return []
        return [tree.item(i, "values")[0] for i in tree.selection()]

    def _redis_delete_selected(self, name):
        keys = self._redis_selected_keys(name)
        if not keys:
            self.append_output(name, "No lists selected — pick one or more rows first.")
            return
        preview = "\n".join(keys[:12]) + ("\n…" if len(keys) > 12 else "")
        if not messagebox.askyesno("Delete lists",
                                   f"Delete {len(keys)} selected list(s)?\n\n{preview}"):
            return
        host, port, pw = self._redis_cfg(name)
        self.append_output(name, f"Deleting {len(keys)} list(s) ...")

        def work():
            try:
                n = redis_lists.delete_keys(host, port, keys, pw)
                self.append_output(name, f"Deleted {n} list(s).")
                self.root.after(0, lambda: self._redis_refresh(name))
            except Exception as e:
                self.append_output(name, f"Redis error: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _redis_empty_all(self, name):
        if not messagebox.askyesno("Empty all lists",
                                   "Delete ALL Redis lists in this datastore?\n\nThis cannot be undone."):
            return
        host, port, pw = self._redis_cfg(name)
        self.append_output(name, "Emptying all Redis lists ...")

        def work():
            try:
                n = redis_lists.empty_all_lists(host, port, pw)
                self.append_output(name, f"Emptied all lists ({n} key(s) deleted).")
                self.root.after(0, lambda: self._redis_refresh(name))
            except Exception as e:
                self.append_output(name, f"Redis error: {e}")

        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------- settings page

    def _build_settings_page(self, page):
        """The gear page: app preferences, project management and the raw config editor."""
        scroll = self._make_scroll_area(page)
        self.settings_scroll = scroll
        container = ttk.Frame(scroll, padding=self._px(24))
        container.pack(expand=True, fill='both')

        ttk.Label(container, text="Settings", style="Title.TLabel").pack(anchor='w')
        ttk.Label(container,
                  text="How the app starts and closes, which projects it manages, and where "
                       "its configuration lives.",
                  style="Subtitle.TLabel").pack(anchor='w', pady=(2, self._px(18)))

        self._build_settings_paths(container)
        self._build_settings_startup(container)
        self._build_settings_window(container)
        self._build_settings_closing(container)
        self._build_settings_projects(container)
        self._build_settings_services(container)
        self._build_settings_config(container)
        self._build_settings_claude(container)
        self._build_settings_updates(container)
        self._build_settings_about(container)

        self._bind_wheel_recursive(scroll)

    # -------------------------------------------------------- settings: paths

    def _build_settings_paths(self, container):
        """Where this machine keeps the tools and projects, as ${tokens}.

        This card is what makes the config portable: everything else refers to a root by
        name, so moving to another PC means correcting these few values instead of every
        service, log file and virtualenv in the file."""
        card = self._card(container, "Path roots")
        ttk.Label(card,
                  text="Services and projects refer to these as ${name} in directories, "
                       "commands and log files. Correct them here and everything follows.",
                  style="Subtitle.TLabel", wraplength=self._px(640), justify='left').pack(
                      anchor='w', pady=(0, self._px(12)))

        self.paths_box = ttk.Frame(card)
        self.paths_box.pack(fill='x')
        self._fill_settings_paths()

        row = ttk.Frame(card)
        row.pack(anchor='w', pady=(self._px(12), 0))
        ttk.Button(row, text="Save paths", style="Accent.TButton",
                   command=self._save_paths).pack(side='left', padx=(0, self._px(8)))
        ttk.Button(row, text="Detect", command=self._detect_paths).pack(
            side='left', padx=(0, self._px(8)))
        ttk.Button(row, text="Convert paths to tokens",
                   command=self._tokenize_paths).pack(side='left')
        self.paths_note = ttk.Label(card, text="", style="Subtitle.TLabel",
                                    wraplength=self._px(640), justify='left')
        self.paths_note.pack(anchor='w', pady=(self._px(10), 0))

        ttk.Label(card,
                  text="Detect looks for each folder on the other drives and under your user "
                       "profile. Convert rewrites absolute paths already in the config as "
                       "tokens, which is what makes an existing setup portable.",
                  style="Subtitle.TLabel", wraplength=self._px(640), justify='left').pack(
                      anchor='w', pady=(self._px(6), 0))

    def _fill_settings_paths(self):
        """One row per root: name, editable value, Browse, and whether it exists."""
        box = self.paths_box
        if box is None:
            return
        for child in box.winfo_children():
            child.destroy()
        self.path_entries = {}
        box.columnconfigure(1, weight=1)

        for i, (name, value) in enumerate(self.service_manager.paths.items()):
            ttk.Label(box, text="${%s}" % name, font=(MONO_FONT, 9)).grid(
                row=i, column=0, sticky='w', padx=(0, self._px(12)), pady=self._px(3))

            var = tk.StringVar(value=value)
            entry = ttk.Entry(box, textvariable=var)
            entry.grid(row=i, column=1, sticky='ew', pady=self._px(3))
            self.path_entries[name] = var

            ttk.Button(box, text="Browse", width=8,
                       command=lambda n=name: self._browse_path(n)).grid(
                           row=i, column=2, padx=(self._px(8), self._px(8)), pady=self._px(3))

            state = ttk.Label(box, text="", width=10)
            state.grid(row=i, column=3, sticky='w', pady=self._px(3))
            # Re-checked on every keystroke, so a typo is obvious before anything is saved.
            var.trace_add("write", lambda *_, v=var, lbl=state: self._mark_path(v, lbl))
            self._mark_path(var, state)

        self._bind_wheel_recursive(self.settings_scroll)

    def _mark_path(self, var, label):
        exists = os.path.isdir(config_manager.expand_tokens(
            var.get().strip(), self.service_manager.paths))
        label.configure(text="found" if exists else "not found",
                        foreground=RUNNING_COLOR if exists else ATTENTION_COLOR)

    def _browse_path(self, name):
        chosen = filedialog.askdirectory(
            parent=self.root, title=f"Folder for ${{{name}}}",
            initialdir=self.path_entries[name].get().strip() or os.path.expanduser("~"))
        if chosen:
            self.path_entries[name].set(os.path.normpath(chosen))

    def _detect_paths(self):
        found = config_manager.detect_paths()
        for name, var in self.path_entries.items():
            if name in found:
                var.set(found[name])
        self.paths_note.configure(text="Detected — review the values, then Save paths.")

    def _save_paths(self):
        self.service_manager.set_paths(
            {name: var.get().strip() for name, var in self.path_entries.items()})
        self.save_config()
        # Every service page shows its directory and the command line it resolves to, so
        # they're rebuilt against the new roots rather than left showing the old ones.
        self._rebuild_service_pages()
        missing = self.service_manager.missing_paths()
        self.paths_note.configure(
            text="Saved." if not missing else
                 f"Saved. Still not found: {', '.join('${%s}' % m for m in missing)}.")

    def _tokenize_paths(self):
        sm = self.service_manager
        preview = sm.tokenize_paths()  # counts and rewrites in one pass
        if not preview:
            sm.save()
            self.paths_note.configure(
                text="Nothing to convert — no absolute path in the config starts with a root.")
            return
        self.save_config()
        self._rebuild_service_pages()
        self._after_apps_changed()
        self.paths_note.configure(
            text=f"Converted {preview} path(s) to ${{tokens}}. The config is now portable: "
                 "on another PC only the roots above need correcting.")

    def _rebuild_service_pages(self):
        """Recreate every service page so it reflects newly-resolved paths."""
        showing = self.current_page
        for name in list(self.service_manager.services):
            if name in self.pages:
                self._drop_service_page(name)
        for name in list(self.service_manager.services):
            self._add_service_page(name)
        if showing in self.pages:
            self.current_page = None
            self.show_page(showing)
        self._apply_status()

    def _settings_check(self, parent, key, text, hint=None):
        """A preference checkbox that writes straight through to appsettings.json."""
        default = config_manager.DEFAULT_SETTINGS.get(key, False)
        var = tk.BooleanVar(value=bool(self.service_manager.settings.get(key, default)))
        self.settings_vars[key] = var
        ttk.Checkbutton(parent, text=text, variable=var,
                        command=lambda: self._set_setting(key, var.get())).pack(anchor='w')
        if hint:
            ttk.Label(parent, text=hint, style="Subtitle.TLabel").pack(
                anchor='w', padx=(self._px(28), 0), pady=(0, self._px(10)))

    def _set_setting(self, key, value):
        self.service_manager.settings[key] = value
        self.service_manager.save_settings()

    def _build_settings_startup(self, container):
        card = self._card(container, "Startup")
        self._settings_check(
            card, "start_minimized", "Start minimised to the system tray",
            "The window isn't shown on launch — click the tray icon to bring it up.")
        self._settings_check(
            card, "autostart_enabled", "Auto-start services flagged for it",
            "Master switch for the per-service Auto-start checkboxes on each dashboard.")
        self._settings_check(
            card, "check_updates", "Check for a new release on launch",
            "Asks GitHub for the latest release in the background and only speaks up if "
            "there is a newer one.")

    def _build_settings_window(self, container):
        card = self._card(container, "Window")
        ttk.Label(card, text="The size the window opens at. Applies the next time the app starts.",
                  style="Subtitle.TLabel").pack(anchor='w', pady=(0, self._px(10)))

        row = ttk.Frame(card)
        row.pack(anchor='w')
        ttk.Label(row, text="Width").pack(side='left', padx=(0, self._px(6)))
        width_entry = ttk.Entry(row, width=7)
        width_entry.pack(side='left', padx=(0, self._px(14)))
        ttk.Label(row, text="Height").pack(side='left', padx=(0, self._px(6)))
        height_entry = ttk.Entry(row, width=7)
        height_entry.pack(side='left', padx=(0, self._px(16)))
        ttk.Button(row, text="Save", command=self._save_window_size).pack(
            side='left', padx=(0, self._px(8)))
        ttk.Button(row, text="Use current size", command=self._use_current_window_size).pack(
            side='left')

        self.settings_window_entries = (width_entry, height_entry)
        self.settings_note = ttk.Label(card, text="", style="Subtitle.TLabel")
        self.settings_note.pack(anchor='w', pady=(self._px(10), 0))
        self._load_window_size_fields()

    def _load_window_size_fields(self):
        if not self.settings_window_entries:
            return
        cfg = self.service_manager.settings.get("window", {}) or {}
        values = (cfg.get("width", 1120), cfg.get("height", 680))
        for entry, value in zip(self.settings_window_entries, values):
            entry.delete(0, 'end')
            entry.insert(0, str(value))

    def _save_window_size(self):
        try:
            width = int(self.settings_window_entries[0].get().strip())
            height = int(self.settings_window_entries[1].get().strip())
            if width < 880 or height < 560:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Window size",
                "Enter whole numbers of at least 880 × 560 — the window's minimum size.",
                parent=self.root)
            return
        self.service_manager.settings["window"] = {"width": width, "height": height}
        self.service_manager.save_settings()
        self.settings_note.configure(text=f"Saved — {width} × {height} applies next launch.")

    def _use_current_window_size(self):
        # The window is created at the stored size multiplied by the display scale, so undo
        # that here: the file holds logical pixels, not this monitor's physical ones.
        width = int(round(self.root.winfo_width() / self.ui_scale))
        height = int(round(self.root.winfo_height() / self.ui_scale))
        for entry, value in zip(self.settings_window_entries, (width, height)):
            entry.delete(0, 'end')
            entry.insert(0, str(value))
        self._save_window_size()

    def _build_settings_closing(self, container):
        card = self._card(container, "Closing")
        self._settings_check(
            card, "minimize_to_tray_on_close", "Close button minimises to the tray",
            "[X] hides the window and leaves everything running. Exit from the tray menu to "
            "actually quit.")
        self._settings_check(
            card, "confirm_on_exit", "Ask before exiting",
            "Exiting stops every process this app started; Windows services are left alone.")

    # ------------------------------------------------------- settings: projects

    def _build_settings_projects(self, container):
        card = self._card(container, "Projects")
        ttk.Label(card,
                  text="One tile on the rail per project. A project's scalar fields double as "
                       "${tokens} its services can use in paths and commands.",
                  style="Subtitle.TLabel").pack(anchor='w', pady=(0, self._px(12)))

        self.settings_apps_box = ttk.Frame(card)
        self.settings_apps_box.pack(fill='x')
        self._fill_settings_apps()

        ttk.Button(card, text="Add Project", command=self.add_app_dialog).pack(
            anchor='w', pady=(self._px(12), 0))

    def _fill_settings_apps(self):
        box = self.settings_apps_box
        if box is None:
            return
        for child in box.winfo_children():
            child.destroy()

        sm = self.service_manager
        for app in sm.apps:
            app_id = app["id"]
            row = ttk.Frame(box)
            row.pack(fill='x', pady=self._px(3))

            swatch = tk.Label(row, bg=app.get("color", "#4c9aff"), width=2, bd=0)
            swatch.pack(side='left', padx=(0, self._px(10)))
            ttk.Label(row, text=app.get("name", app_id)).pack(side='left')
            ttk.Label(row, text=f"{app.get('short', '')} · {app_id} · "
                                f"{len(sm.services_for_app(app_id))} service(s)",
                      style="Subtitle.TLabel").pack(side='left', padx=(self._px(12), 0))

            ttk.Button(row, text="Remove", width=9,
                       command=lambda a=app_id: self.remove_app(a)).pack(side='right')
            ttk.Button(row, text="Edit", width=9,
                       command=lambda a=app_id: self.edit_app_dialog(a)).pack(
                           side='right', padx=(0, self._px(8)))

        self._bind_wheel_recursive(self.settings_scroll)

    def add_app_dialog(self):
        AppDialog(self)

    def edit_app_dialog(self, app_id):
        if app_id in self.service_manager.app_by_id:
            AppDialog(self, app_id=app_id)

    def commit_app(self, app_id, app):
        """Apply the project editor's result and repaint everything drawn per app."""
        if app_id is None:
            self.service_manager.add_app(app)
        else:
            self.service_manager.update_app(app_id, app)
        self._after_apps_changed()

    def remove_app(self, app_id):
        sm = self.service_manager
        if len(sm.apps) <= 1:
            messagebox.showerror("Remove project",
                                 "There has to be at least one project.", parent=self.root)
            return

        used = sm.services_using_app(app_id)
        if used:
            listing = "\n".join(f"  • {n}" for n in used[:12])
            if len(used) > 12:
                listing += f"\n  … and {len(used) - 12} more"
            messagebox.showerror(
                "Remove project",
                f"{len(used)} service(s) still belong to this project:\n\n{listing}\n\n"
                "Remove them, or reassign them to another project, first.",
                parent=self.root)
            return

        name = sm.app_by_id[app_id].get("name", app_id)
        if not messagebox.askyesno("Remove project", f"Remove '{name}' from the rail?",
                                   parent=self.root):
            return
        sm.remove_app(app_id)
        self._after_apps_changed()

    # ------------------------------------------------------- settings: services

    def _build_settings_services(self, container):
        card = self._card(container, "Services")
        self.settings_summary = ttk.Label(card, text="", style="Subtitle.TLabel")
        self.settings_summary.pack(anchor='w', pady=(0, self._px(12)))

        row = ttk.Frame(card)
        row.pack(anchor='w')
        ttk.Button(row, text="Add Service", style="Accent.TButton",
                   command=lambda: self.add_service_dialog()).pack(side='left', padx=(0, self._px(8)))
        ttk.Button(row, text="Restore missing defaults",
                   command=self.restore_default_services).pack(side='left')

        ttk.Label(card,
                  text="Restoring adds back any service this app ships with that isn't in the "
                       "configuration; entries you already have are never overwritten.",
                  style="Subtitle.TLabel").pack(anchor='w', pady=(self._px(10), 0))
        self._refresh_settings_summary()

    def _refresh_settings_summary(self):
        if self.settings_summary is None:
            return
        sm = self.service_manager
        self.settings_summary.configure(
            text=f"{len(sm.services)} service(s) across {len(sm.apps)} project(s).")

    # -------------------------------------------------- settings: JSON editor

    def _build_settings_config(self, container):
        card = self._card(container, "Configuration files")

        self.config_editor_file = tk.StringVar(value=config_manager.CONFIG_FILE_NAME)
        picker = ttk.Frame(card)
        picker.pack(anchor='w', pady=(0, self._px(8)))
        for filename in (config_manager.CONFIG_FILE_NAME, config_manager.SETTINGS_FILE_NAME):
            ttk.Radiobutton(picker, text=filename, value=filename,
                            variable=self.config_editor_file,
                            command=self._load_config_editor).pack(side='left', padx=(0, self._px(16)))
        ttk.Button(picker, text="Open folder", command=self._open_config_folder).pack(side='left')

        self.config_path_label = ttk.Label(card, text="", style="Mono.TLabel")
        self.config_path_label.pack(anchor='w', pady=(0, self._px(8)))

        self.config_editor = self._make_code_editor(card, height=18)

        buttons = ttk.Frame(card)
        buttons.pack(fill='x')
        ttk.Button(buttons, text="Save & apply", style="Accent.TButton",
                   command=self._save_config_editor).pack(side='left', padx=(0, self._px(8)))
        ttk.Button(buttons, text="Format", command=self._format_config_editor).pack(
            side='left', padx=(0, self._px(8)))
        ttk.Button(buttons, text="Reload from disk", command=self._load_config_editor).pack(
            side='left')
        self.config_editor_status = ttk.Label(buttons, text="", style="Subtitle.TLabel")
        self.config_editor_status.pack(side='left', padx=(self._px(14), 0))

        ttk.Label(card,
                  text="Saving validates the JSON, writes the file and reloads the app, so the "
                       "editor is safe to use while services are running. The app rewrites "
                       "services.json whenever you toggle a checkbox, so save here before "
                       "changing things elsewhere.",
                  style="Subtitle.TLabel", wraplength=self._px(640), justify='left').pack(
                      anchor='w', pady=(self._px(10), 0))

        self._load_config_editor()

    def _make_code_editor(self, parent, height=18):
        """A dark, monospaced, editable text box with both scrollbars — JSON wraps badly, so
        long command lines scroll sideways instead of being folded."""
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True, pady=(0, self._px(10)))

        text = tk.Text(frame, wrap="none", height=height,
                       bg=LOG_BG, fg=LOG_FG, insertbackground=LOG_INSERT,
                       selectbackground=LOG_SELECT_BG, relief="flat", borderwidth=0,
                       highlightthickness=0, font=(MONO_FONT, 10),
                       padx=self._px(12), pady=self._px(10), undo=True)
        text.tag_configure("jsonerr", background="#5a1d1d")

        vsb = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        text.pack(side="left", fill="both", expand=True)

        text.bind("<KeyRelease>", self._on_config_editor_edit)
        return text

    def _on_config_editor_edit(self, _event=None):
        self.config_editor.tag_remove("jsonerr", "1.0", "end")
        if not self.config_editor_dirty:
            self.config_editor_dirty = True
            self._config_status("Unsaved changes.")

    def _config_editor_path(self):
        if self.config_editor_file.get() == config_manager.SETTINGS_FILE_NAME:
            return config_manager.get_settings_path()
        return config_manager.get_config_path()

    def _config_status(self, message, error=False):
        if self.config_editor_status is not None:
            self.config_editor_status.configure(
                text=message, foreground=LOG_ERROR if error else MUTED)

    def _open_config_folder(self):
        try:
            os.startfile(config_manager.config_dir())
        except Exception as e:
            messagebox.showerror("Open folder", f"Could not open the folder:\n{e}",
                                 parent=self.root)

    def _load_config_editor(self):
        path = self._config_editor_path()
        try:
            with open(path, 'r', encoding='utf-8-sig') as file:  # tolerate a BOM, as elsewhere
                content = file.read()
            status, error = f"Loaded {os.path.basename(path)}.", False
        except OSError as e:
            content, status, error = "", f"Could not read the file: {e}", True

        self.config_editor.delete('1.0', 'end')
        self.config_editor.insert('1.0', content)
        self.config_editor.edit_reset()  # the file as loaded is the undo baseline
        self.config_editor.tag_remove("jsonerr", "1.0", "end")
        self.config_editor_baseline = self.config_editor.get('1.0', 'end-1c')
        self.config_editor_dirty = False
        self.config_path_label.configure(text=path)
        self._config_status(status, error)

    def _parse_config_editor(self):
        """Parse the editor's contents, reporting where it broke. Returns (data, ok)."""
        try:
            return json.loads(self.config_editor.get('1.0', 'end-1c')), True
        except json.JSONDecodeError as e:
            self.config_editor.tag_remove("jsonerr", "1.0", "end")
            self.config_editor.tag_add("jsonerr", f"{e.lineno}.0", f"{e.lineno}.end")
            self.config_editor.see(f"{e.lineno}.0")
            self._config_status(f"Invalid JSON — line {e.lineno}: {e.msg}", error=True)
            messagebox.showerror("Invalid JSON",
                                 f"Line {e.lineno}, column {e.colno}:\n{e.msg}",
                                 parent=self.root)
            return None, False

    def _format_config_editor(self):
        data, ok = self._parse_config_editor()
        if not ok:
            return
        self.config_editor.delete('1.0', 'end')
        self.config_editor.insert('1.0', json.dumps(data, indent=4))
        self.config_editor_dirty = True
        self._config_status("Reformatted — not saved yet.")

    def _save_config_editor(self):
        data, ok = self._parse_config_editor()
        if not ok:
            return

        filename = self.config_editor_file.get()
        problem = (self._validate_services_document(data)
                   if filename == config_manager.CONFIG_FILE_NAME
                   else self._validate_settings_document(data))
        if problem:
            self._config_status(problem, error=True)
            messagebox.showerror("Configuration rejected", problem, parent=self.root)
            return

        path = self._config_editor_path()
        try:
            with open(path, 'w', encoding='utf-8') as file:
                file.write(json.dumps(data, indent=4))
        except OSError as e:
            self._config_status(f"Could not write the file: {e}", error=True)
            messagebox.showerror("Save failed", f"Could not write {path}:\n{e}",
                                 parent=self.root)
            return

        self.config_editor_dirty = False
        self.config_editor_baseline = self.config_editor.get('1.0', 'end-1c')
        # Reload rather than just accepting the file: a hand-edit can rename services,
        # change their type or add a project, none of which the live UI would otherwise
        # know about — and the next checkbox toggle would write the stale state back.
        self.reload_config()
        self._config_status(f"Saved {os.path.basename(path)} and reloaded.")

    def _validate_services_document(self, data):
        """A readable reason services.json can't be applied, or None if it's usable."""
        if not isinstance(data, dict):
            return "The file must contain a JSON object."

        paths = data.get("paths", {})
        if not isinstance(paths, dict):
            return '"paths" must be an object of ${token} name -> folder.'
        for name, value in paths.items():
            if not isinstance(value, str):
                return f'Path root "{name}" must be a string.'

        apps = data.get("apps")
        if not isinstance(apps, list) or not apps:
            return '"apps" must be a non-empty list of project definitions.'

        ids = []
        for app in apps:
            if not isinstance(app, dict) or not str(app.get("id", "")).strip():
                return 'Every project in "apps" needs a non-empty "id".'
            ids.append(app["id"])
        if len(set(ids)) != len(ids):
            return 'Two projects share the same "id".'

        services = data.get("services")
        if not isinstance(services, dict):
            return '"services" must be an object keyed by service name.'

        for name, info in services.items():
            if not isinstance(info, dict):
                return f'Service "{name}" must be an object.'
            if name.startswith("@"):
                return (f'Service "{name}" can\'t start with "@" — that prefix is reserved '
                        "for the dashboard and build pages.")
            refs = info.get("app")
            refs = refs if isinstance(refs, list) else [refs]
            unknown = [str(a) for a in refs if a not in ids]
            if unknown:
                return (f'Service "{name}" refers to unknown project(s): '
                        f'{", ".join(unknown)}. Known ids: {", ".join(ids)}.')
        return None

    def _validate_settings_document(self, data):
        if not isinstance(data, dict):
            return "The file must contain a JSON object."
        window = data.get("window", {})
        if not isinstance(window, dict):
            return '"window" must be an object with "width" and "height".'
        for key in ("width", "height"):
            if key in window and not isinstance(window[key], int):
                return f'"window.{key}" must be a whole number.'
        return None

    # ------------------------------------------------------- settings: about

    def _build_settings_claude(self, container):
        """Install or remove this app's MCP server in Claude Code, or show the commands."""
        card = self._card(container, "Claude Code")
        ttk.Label(card,
                  text="Lets Claude Code check, start, restart and read the logs of the "
                       "services here, instead of starting its own copies of the servers. "
                       "Installs for all your projects.",
                  style="Subtitle.TLabel", wraplength=self._px(640), justify='left').pack(
                      anchor='w', pady=(0, self._px(12)))

        state = ttk.Frame(card)
        state.pack(fill='x')
        self.claude_state = ttk.Label(state, text="", style="State.TLabel")
        self.claude_state.pack(side='left')
        self.claude_detail = ttk.Label(card, text="", style="Mono.TLabel", justify='left',
                                       wraplength=self._px(640))
        self.claude_detail.pack(anchor='w', pady=(self._px(4), self._px(10)))

        self.claude_note_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(card, variable=self.claude_note_var,
                        text="Also tell Claude to use it, in ~/.claude/CLAUDE.md").pack(anchor='w')
        ttk.Label(card,
                  text="A short section saying to use these tools and never start the servers "
                       "from a shell. Without it, Claude often starts its own copy anyway. "
                       "Remove takes it out again; the rest of the file is left alone.",
                  style="Subtitle.TLabel", wraplength=self._px(620), justify='left').pack(
                      anchor='w', padx=(self._px(28), 0), pady=(0, self._px(12)))

        row = ttk.Frame(card)
        row.pack(anchor='w')
        self.claude_install_btn = ttk.Button(row, text="Install", style="Accent.TButton",
                                             command=lambda: self._claude_action("install"))
        self.claude_install_btn.pack(side='left', padx=(0, self._px(8)))
        self.claude_remove_btn = ttk.Button(row, text="Remove",
                                            command=lambda: self._claude_action("remove"))
        self.claude_remove_btn.pack(side='left')
        self.claude_result = ttk.Label(card, text="", style="Subtitle.TLabel",
                                       wraplength=self._px(640), justify='left')
        self.claude_result.pack(anchor='w', pady=(self._px(8), 0))

        # The same thing by hand, for a machine where the button can't reach `claude`.
        ttk.Label(card, text="To do it by hand, run in a terminal:", style="Section.TLabel").pack(
            anchor='w', pady=(self._px(16), self._px(6)))
        manual = ttk.Frame(card)
        manual.pack(fill='x')
        manual.columnconfigure(1, weight=1)
        for i, (label, command) in enumerate(zip(("Install", "Remove"),
                                                 claude_setup.manual_commands())):
            ttk.Label(manual, text=label, style="Faint.TLabel").grid(
                row=i, column=0, sticky='w', padx=(0, self._px(10)), pady=self._px(3))
            entry = ttk.Entry(manual, font=(MONO_FONT, 9))
            entry.insert(0, command)
            entry.configure(state="readonly")
            entry.grid(row=i, column=1, sticky='ew', pady=self._px(3))
            ttk.Button(manual, text="Copy", style="Small.TButton",
                       command=lambda c=command: self._copy_text(c)).grid(
                           row=i, column=2, padx=(self._px(8), 0), pady=self._px(3))
        ttk.Label(card,
                  text="It runs this copy of the app, so install again if you move the .exe. "
                       "Open Claude Code sessions pick up a change once they're restarted.",
                  style="Subtitle.TLabel", wraplength=self._px(640), justify='left').pack(
                      anchor='w', pady=(self._px(10), 0))
        self._refresh_claude_status()

    def _refresh_claude_status(self):
        st = claude_setup.status()
        if not st["cli"]:
            label, color = "Claude Code not found", ATTENTION_COLOR
            detail = "Install Claude Code, then install this from here or by hand."
        elif st["current"]:
            label, color = "Installed", RUNNING_COLOR
            detail = "Runs " + subprocess.list2cmdline(st["command"])
        elif st["installed"]:
            label, color = "Installed for another copy of the app", ATTENTION_COLOR
            entry = st["entry"]
            detail = ("Runs " + subprocess.list2cmdline([entry.get("command", "")]
                                                         + list(entry.get("args") or []))
                      + ". Install again to point it at this one.")
        elif st["legacy"]:
            label, color = "Installed under an old name", ATTENTION_COLOR
            detail = (f"Registered as '{st['legacy'][0]}'. Install replaces it with "
                      f"'{claude_setup.SERVER_NAME}'.")
        else:
            label, color, detail = "Not installed", MUTED, ""
        self.claude_state.configure(text=label, foreground=color)
        self.claude_detail.configure(text=detail)
        self.claude_install_btn.configure(
            text="Install again" if st["installed"] else "Install",
            state="normal" if st["cli"] else "disabled")
        self.claude_remove_btn.configure(
            state="normal" if st["cli"] and (st["installed"] or st["legacy"] or st["note"])
            else "disabled")
        self.claude_note_var.set(st["note"] or not st["installed"])

    def _claude_action(self, action):
        """Run `claude mcp add/remove` off the main thread; it takes a second or two."""
        with_note = self.claude_note_var.get()
        for button in (self.claude_install_btn, self.claude_remove_btn):
            button.configure(state="disabled")
        self.claude_result.configure(
            text="Installing…" if action == "install" else "Removing…", foreground=MUTED)

        def work():
            try:
                if action == "install":
                    ok, message = claude_setup.install()
                    if ok and with_note:
                        claude_setup.add_note()
                    elif ok and not with_note and claude_setup.has_note():
                        claude_setup.remove_note()
                else:
                    ok, message = claude_setup.uninstall()
                    if ok and claude_setup.has_note():
                        claude_setup.remove_note()
            except Exception as e:  # a hung CLI, an unwritable CLAUDE.md, ...
                ok, message = False, f"{type(e).__name__}: {e}"
            self.root.after(0, done, ok, message)

        def done(ok, message):
            self.claude_result.configure(text=message, foreground=MUTED if ok else FAULT_COLOR)
            self.home_log(f"Claude Code: {message}")
            self._refresh_claude_status()

        threading.Thread(target=work, daemon=True).start()

    def _copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.claude_result.configure(text="Copied to the clipboard.", foreground=MUTED)

    def _build_settings_updates(self, container):
        """Where releases come from. Public repo, so the token is optional."""
        card = self._card(container, "Updates")
        ttk.Label(card,
                  text="The GitHub repository releases are published to. It's public, so "
                       "the token is optional — it raises the API rate limit, and is what "
                       "the check would need if the repo is ever made private.",
                  style="Subtitle.TLabel", wraplength=self._px(640), justify='left').pack(
                      anchor='w', pady=(0, self._px(12)))

        cfg = update.update_settings(self.service_manager.settings)
        self.update_repo_var = tk.StringVar(value=cfg["repo"])
        self.update_token_var = tk.StringVar(value=cfg["token"])

        form = ttk.Frame(card)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Repository").grid(row=0, column=0, sticky='w',
                                                padx=(0, self._px(10)), pady=self._px(4))
        ttk.Entry(form, textvariable=self.update_repo_var).grid(
            row=0, column=1, sticky='ew', pady=self._px(4))

        ttk.Label(form, text="Token").grid(row=1, column=0, sticky='w',
                                           padx=(0, self._px(10)), pady=self._px(4))
        # Masked so it isn't readable over a shoulder or in a screen share.
        ttk.Entry(form, textvariable=self.update_token_var, show="•").grid(
            row=1, column=1, sticky='ew', pady=self._px(4))

        ttk.Label(card,
                  text="owner/repo. A token, if you use one, is stored in appsettings.json "
                       "on this machine and never built into the .exe; a fine-grained one "
                       "needs Contents: Read (a classic one needs the repo scope). Leave it "
                       "empty to fall back to the ONCODES_UPDATE_TOKEN, GH_TOKEN or "
                       "GITHUB_TOKEN environment variable.",
                  style="Subtitle.TLabel", wraplength=self._px(640), justify='left').pack(
                      anchor='w', pady=(self._px(8), self._px(10)))

        row = ttk.Frame(card)
        row.pack(anchor='w')
        ttk.Button(row, text="Save", style="Accent.TButton",
                   command=self._save_update_settings).pack(side='left', padx=(0, self._px(8)))
        ttk.Button(row, text="Check for updates now",
                   command=self._check_updates_now).pack(side='left')

        self.update_status = ttk.Label(card, text="", style="Subtitle.TLabel",
                                       wraplength=self._px(640), justify='left')
        self.update_status.pack(anchor='w', pady=(self._px(10), 0))

    def _build_settings_about(self, container):
        card = self._card(container, "About")
        ttk.Label(card,
                  text=f"OnCodes Dev Service Manager   v{update.CURRENT_VERSION}").pack(anchor='w')
        ttk.Label(card, text="Manages the SRP Live Help and Neobe stacks side by side.",
                  style="Subtitle.TLabel").pack(anchor='w', pady=(2, self._px(12)))

    def _save_update_settings(self):
        """Persist the repo and token, then report through the same line the check uses."""
        self.service_manager.settings["updates"] = {
            "repo": self.update_repo_var.get().strip(),
            "token": self.update_token_var.get().strip(),
        }
        self.service_manager.save_settings()
        self._set_update_status("Saved.", MUTED)

    def _set_update_status(self, text, colour=None):
        if self.update_status is not None:
            self.update_status.configure(text=text, foreground=colour or MUTED)

    def _check_updates_now(self):
        """Unlike the launch check, this one reports every outcome, failures included."""
        self._set_update_status("Checking…")

        def work():
            # Check what's in the boxes, not what was last saved — otherwise correcting a
            # mistyped token means saving before it can be tested.
            settings = {"updates": {"repo": self.update_repo_var.get().strip(),
                                    "token": self.update_token_var.get().strip()}}
            result = update.check(settings)
            self.root.after(0, lambda: self._show_update_result(result))

        threading.Thread(target=work, daemon=True).start()

    def _show_update_result(self, result):
        if result.has_update:
            self._set_update_status(f"v{result.version} is available.", ATTENTION_COLOR)
            update.show_update_alert(result.version, result.notes, result.url)
            return

        if result.status == "current":
            self._set_update_status(f"Up to date (v{update.CURRENT_VERSION}).", RUNNING_COLOR)
            messagebox.showinfo("Check for updates",
                                f"You're on the latest version (v{update.CURRENT_VERSION}).",
                                parent=self.root)
            return

        # Everything else is a misconfiguration the user can act on, so say which one it is
        # rather than the old catch-all "could not reach GitHub".
        self._set_update_status(update.describe(result), ATTENTION_COLOR)
        messagebox.showwarning("Check for updates", update.describe(result), parent=self.root)

    def _sync_settings_widgets(self):
        """Refresh every settings control from the (re-read) settings and config."""
        for key, var in self.settings_vars.items():
            var.set(bool(self.service_manager.settings.get(
                key, config_manager.DEFAULT_SETTINGS.get(key, False))))
        self._load_window_size_fields()
        self._fill_settings_paths()
        self._fill_settings_apps()
        self._refresh_settings_summary()

        # The Updates fields aren't checkboxes, so the loop above doesn't cover them; a
        # token edited in the JSON editor has to show up here too.
        if self.update_repo_var is not None:
            cfg = update.update_settings(self.service_manager.settings)
            self.update_repo_var.set(cfg["repo"])
            self.update_token_var.set(cfg["token"])

    # ----------------------------------------------------------- log helpers

    def append_output(self, service_name, text):
        # Output arrives on background threads; Tkinter is not thread-safe, so queue it and
        # let the main thread draw whatever has piled up every LOG_FLUSH_MS.
        self.log_buffer.append(service_name, text)
        self.pending_output.append((service_name, text))  # deque.append is thread-safe
        if not self.output_flush_scheduled:
            self.output_flush_scheduled = True
            self.root.after(LOG_FLUSH_MS, self._flush_output)

    def _flush_output(self):
        # Cleared before draining: a line queued mid-drain either gets drained here or
        # schedules the next flush itself, so nothing is left sitting in the queue.
        self.output_flush_scheduled = False
        batches = {}
        while self.pending_output:
            service_name, text = self.pending_output.popleft()
            batches.setdefault(service_name, []).append(text)
        for service_name, entries in batches.items():
            output_area = self.service_tabs.get(service_name, {}).get("output_area")
            if output_area is not None:
                self._append_lines(output_area, entries)

    def save_config(self):
        self.service_manager.save()

    def home_log(self, message, app_id=None):
        # Mirror bulk-action progress to the app's activity log (main-thread safe).
        app_id = app_id or self.current_app
        self.log_buffer.append(f"activity:{app_id}", message)
        if self.home_logs.get(app_id) is not None:
            self.root.after(0, self._home_log, app_id, message)

    def _home_log(self, app_id, message):
        self._append_log(self.home_logs[app_id], message)

    # ------------------------------------------------------------- bulk actions

    def _bulk_targets(self, app_id):
        """The app's services that bulk actions consider (never tasks or log views)."""
        sm = self.service_manager
        return [n for n in sm.services_for_app(app_id)
                if not self._is_logview(n) and not self._is_task(n)]

    def start_all(self, app_id=None):
        """Start the services this app has selected for 'Start All'."""
        app_id = app_id or self.current_app
        sm = self.service_manager
        name = sm.app_by_id[app_id].get("name", app_id)
        self.home_log(f"Starting selected {name} services...", app_id)
        started = 0
        for service_name in self._bulk_targets(app_id):
            if not sm.includes_in_start_all(service_name, app_id):
                self.home_log(f"  - {service_name}: skipped (not in Start All)", app_id)
                continue
            # Skip Windows services that are already running (avoids a needless UAC prompt).
            if self._is_winservice(service_name) and sm.is_winservice_running(service_name):
                self.home_log(f"  - {service_name}: already running", app_id)
                continue
            sm.start_service(service_name, self.service_tabs, self.append_output, self.save_config)
            self.home_log(f"  - {service_name}: start requested", app_id)
            started += 1
        self.home_log(f"Start All complete ({started} service(s)).\n", app_id)
        self.root.after(300, self._apply_status)

    def stop_all(self, app_id=None):
        """Stop this app's running services, leaving shared ones the other app still needs."""
        app_id = app_id or self.current_app
        sm = self.service_manager
        name = sm.app_by_id[app_id].get("name", app_id)
        self.home_log(f"Stopping {name} services...", app_id)
        for service_name in self._bulk_targets(app_id):
            # Only touch a Windows service that's actually running, so an already-stopped
            # one doesn't trigger a pointless UAC prompt during Stop All.
            if self._is_winservice(service_name) and not sm.is_winservice_running(service_name):
                continue
            if not self._is_winservice(service_name) and not sm.processes.get(service_name):
                continue
            stopped = sm.stop_service(service_name, self.append_output, requesting_app=app_id)
            if stopped:
                self.home_log(f"  - {service_name}: stop requested", app_id)
            else:
                holder = sm._other_app_holding(service_name, app_id)
                if holder:
                    self.home_log(f"  - {service_name}: left running (shared with {holder})", app_id)
        self.home_log(f"{name} services stopped.\n", app_id)
        self._apply_status()

    def restart_all(self, app_id=None):
        """Restart the services selected for 'Start All' (so excluded ones aren't started)."""
        app_id = app_id or self.current_app
        sm = self.service_manager
        name = sm.app_by_id[app_id].get("name", app_id)
        self.home_log(f"Restarting selected {name} services...", app_id)
        count = 0
        for service_name in self._bulk_targets(app_id):
            if not sm.includes_in_start_all(service_name, app_id):
                self.home_log(f"  - {service_name}: skipped (not in Start All)", app_id)
                continue
            sm.restart_service(service_name, self.service_tabs, self.append_output, self.save_config)
            self.home_log(f"  - {service_name}: restart requested", app_id)
            count += 1
        self.home_log(f"Restart All complete ({count} service(s)).\n", app_id)
        self.root.after(300, self._apply_status)


# --------------------------------------------------------------------- dialogs

def service_kind_label(info):
    """What a service is, in the words the sidebar sections use."""
    return {"process": "Process", "cron": "URL poll", "winservice": "Windows service",
            "logview": "Log", "task": "Task"}[service_type_of(info)]


def service_type_of(info):
    """Which editor type an existing service definition corresponds to.

    A plain child process is the absence of a `type`, so it's what anything unrecognised
    falls back to — except a legacy entry that only carries a `url`, which is a cron."""
    kind = info.get("type")
    if kind in ("cron", "winservice", "logview", "task"):
        return kind
    return "cron" if "url" in info else "process"


class ModalDialog(tk.Toplevel):
    """Shared plumbing for the editor windows: modal, centred, dark title bar."""

    def __init__(self, ui, title):
        super().__init__(ui.root)
        self.ui = ui
        self.title(title)
        self.configure(background="#1c1c1c")
        self.transient(ui.root)
        self.resizable(False, False)
        self.bind("<Escape>", lambda e: self.destroy())

    def _p(self, n):
        return self.ui._px(n)

    def present(self):
        """Size to the content, centre on the main window and take the focus."""
        self.update_idletasks()
        parent = self.ui.root
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{x}+{y}")
        apply_dark_titlebar(self)
        self.grab_set()
        self.focus_force()


class ConfirmDialog(ModalDialog):
    """A yes/no question that doesn't block the caller, unlike messagebox.

    Used when something outside the window (the control API) asks for a change the user
    has to approve: the answer arrives through on_answer, and closing the dialog any other
    way (Escape, the title-bar X, or the caller giving up and calling close()) means No.
    Kept on top until answered, since the request didn't come from a click in this window."""

    def __init__(self, ui, title, question, detail, confirm, on_answer):
        super().__init__(ui, title)
        self.on_answer = on_answer
        self.answered = False
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", lambda: self._answer(False))
        self.bind("<Escape>", lambda e: self._answer(False))

        body = ttk.Frame(self, padding=(self._p(22), self._p(18)))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=question, style="Section.TLabel",
                  wraplength=self._p(420)).pack(anchor="w")
        if detail:
            ttk.Label(body, text=detail, style="Subtitle.TLabel", justify="left",
                      wraplength=self._p(420)).pack(anchor="w", pady=(self._p(8), 0))

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(self._p(18), 0))
        # The safe answer is the default: Enter keeps the service, a deliberate click removes.
        keep = ttk.Button(buttons, text="Keep", style="Accent.TButton",
                          command=lambda: self._answer(False))
        keep.pack(side="right")
        ttk.Button(buttons, text=confirm, command=lambda: self._answer(True)).pack(
            side="right", padx=(0, self._p(8)))
        self.bind("<Return>", lambda e: self._answer(False))
        self.present()
        keep.focus_set()

    def _answer(self, value):
        if self.answered:
            return
        self.answered = True
        self.destroy()
        self.on_answer(value)

    def close(self):
        """Withdraw the question unanswered (counts as No)."""
        self._answer(False)


class ServiceDialog(ModalDialog):
    """Add or edit one service definition, with the fields following the chosen type."""

    def __init__(self, ui, name=None, preselect_app=None):
        super().__init__(ui, "Edit service" if name else "Add service")
        sm = ui.service_manager
        self.original_name = name
        self.original_info = dict(sm.services.get(name, {})) if name else {}

        self.vars = {key: tk.StringVar() for key in (
            "name", "dir", "command", "url", "interval", "service_name", "log_file",
            "redis_host", "redis_port", "section")}
        self.flags = {key: tk.BooleanVar() for key in
                      ("interactive", "include_in_start_all", "autostart")}
        self.app_vars = {}
        self.type_label = tk.StringVar()

        self._load(preselect_app)
        self._build()
        self._on_type_change()
        self.present()

    # ------------------------------------------------------------ prefilling

    def _load(self, preselect_app):
        sm, info = self.ui.service_manager, self.original_info

        self.vars["name"].set(self.original_name or "")
        self.vars["dir"].set(info.get("dir", ""))
        command = info.get("command") or []
        self.vars["command"].set(" ".join(command) if isinstance(command, list) else str(command))
        self.vars["url"].set(info.get("url", ""))
        self.vars["interval"].set(str(info.get("interval", 60)))
        self.vars["service_name"].set(info.get("service_name", ""))
        self.vars["log_file"].set(info.get("log_file", ""))
        self.vars["section"].set(info.get("section", ""))

        redis = info.get("redis") or {}
        self.vars["redis_host"].set(redis.get("host", ""))
        self.vars["redis_port"].set(str(redis["port"]) if redis.get("port") else "")

        self.flags["interactive"].set(bool(info.get("interactive", False)))
        self.flags["autostart"].set(bool(info.get("autostart", False)))

        if self.original_name:
            owners = sm.apps_of(self.original_name)
            self.flags["include_in_start_all"].set(
                sm.includes_in_start_all(self.original_name, owners[0]) if owners else True)
        else:
            owners = [preselect_app] if preselect_app else []
            self.flags["include_in_start_all"].set(True)
        self.shown_start_all = self.flags["include_in_start_all"].get()
        self.original_start_all = info.get("include_in_start_all")

        self.selected_apps = [a for a in owners if a in sm.app_by_id]
        self.type_label.set(dict((k, label) for k, label, _ in SERVICE_TYPES)[
            service_type_of(info) if self.original_name else "process"])

    # --------------------------------------------------------------- layout

    def _build(self):
        body = ttk.Frame(self, padding=self._p(20))
        body.pack(fill='both', expand=True)
        body.columnconfigure(1, weight=1)
        row = 0

        ttk.Label(body, text="Name").grid(row=row, column=0, sticky='w',
                                          padx=(0, self._p(12)), pady=self._p(5))
        entry = ttk.Entry(body, textvariable=self.vars["name"], width=46)
        entry.grid(row=row, column=1, sticky='ew', pady=self._p(5))
        entry.focus_set()
        row += 1

        ttk.Label(body, text="Projects").grid(row=row, column=0, sticky='nw',
                                              padx=(0, self._p(12)), pady=self._p(5))
        apps_box = ttk.Frame(body)
        apps_box.grid(row=row, column=1, sticky='w', pady=self._p(5))
        for app in self.ui.service_manager.apps:
            var = tk.BooleanVar(value=app["id"] in self.selected_apps)
            self.app_vars[app["id"]] = var
            ttk.Checkbutton(apps_box, text=app.get("name", app["id"]), variable=var).pack(
                side='left', padx=(0, self._p(14)))
        row += 1
        ttk.Label(body, text="Ticking more than one shares the service between projects.",
                  style="Subtitle.TLabel").grid(row=row, column=1, sticky='w',
                                                pady=(0, self._p(8)))
        row += 1

        ttk.Label(body, text="Type").grid(row=row, column=0, sticky='w',
                                          padx=(0, self._p(12)), pady=self._p(5))
        combo = ttk.Combobox(body, textvariable=self.type_label, state="readonly",
                             values=[label for _, label, _ in SERVICE_TYPES])
        combo.grid(row=row, column=1, sticky='ew', pady=self._p(5))
        combo.bind("<<ComboboxSelected>>", self._on_type_change)
        row += 1

        self.type_hint = ttk.Label(body, text="", style="Subtitle.TLabel",
                                   wraplength=self._p(400), justify='left')
        self.type_hint.grid(row=row, column=1, sticky='w', pady=(0, self._p(8)))
        row += 1

        ttk.Separator(body, orient='horizontal').grid(row=row, column=0, columnspan=2,
                                                      sticky='ew', pady=self._p(8))
        row += 1

        self.type_frame = ttk.Frame(body)
        self.type_frame.grid(row=row, column=0, columnspan=2, sticky='ew')
        self.type_frame.columnconfigure(1, weight=1)
        row += 1

        self.startup_frame = ttk.Frame(body)
        self.startup_frame.grid(row=row, column=0, columnspan=2, sticky='w',
                                pady=(self._p(10), 0))
        ttk.Checkbutton(self.startup_frame, text="Include in Start All",
                        variable=self.flags["include_in_start_all"]).pack(anchor='w')
        ttk.Checkbutton(self.startup_frame, text="Start automatically on launch",
                        variable=self.flags["autostart"]).pack(anchor='w')
        row += 1

        buttons = ttk.Frame(body)
        buttons.grid(row=row, column=0, columnspan=2, sticky='e', pady=(self._p(18), 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(
            side='right', padx=(self._p(8), 0))
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self._save).pack(
            side='right')

    def _type_key(self):
        return next(key for key, label, _ in SERVICE_TYPES if label == self.type_label.get())

    def _field(self, row, label, key, hint=None):
        ttk.Label(self.type_frame, text=label).grid(row=row, column=0, sticky='w',
                                                    padx=(0, self._p(12)), pady=self._p(4))
        ttk.Entry(self.type_frame, textvariable=self.vars[key], width=46).grid(
            row=row, column=1, sticky='ew', pady=self._p(4))
        if not hint:
            return row + 1
        ttk.Label(self.type_frame, text=hint, style="Subtitle.TLabel",
                  wraplength=self._p(400), justify='left').grid(
                      row=row + 1, column=1, sticky='w', pady=(0, self._p(6)))
        return row + 2

    def _redis_fields(self, row):
        ttk.Label(self.type_frame, text="Redis").grid(row=row, column=0, sticky='w',
                                                      padx=(0, self._p(12)), pady=self._p(4))
        box = ttk.Frame(self.type_frame)
        box.grid(row=row, column=1, sticky='w', pady=self._p(4))
        ttk.Label(box, text="host").pack(side='left', padx=(0, self._p(6)))
        ttk.Entry(box, textvariable=self.vars["redis_host"], width=18).pack(
            side='left', padx=(0, self._p(12)))
        ttk.Label(box, text="port").pack(side='left', padx=(0, self._p(6)))
        ttk.Entry(box, textvariable=self.vars["redis_port"], width=8).pack(side='left')
        ttk.Label(self.type_frame,
                  text="Optional — filling these in adds the Redis Lists panel to the page.",
                  style="Subtitle.TLabel").grid(row=row + 1, column=1, sticky='w',
                                                pady=(0, self._p(6)))
        return row + 2

    def _on_type_change(self, _event=None):
        """Swap in the fields the selected type actually needs."""
        kind = self._type_key()
        self.type_hint.configure(
            text=next(desc for key, _, desc in SERVICE_TYPES if key == kind))
        for child in self.type_frame.winfo_children():
            child.destroy()

        row = 0
        if kind in ("process", "task"):
            row = self._field(row, "Directory", "dir",
                              "Where the command runs. ${tokens} are expanded — the path "
                              "roots from Settings, plus this project's own fields.")
            row = self._field(row, "Command", "command",
                              "Split on spaces, so paths with spaces won't work. "
                              "${python} is expanded.")
        if kind == "task":
            row = self._field(row, "Section", "section",
                              "Optional heading this task is grouped under in the sidebar, "
                              "e.g. Migrations. Defaults to Tasks.")
            ttk.Checkbutton(self.type_frame, text="Interactive (adds a Reply box for prompts)",
                            variable=self.flags["interactive"]).grid(
                                row=row, column=1, sticky='w', pady=self._p(4))
            row += 1
        elif kind == "cron":
            row = self._field(row, "URL", "url", "Called on every tick, with TLS verification off.")
            row = self._field(row, "Interval (s)", "interval", "Seconds between calls.")
        elif kind == "winservice":
            row = self._field(row, "Service name", "service_name",
                              "The service's name in services.msc, e.g. wampapache64. "
                              "Controlling it prompts for UAC.")
        elif kind == "logview":
            row = self._field(row, "Log file", "log_file",
                              "Tailed into the page, including after the file is rotated.")

        if kind in ("process", "winservice"):
            row = self._field(row, "Log file", "log_file",
                              "Optional — a file to tail into this page alongside anything "
                              "the service prints. ${tokens} are expanded.")
            row = self._redis_fields(row)

        # Only things that can be started take part in Start All / auto-start.
        if kind in ("process", "cron", "winservice"):
            self.startup_frame.grid()
        else:
            self.startup_frame.grid_remove()
        self.update_idletasks()

    # ------------------------------------------------------------- committing

    def _save(self):
        try:
            name, info = self._collect()
        except ValueError as error:
            messagebox.showerror("Service", str(error), parent=self)
            return
        self.ui.commit_service(self.original_name, name, info)
        self.destroy()

    def _collect(self):
        """Read the form and return (name, service definition). Raises ValueError.

        The form only turns its fields into a definition; the rules every definition must
        meet live in config_manager.validate_service, shared with the control API."""
        sm = self.ui.service_manager
        name = self.vars["name"].get().strip()
        apps = [app_id for app_id, var in self.app_vars.items() if var.get()]

        kind = self._type_key()
        # Start from whatever the entry already had that this form doesn't manage, so a
        # hand-added key survives a round-trip through the dialog.
        info = {key: value for key, value in self.original_info.items()
                if key not in EDITOR_KEYS}
        info["app"] = apps[0] if len(apps) == 1 else apps
        info["type"] = kind

        if kind in ("process", "task"):
            info["dir"] = self.vars["dir"].get().strip()
            info["command"] = self.vars["command"].get().split()

        if kind == "task":
            info["type"] = "task"
            section = self.vars["section"].get().strip()
            if section:
                info["section"] = section
            if self.flags["interactive"].get():
                info["interactive"] = True
        elif kind == "cron":
            url = self.vars["url"].get().strip()
            if not url:
                raise ValueError("Enter the URL to call.")
            try:
                interval = int(self.vars["interval"].get().strip())
                if interval <= 0:
                    raise ValueError
            except ValueError:
                raise ValueError("The interval must be a positive whole number of seconds.")
            info.update({"type": "cron", "url": url, "interval": interval})
        elif kind == "winservice":
            service_name = self.vars["service_name"].get().strip()
            if not service_name:
                raise ValueError("Enter the Windows service name, as shown in services.msc.")
            info.update({"type": "winservice", "service_name": service_name})
        elif kind == "logview":
            log_file = self.vars["log_file"].get().strip()
            if not log_file:
                raise ValueError("Point the log viewer at a file.")
            info.update({"type": "logview", "log_file": log_file})

        if kind in ("process", "winservice"):
            log_file = self.vars["log_file"].get().strip()
            if log_file:
                info["log_file"] = log_file
            redis = self._redis_block()
            if redis:
                info["redis"] = redis

        if kind in ("process", "cron", "winservice"):
            start_all = self.flags["include_in_start_all"].get()
            # A shared service can carry a per-project object here; leave it intact unless
            # the checkbox was actually moved, which would flatten it to one value.
            if isinstance(self.original_start_all, dict) and start_all == self.shown_start_all:
                info["include_in_start_all"] = self.original_start_all
            else:
                info["include_in_start_all"] = start_all
            info["autostart"] = bool(self.flags["autostart"].get())

        return config_manager.validate_service(name, info, sm.services, sm.app_id_list,
                                               self.original_name)

    def _redis_block(self):
        host = self.vars["redis_host"].get().strip()
        port = self.vars["redis_port"].get().strip()
        if not host and not port:
            return None
        if not host:
            raise ValueError("Enter a Redis host, or clear the port to drop the Lists panel.")
        try:
            return {"host": host, "port": int(port or 6379)}
        except ValueError:
            raise ValueError("The Redis port must be a whole number.")


class AppDialog(ModalDialog):
    """Add or edit a project: its rail tile, its ${tokens} and its optional build page."""

    ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
    COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")

    def __init__(self, ui, app_id=None):
        super().__init__(ui, "Edit project" if app_id else "Add project")
        self.app_id = app_id
        self.original = dict(ui.service_manager.app_by_id.get(app_id, {})) if app_id else {}

        self.vars = {key: tk.StringVar() for key in
                     ("id", "name", "short", "color",
                      "build_label", "build_path", "build_command")}
        self.build_enabled = tk.BooleanVar()
        self.token_rows = []

        self._load()
        self._build()
        self._on_build_toggle()
        self.present()

    def _load(self):
        app = self.original
        build = app.get("build") or {}
        self.vars["id"].set(app.get("id", ""))
        self.vars["name"].set(app.get("name", ""))
        self.vars["short"].set(app.get("short", ""))
        self.vars["color"].set(app.get("color", "#4c9aff"))
        self.vars["build_label"].set(build.get("label", ""))
        self.vars["build_path"].set(build.get("path", ""))
        self.vars["build_command"].set(build.get("command", "npm run build"))
        self.build_enabled.set(bool(app.get("build")))

    def _build(self):
        body = ttk.Frame(self, padding=self._p(20))
        body.pack(fill='both', expand=True)
        body.columnconfigure(1, weight=1)
        row = 0

        ttk.Label(body, text="Id").grid(row=row, column=0, sticky='w',
                                        padx=(0, self._p(12)), pady=self._p(5))
        id_entry = ttk.Entry(body, textvariable=self.vars["id"], width=42)
        id_entry.grid(row=row, column=1, sticky='ew', pady=self._p(5))
        row += 1
        if self.app_id:
            # Services reference an app by id, so renaming it here would orphan them.
            id_entry.configure(state="readonly")
            hint = "The id is fixed — every service refers to the project by it."
        else:
            hint = "Lower-case, no spaces, e.g. srp. Services refer to the project by this."
        ttk.Label(body, text=hint, style="Subtitle.TLabel").grid(
            row=row, column=1, sticky='w', pady=(0, self._p(6)))
        row += 1

        for label, key in (("Name", "name"), ("Short", "short")):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky='w',
                                             padx=(0, self._p(12)), pady=self._p(5))
            ttk.Entry(body, textvariable=self.vars[key], width=42).grid(
                row=row, column=1, sticky='ew', pady=self._p(5))
            row += 1
        ttk.Label(body, text="Short is the 2–3 character monogram on the rail tile.",
                  style="Subtitle.TLabel").grid(row=row, column=1, sticky='w',
                                                pady=(0, self._p(6)))
        row += 1

        ttk.Label(body, text="Colour").grid(row=row, column=0, sticky='w',
                                            padx=(0, self._p(12)), pady=self._p(5))
        colour_box = ttk.Frame(body)
        colour_box.grid(row=row, column=1, sticky='w', pady=self._p(5))
        ttk.Entry(colour_box, textvariable=self.vars["color"], width=12).pack(side='left')
        self.swatch = tk.Label(colour_box, bg=self._safe_colour(), width=3, bd=0)
        self.swatch.pack(side='left', padx=self._p(8))
        ttk.Button(colour_box, text="Pick", command=self._pick_colour).pack(side='left')
        self.vars["color"].trace_add("write", lambda *_: self._update_swatch())
        row += 1

        ttk.Separator(body, orient='horizontal').grid(row=row, column=0, columnspan=2,
                                                      sticky='ew', pady=self._p(10))
        row += 1

        ttk.Label(body, text="Tokens", style="Section.TLabel").grid(row=row, column=0,
                                                                    sticky='w')
        ttk.Label(body,
                  text="Reusable values services can write as ${name} in a directory or "
                       "command, e.g. python or project_dir.",
                  style="Subtitle.TLabel", wraplength=self._p(380), justify='left').grid(
                      row=row, column=1, sticky='w')
        row += 1

        self.tokens_box = ttk.Frame(body)
        self.tokens_box.grid(row=row, column=0, columnspan=2, sticky='ew', pady=self._p(6))
        row += 1
        for key, value in self.original.items():
            if key not in APP_RESERVED_KEYS and isinstance(value, str):
                self._add_token_row(key, value)

        ttk.Button(body, text="Add token", command=lambda: self._add_token_row()).grid(
            row=row, column=0, columnspan=2, sticky='w')
        row += 1

        ttk.Separator(body, orient='horizontal').grid(row=row, column=0, columnspan=2,
                                                      sticky='ew', pady=self._p(10))
        row += 1

        ttk.Checkbutton(body, text="Front-end build page", variable=self.build_enabled,
                        command=self._on_build_toggle).grid(row=row, column=0, columnspan=2,
                                                            sticky='w')
        row += 1
        self.build_frame = ttk.Frame(body)
        self.build_frame.grid(row=row, column=0, columnspan=2, sticky='ew', pady=self._p(6))
        self.build_frame.columnconfigure(1, weight=1)
        for i, (label, key) in enumerate((("Label", "build_label"), ("Project path", "build_path"),
                                          ("Command", "build_command"))):
            ttk.Label(self.build_frame, text=label).grid(row=i, column=0, sticky='w',
                                                         padx=(0, self._p(12)), pady=self._p(4))
            ttk.Entry(self.build_frame, textvariable=self.vars[key], width=42).grid(
                row=i, column=1, sticky='ew', pady=self._p(4))
        row += 1

        buttons = ttk.Frame(body)
        buttons.grid(row=row, column=0, columnspan=2, sticky='e', pady=(self._p(16), 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(
            side='right', padx=(self._p(8), 0))
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self._save).pack(
            side='right')

    # ------------------------------------------------------------- sub-widgets

    def _safe_colour(self):
        colour = self.vars["color"].get().strip()
        return colour if self.COLOR_PATTERN.match(colour) else RAIL_TILE_BG

    def _update_swatch(self):
        self.swatch.configure(bg=self._safe_colour())

    def _pick_colour(self):
        chosen = colorchooser.askcolor(color=self._safe_colour(), parent=self,
                                       title="Project colour")
        if chosen and chosen[1]:
            self.vars["color"].set(chosen[1])

    def _add_token_row(self, key="", value=""):
        row = ttk.Frame(self.tokens_box)
        row.pack(fill='x', pady=self._p(2))
        key_var, value_var = tk.StringVar(value=key), tk.StringVar(value=value)

        ttk.Entry(row, textvariable=key_var, width=14).pack(side='left')
        ttk.Label(row, text="=").pack(side='left', padx=self._p(6))
        ttk.Entry(row, textvariable=value_var, width=34).pack(side='left', fill='x', expand=True)

        entry = (row, key_var, value_var)
        ttk.Button(row, text="✕", width=3,
                   command=lambda: self._remove_token_row(entry)).pack(side='left',
                                                                       padx=(self._p(6), 0))
        self.token_rows.append(entry)

    def _remove_token_row(self, entry):
        entry[0].destroy()
        self.token_rows.remove(entry)

    def _on_build_toggle(self):
        if self.build_enabled.get():
            self.build_frame.grid()
        else:
            self.build_frame.grid_remove()
        self.update_idletasks()

    # ------------------------------------------------------------- committing

    def _save(self):
        try:
            app = self._collect()
        except ValueError as error:
            messagebox.showerror("Project", str(error), parent=self)
            return
        self.ui.commit_app(self.app_id, app)
        self.destroy()

    def _collect(self):
        sm = self.ui.service_manager
        app_id = self.vars["id"].get().strip().lower()
        if not self.ID_PATTERN.match(app_id):
            raise ValueError("The id must be lower-case letters, digits, - or _, "
                             "starting with a letter or digit.")
        if self.app_id is None and app_id in sm.app_by_id:
            raise ValueError(f"A project with the id '{app_id}' already exists.")

        name = self.vars["name"].get().strip()
        if not name:
            raise ValueError("Give the project a name.")

        colour = self.vars["color"].get().strip()
        if not self.COLOR_PATTERN.match(colour):
            raise ValueError("The colour must be a hex value like #4c9aff.")

        app = {
            "id": app_id,
            "name": name,
            "short": (self.vars["short"].get().strip() or app_id[:3]).upper(),
            "color": colour,
        }

        for _, key_var, value_var in self.token_rows:
            key = key_var.get().strip()
            if not key:
                continue
            if key in APP_RESERVED_KEYS:
                raise ValueError(f"'{key}' is a built-in field and can't be used as a token.")
            app[key] = value_var.get().strip()

        if self.build_enabled.get():
            path = self.vars["build_path"].get().strip()
            if not path:
                raise ValueError("Enter the project path for the build page, "
                                 "or untick the build option.")
            app["build"] = {
                "label": self.vars["build_label"].get().strip() or "Build",
                "path": path,
                "command": self.vars["build_command"].get().strip() or "npm run build",
            }

        # Keep anything on the original definition this form doesn't cover (a nested block
        # someone added by hand), rather than dropping it on save.
        for key, value in self.original.items():
            if key not in app and key not in APP_RESERVED_KEYS and not isinstance(value, str):
                app[key] = value

        return app
