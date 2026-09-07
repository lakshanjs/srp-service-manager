import copy
import json
import os

CONFIG_FILE_NAME = "services.json"
SETTINGS_FILE_NAME = "appsettings.json"
APP_DIR_NAME = "OnCodes"


# --------------------------------------------------------------------- paths

def config_dir():
    """`%APPDATA%\\OnCodes`, created on demand.

    One location for every way of launching: running from source and running the packaged
    .exe previously each kept their own copy beside whichever binary was in use, so editing
    one had no effect on the other. Created here rather than at first write so the folder
    exists before anything tries to read from it."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def get_config_path():
    return os.path.join(config_dir(), CONFIG_FILE_NAME)


def get_settings_path():
    return os.path.join(config_dir(), SETTINGS_FILE_NAME)


# --------------------------------------------------------------- path roots

# Machine-specific roots, referenced from services and apps as ${name}. Everything that
# would otherwise be a hard-coded absolute path goes through one of these, so moving to
# another PC means correcting these five values instead of every service in the file.
DEFAULT_PATHS = {
    "oncodes": "D:\\OnCodes",
    "srp_tools": "D:\\OnCodes\\SRP_Tools",
    "wamp": "D:\\wamp",
    "envs": "D:\\ENVS",
    "neolution": "D:\\Neolution",
}

# Drives to look on when a root isn't where the default says, most likely first.
PROBE_DRIVES = ("D", "C", "E", "F", "G")


def candidate_dirs(path):
    """Where else the same folder might plausibly live on this machine.

    The path as given, then the same path on each of the other fixed drives, then under the
    user profile — enough to find `D:\\OnCodes` on a PC where it's `C:\\OnCodes`."""
    _, rest = os.path.splitdrive(path)
    yield path
    for letter in PROBE_DRIVES:
        candidate = f"{letter}:{rest}"
        if candidate.lower() != path.lower():
            yield candidate
    yield os.path.join(os.path.expanduser("~"), rest.lstrip("\\/"))


def detect_paths():
    """Best guess at each root on this machine, for a first run on a new PC.

    A root that can't be found anywhere keeps its default, so it still appears on the
    settings page as something to point at rather than silently vanishing."""
    return {name: next((c for c in candidate_dirs(default) if os.path.isdir(c)), default)
            for name, default in DEFAULT_PATHS.items()}


# ------------------------------------------------------------------ defaults

DEFAULT_APPS = [
    {
        "id": "srp",
        "name": "SRP Live Help",
        "short": "SRP",
        "color": "#4c9aff",
        "build": {
            "label": "Nova UI",
            "path": "${oncodes}\\srplivehelp\\nova",
            "command": "npm run build",
        },
    },
    {
        "id": "neo",
        "name": "Neobe",
        "short": "NEO",
        "color": "#3fb950",
        # Django lives in a virtualenv that isn't on PATH, so services and tasks in this
        # app reference it through the ${python} / ${project_dir} tokens below. Those in
        # turn resolve through the machine-wide roots, so nothing here is machine-specific.
        "python": "${envs}\\neobe\\Scripts\\python.exe",
        "project_dir": "${neolution}\\neobe\\neobe",
        # The Vue + Vite PWA Django serves under /pms/app/. Its bundle is committed, so
        # this build page is how it gets regenerated after pulling front-end changes.
        "build": {
            "label": "NeoHotelier App",
            "path": "${project_dir}\\app",
            "command": "npm run build",
        },
    },
]

