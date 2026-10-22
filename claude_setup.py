"""Register this app's MCP server with Claude Code, and the CLAUDE.md note that goes with it.

Install and remove go through the official `claude mcp` commands, so Claude Code stays the
one writing its own config. Status is read straight from ~/.claude.json instead: `claude mcp
list` health-checks (i.e. launches) every registered server, which takes seconds.

The registered command depends on how the app is running. The packaged .exe serves MCP
itself (`… .exe --mcp`), so it needs no Python; from source, it's python + mcp_server.py.
Either way it points at *this* copy, so moving the .exe means installing again.

One exception: an .exe set to "Run as administrator" (a Windows compatibility flag on its
path) can't be launched by Claude Code, which isn't elevated. Then a copy of the .exe in
%LOCALAPPDATA%\\OnCodes\\mcp is registered instead, where the flag doesn't apply, and the
app refreshes that copy each time it starts (refresh_mcp_copy) so it matches the app."""

import os
import sys
import glob
import json
import time
import shutil
import winreg
import subprocess

SERVER_NAME = "oncodes-service-manager"
# Earlier names this server was registered under, cleaned up by install.
LEGACY_NAMES = ("service-manager",)
CREATE_NO_WINDOW = 0x08000000
CLI_TIMEOUT = 60

# The note in ~/.claude/CLAUDE.md that tells Claude to use the tools instead of starting
# servers itself. Kept between markers so it can be updated or removed without touching
# anything else in the file.
NOTE_START = "<!-- oncodes-service-manager:start -->"
NOTE_END = "<!-- oncodes-service-manager:end -->"
NOTE = f"""{NOTE_START}
# Dev servers: use OnCodes Dev Service Manager

The dev servers for the projects in OnCodes Dev Service Manager (Django runserver, Centrifugo,
Celery, Vite, PHP/Apache, MySQL/MariaDB, Memurai/Redis and the like, plus one-shot manage.py
tasks) run in that app, which I watch while you work.

- Never start, stop or kill those servers yourself from a shell (`manage.py runserver`,
  `centrifugo`, `celery`, `npm run dev`, `taskkill`, ...). A second copy fights the managed
  one for its port, and I can't see its output.
- Use the `{SERVER_NAME}` MCP tools instead: `service_status`, `service_logs`,
  `restart_service` (pass `wait` to get the startup output), `start_service`,
  `stop_service`, `run_task`, `task_input`, `start_all` / `stop_all`, and
  `get_service_config` / `add_service` / `update_service` / `remove_service`.
- To see what a change did, restart with `wait`, or take `cursor_before` from the reply and
  read `service_logs` with `since` set to it.
- If the tools are unavailable or the app isn't running, tell me and wait. Don't fall back
  to starting servers yourself.
{NOTE_END}
"""
# The same note as first written by hand, before it had markers.
LEGACY_NOTE_HEADING = "# Dev servers: use OnCodes Dev Service Manager"


COMPAT_LAYERS = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
MCP_COPY_NAME = "OnCodes Service Manager MCP.exe"


def runs_as_admin(path):
    """True if Windows is set to always run `path` as administrator (Properties ->
    Compatibility -> "Run this program as an administrator", for this user or all)."""
    target = os.path.normcase(os.path.abspath(path))
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, COMPAT_LAYERS) as key:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    if (os.path.normcase(name) == target
                            and "RUNASADMIN" in str(value).upper().split()):
                        return True
                    i += 1
        except OSError:
            continue
    return False


def mcp_copy_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "OnCodes", "mcp", MCP_COPY_NAME)


def uses_copy():
    """Whether this app has to register the copy rather than itself (see the module doc)."""
    return getattr(sys, "frozen", False) and runs_as_admin(sys.executable)


def refresh_mcp_copy(only_if_present=False):
    """Make the copy match the running .exe, if this app uses one. Install creates it; at
    startup (only_if_present) an existing copy is brought up to date after a deploy.

    Open Claude Code sessions keep the copy running, and Windows won't overwrite or delete
    a running .exe, but it will rename one: so the old copy is moved aside first and
    cleaned up once nothing has it open. Returns an error message, or None."""
    target = mcp_copy_path()
    if not uses_copy() or (only_if_present and not os.path.exists(target)):
        return None
    try:
        src = os.stat(sys.executable)
        try:
            dst = os.stat(target)
            if dst.st_size == src.st_size and int(dst.st_mtime) == int(src.st_mtime):
                return None  # already this build
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target):
            os.replace(target, f"{target}.old-{int(time.time())}")
        shutil.copy2(sys.executable, target)  # copy2 keeps the mtime the check compares
    except OSError as e:
        return f"Couldn't update the MCP copy at {target}: {e}"
    finally:
        for old in glob.glob(glob.escape(target) + ".old-*"):
            try:
                os.remove(old)
            except OSError:
                pass  # still in use by an open session; next start gets it
    return None