DEFAULT_SERVICES = {
    # ------------------------------------------------------------------ SRP
    # Directories go through the ${srp_tools} / ${oncodes} / ${wamp} roots instead of naming
    # a drive, so these defaults are correct on any machine once the roots are.
    "Main Centrifugo": {
        "app": "srp",
        "dir": "${srp_tools}\\centrifugo",
        "command": ["centrifugo", "-c", "config.json"],
    },
    "Worker": {
        "app": "srp",
        "dir": "${oncodes}\\srplivehelp\\src\\servers\\workers",
        "command": ["php", "worker.php"],
    },
    "Cron Task": {
        "app": "srp",
        "type": "cron",
        "url": "https://srplh.test/cron/tasks",
        "interval": 60,
    },
    "Memcached": {
        "app": "srp",
        "dir": "${srp_tools}\\memcached\\bin",
        "command": ["memcached.exe"],
    },
    "Ngrok": {
        "app": "srp",
        "dir": "",
        "command": ["ngrok", "http", "--url=in-unicorn-smart.ngrok-free.app", "80"],
    },
    "Tika": {
        "app": "srp",
        "dir": "${srp_tools}\\tika",
        "command": ["java", "-jar", "tika-server-standard-2.8.0.jar"],
        "include_in_start_all": False,
    },
    "Elasticsearch": {
        "app": "srp",
        "dir": "${srp_tools}\\elasticsearch\\bin",
        "command": ["elasticsearch.bat"],
        "include_in_start_all": False,
    },
    "WAMP Apache": {
        "app": "srp",
        "type": "winservice",
        "service_name": "wampapache64",
        "log_file": "${wamp}\\logs\\apache_error.log",
        "include_in_start_all": False,
        "autostart": False,
    },
    "WAMP MariaDB": {
        "app": "srp",
        "type": "winservice",
        "service_name": "wampmariadb64",
        "log_file": "${wamp}\\logs\\mariadb.log",
        "include_in_start_all": False,
        "autostart": False,
    },
    "PHP Errors": {
        "app": "srp",
        "type": "logview",
        "log_file": "${wamp}\\logs\\php_error.log",
    },

    # --------------------------------------------------------------- shared
    # Both stacks talk to these, so they're listed under both apps and appear in both
    # sidebars. Stop All in one app leaves them running while the other app still needs
    # them; only the last app standing actually stops them.
    "MySQL": {
        "app": ["srp", "neo"],
        "type": "winservice",
        "service_name": "wampmysqld64",
        "log_file": "${wamp}\\logs\\mysql.log",
        "include_in_start_all": {"srp": False, "neo": True},
        "autostart": False,
    },
    "Memurai": {
        "app": ["srp", "neo"],
        "type": "winservice",
        "service_name": "Memurai",
        "redis": {"host": "127.0.0.1", "port": 6379},
        "include_in_start_all": {"srp": False, "neo": True},
        "autostart": False,
    },

    # ------------------------------------------------------------------ NEO
    "Django Dev Server": {
        "app": "neo",
        "dir": "${project_dir}",
        "command": ["${python}", "manage.py", "runserver", "0.0.0.0:8000"],
        "include_in_start_all": True,
        "autostart": False,
    },
    # `npm run dev` for the PWA: Vite serves the modules on :5173 while Django still
    # serves the page. Long-running like the Django server, so it's a service with a
    # status dot and a Stop button rather than a one-shot task.
    "Vite Dev Server": {
        "app": "neo",
        "dir": "${project_dir}\\app",
        "command": ["npm", "run", "dev"],
        "include_in_start_all": True,
        "autostart": False,
    },
    "Celery Worker": {
        "app": "neo",
        "dir": "${project_dir}",
        "command": ["${python}", "-m", "celery", "-A", "neobe", "worker", "-l", "info", "-P", "solo"],
        # settings_local sets CELERY_ALWAYS_EAGER, so the worker isn't needed by default.
        "include_in_start_all": False,
        "autostart": False,
    },

    # NEO one-shot management commands: run, stream output, exit. No status dot, and
    # they're excluded from Start All / Stop All and the startup grid.
    "makemigrations": {
        "app": "neo",
        "type": "task",
        "section": "Migrations",
        "dir": "${project_dir}",
        "command": ["${python}", "manage.py", "makemigrations"],
        "interactive": True,
    },
    "migrate": {
        "app": "neo",
        "type": "task",
        "section": "Migrations",
        "dir": "${project_dir}",
        "command": ["${python}", "manage.py", "migrate"],
    },
    "showmigrations": {
        "app": "neo",
        "type": "task",
        "section": "Migrations",
        "dir": "${project_dir}",
        "command": ["${python}", "manage.py", "showmigrations"],
    },
    "collectstatic": {
        "app": "neo",
        "type": "task",
        "section": "Tasks",
        "dir": "${project_dir}",
        "command": ["${python}", "manage.py", "collectstatic", "--noinput"],
    },
    "createsuperuser": {
        "app": "neo",
        "type": "task",
        "section": "Tasks",
        "dir": "${project_dir}",
        "command": ["${python}", "manage.py", "createsuperuser"],
        "interactive": True,
    },
    "check": {
        "app": "neo",
        "type": "task",
        "section": "Tasks",
        "dir": "${project_dir}",
        "command": ["${python}", "manage.py", "check"],
    },
}


# ------------------------------------------------------------------- loading

def _fresh_config():
    """The shipped defaults, with the path roots detected for this machine."""
    return {
        "paths": detect_paths(),
        "apps": copy.deepcopy(DEFAULT_APPS),
        "services": copy.deepcopy(DEFAULT_SERVICES),
    }


def load_config():
    """Return `{"paths": {...}, "apps": [...], "services": {...}}`, creating it if needed."""
    config_path = get_config_path()
    try:
        # utf-8-sig, not utf-8: this file is meant to be hand-edited, and Notepad /
        # PowerShell redirection both save a UTF-8 BOM. Plain utf-8 raises on the BOM,
        # which would look exactly like a corrupt file and silently reset the config.
        with open(config_path, 'r', encoding='utf-8-sig') as file:
            data = json.load(file)
    except FileNotFoundError:
        # First run on this machine: write the defaults out so there's something to edit.
        data = _fresh_config()
        save_config(data["apps"], data["services"], data["paths"])
        return data
    except json.JSONDecodeError:
        # Never quietly discard a config the user has invested in: keep the broken file
        # alongside the regenerated defaults so their edits can be recovered.
        try:
            os.replace(config_path, config_path + ".bak")
        except OSError:
            pass
        data = _fresh_config()
        save_config(data["apps"], data["services"], data["paths"])
        return data

    # A config written before path roots existed keeps working — its absolute paths are
    # left alone and simply contain no tokens to expand.
    data.setdefault("paths", dict(DEFAULT_PATHS))
    data.setdefault("apps", copy.deepcopy(DEFAULT_APPS))
    data.setdefault("services", {})
    return data


def save_config(apps, services, paths=None):
    document = {"paths": paths if paths is not None else dict(DEFAULT_PATHS),
                "apps": apps, "services": services}
    with open(get_config_path(), 'w', encoding='utf-8') as file:
        json.dump(document, file, indent=4)


def default_apps():
    """A private copy of the shipped project definitions.

    Used to restore parts of a project a config predates — a `build` block added to the
    defaults after the file on this machine was first written."""
    return copy.deepcopy(DEFAULT_APPS)


def default_services():
    """A private copy of the shipped service definitions.

    Deep-copied because the caller merges these into the live config, and a shared nested
    list (a `command`) edited afterwards would silently mutate the defaults themselves."""
    return copy.deepcopy(DEFAULT_SERVICES)


# --------------------------------------------------------------- app helpers

def app_ids(info, all_app_ids):
    """The app(s) a service belongs to, as a list. Unknown/missing -> the first app."""
    raw = info.get("app")
    if isinstance(raw, str):
        ids = [raw]
    elif isinstance(raw, (list, tuple)):
        ids = list(raw)
    else:
        ids = []
    ids = [a for a in ids if a in all_app_ids]
    return ids or ([all_app_ids[0]] if all_app_ids else [])


def is_shared(info, all_app_ids):
    return len(app_ids(info, all_app_ids)) > 1


# Fields the app defines itself on a project. Every *other* scalar key on an app is a
# ${token} its services can use, which is what makes this list worth naming in one place.
APP_RESERVED_KEYS = {"id", "name", "short", "color", "build"}

# Tokens can point at other tokens (a service's ${python} is an app value that itself
# contains ${envs}), so expansion repeats until nothing changes. The cap is what stops a
# config that references itself — ${a} = "${b}", ${b} = "${a}" — from spinning forever.
MAX_TOKEN_PASSES = 5


def expand_tokens(value, scope):
    """Substitute ${name} from `scope` in a string or list of strings.

    `scope` is any flat name -> value mapping: the path roots, an app definition, or the
    two merged (which is what services get). Unknown tokens are left as they are, so a typo
    shows up in the command line rather than silently becoming an empty string."""
    if not scope:
        return value
    if isinstance(value, list):
        return [expand_tokens(v, scope) for v in value]
    if not isinstance(value, str):
        return value

    for _ in range(MAX_TOKEN_PASSES):
        before = value
        for key, replacement in scope.items():
            if isinstance(replacement, str):
                value = value.replace("${%s}" % key, replacement)
        if value == before:
            break
    return value


# ------------------------------------------------------- application settings

DEFAULT_SETTINGS = {
    "window": {"width": 1120, "height": 680},
    "last_app": None,
    # Launch behaviour
    "start_minimized": False,     # go straight to the tray instead of showing the window
    "autostart_enabled": True,    # master switch for the per-service `autostart` flags
    "check_updates": True,        # look for a new release on launch
    # Where releases are published. The repo is public, so the token is optional — it
    # only matters if the repo is ever made private, or to lift the 60-an-hour cap on
    # unauthenticated API calls. It lives here, per machine in %APPDATA%, and is never
    # bundled into the .exe, which gets copied around and can't hold a credential safely.
    "updates": {"repo": "lakshanjs/srp-service-manager", "token": ""},
    # Closing behaviour
    "minimize_to_tray_on_close": False,  # [X] hides to the tray instead of exiting
    "confirm_on_exit": False,            # ask before stopping services and quitting
}


def load_settings():
    settings_path = get_settings_path()
    try:
        with open(settings_path, 'r', encoding='utf-8-sig') as file:  # tolerate a BOM
            data = json.load(file)
    except FileNotFoundError:
        data = {}
    except json.JSONDecodeError:
        data = {}

    # Drop in any missing defaults without clobbering values the user has changed.
    for key, default in DEFAULT_SETTINGS.items():
        if isinstance(default, dict):
            merged = dict(default)
            merged.update(data.get(key) or {})
            data[key] = merged
        else:
            data.setdefault(key, default)

    if not os.path.exists(settings_path):
        save_settings(data)  # create the file on first run

    return data


def save_settings(settings):
    with open(get_settings_path(), 'w', encoding='utf-8') as file:
        json.dump(settings, file, indent=4)