def server_command():
    """The command Claude Code should run to start this copy's MCP server."""
    if getattr(sys, "frozen", False):
        return [mcp_copy_path() if uses_copy() else sys.executable, "--mcp"]
    python = sys.executable
    # pythonw has no stdio to speak MCP over; use the console python beside it.
    console = os.path.join(os.path.dirname(python), "python.exe")
    if os.path.basename(python).lower() == "pythonw.exe" and os.path.exists(console):
        python = console
    return [python, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")]


def find_cli():
    """Path to the `claude` command, or None. A window started from Explorer may not
    have the user's PATH additions yet, so the usual install locations are tried too."""
    found = shutil.which("claude")
    if found:
        return found
    home = os.path.expanduser("~")
    for candidate in (os.path.join(home, ".local", "bin", "claude.exe"),
                      os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd")):
        if os.path.exists(candidate):
            return candidate
    return None


def _claude_json():
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def registration(name=SERVER_NAME):
    """The user-scope entry Claude Code has for `name`, or None."""
    return (_claude_json().get("mcpServers") or {}).get(name)


def _command_of(entry):
    return [entry.get("command", "")] + list(entry.get("args") or [])


def _same_command(a, b):
    norm = lambda parts: [os.path.normcase(os.path.normpath(p)) if os.path.isabs(p) else p
                          for p in parts]
    return norm(a) == norm(b)


def status():
    """What the settings card shows: whether the CLI is there, whether the server is
    installed, and whether it runs this copy of the app."""
    entry = registration()
    return {"cli": find_cli(),
            "entry": entry,
            "installed": entry is not None,
            "current": entry is not None and _same_command(_command_of(entry), server_command()),
            "command": server_command(),
            "legacy": [n for n in LEGACY_NAMES if registration(n) is not None],
            "copy": uses_copy(),
            "note": has_note()}


def _run(cli, *args):
    result = subprocess.run([cli, *args], capture_output=True, text=True, timeout=CLI_TIMEOUT,
                            creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="replace")
    return result.returncode, (result.stdout + result.stderr).strip()


def install():
    """Register this copy's server for every project (replacing an older registration,
    including one under a legacy name). Returns (ok, message)."""
    cli = find_cli()
    if not cli:
        return False, "Claude Code's `claude` command wasn't found. Install Claude Code first."
    problem = refresh_mcp_copy()  # the copy has to exist before it's registered
    if problem:
        return False, problem
    for name in (SERVER_NAME,) + LEGACY_NAMES:
        if registration(name) is not None:
            _run(cli, "mcp", "remove", name, "--scope", "user")
    code, out = _run(cli, "mcp", "add", "--scope", "user", SERVER_NAME, "--", *server_command())
    if code != 0:
        return False, out or f"`claude mcp add` failed (exit code {code})."
    return True, "Installed for all projects. Restart open Claude Code sessions to pick it up."


def uninstall():
    cli = find_cli()
    if not cli:
        return False, "Claude Code's `claude` command wasn't found."
    for name in (SERVER_NAME,) + LEGACY_NAMES:
        if registration(name) is None:
            continue
        code, out = _run(cli, "mcp", "remove", name, "--scope", "user")
        if code != 0 and registration(name) is not None:
            return False, out or f"`claude mcp remove {name}` failed (exit code {code})."
    return True, "Removed. Open Claude Code sessions keep it until they're restarted."


def manual_commands():
    """(install, remove) as lines to paste into a terminal."""
    add = subprocess.list2cmdline(["claude", "mcp", "add", "--scope", "user", SERVER_NAME,
                                   "--", *server_command()])
    return add, f"claude mcp remove {SERVER_NAME} --scope user"


# ------------------------------------------------------------------ CLAUDE.md

def claude_md_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "CLAUDE.md")


def _read_md():
    try:
        with open(claude_md_path(), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def has_note():
    text = _read_md()
    return NOTE_START in text or LEGACY_NOTE_HEADING in text


def _without_note(text):
    if NOTE_START in text and NOTE_END in text:
        before, rest = text.split(NOTE_START, 1)
        return before.rstrip() + "\n" + rest.split(NOTE_END, 1)[1].lstrip("\n")
    if LEGACY_NOTE_HEADING in text:
        # The hand-written version ran from its heading to the next heading (or the end).
        before, rest = text.split(LEGACY_NOTE_HEADING, 1)
        nxt = rest.find("\n# ")
        return before.rstrip() + "\n" + (rest[nxt + 1:] if nxt != -1 else "")
    return text


def add_note():
    """Add (or refresh) the note in ~/.claude/CLAUDE.md, leaving the rest of the file be."""
    text = _without_note(_read_md()).strip()
    os.makedirs(os.path.dirname(claude_md_path()), exist_ok=True)
    with open(claude_md_path(), "w", encoding="utf-8") as f:
        f.write((text + "\n\n" if text else "") + NOTE)


def remove_note():
    text = _without_note(_read_md())
    if text.strip():
        with open(claude_md_path(), "w", encoding="utf-8") as f:
            f.write(text.strip() + "\n")
    elif os.path.exists(claude_md_path()):
        os.remove(claude_md_path())  # nothing but our note was ever in it
