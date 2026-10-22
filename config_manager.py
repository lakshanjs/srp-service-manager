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
    # Live updates (the till, the kitchen screen): `manage.py run_centrifugo` starts the
    # Centrifugo program with the secrets from this PC's settings_local.py, so nothing secret
    # lives here and the entry is the same on every PC. Each developer only needs
    # centrifugo.exe in ~/.neobe-centrifugo (or on PATH). Port 8100; SRP's is separate.
    "Neo Centrifugo": {
        "app": "neo",
        "dir": "${project_dir}",
        "command": ["${python}", "manage.py", "run_centrifugo"],
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


# ------------------------------------------------------------ service schema

SERVICE_KINDS = ("process", "cron", "winservice", "logview", "task")

# The fields each kind of service uses. A kind's own fields are kept; another kind's are
# dropped, so switching a process to a URL poll can't leave a stale `command` behind (start
# looks for `command` first). Keys no kind uses, e.g. added by hand, are always kept.
KIND_FIELDS = {
    "process": {"dir", "command", "log_file", "redis", "include_in_start_all", "autostart"},
    "task": {"dir", "command", "section", "interactive"},
    "cron": {"url", "interval", "include_in_start_all", "autostart"},
    "winservice": {"service_name", "log_file", "redis", "include_in_start_all", "autostart"},
    "logview": {"log_file"},
}
SCHEMA_FIELDS = set().union(*KIND_FIELDS.values()) | {"app", "type"}


def service_kind(info):
    """A definition's kind. A plain process is the absence of a `type`, except a legacy
    entry that only carries a `url`, which is a URL poll."""
    kind = info.get("type")
    if kind in SERVICE_KINDS:
        return kind
    return "cron" if "url" in info else "process"


def validate_service(name, info, existing, all_app_ids, original_name=None):
    """Check a service definition and return (name, cleaned copy); raise ValueError saying
    what to fix. The service editor and the control API both go through this, so neither
    can write an entry the other would refuse.

    `existing` is the current services mapping; `original_name` is the entry being edited
    (None when adding), so it doesn't count as a clash with itself."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the service a name.")
    if name.startswith("@"):
        # Service names double as page ids, and '@' is what marks the built-in pages.
        raise ValueError("A service name can't start with '@'.")
    if name != original_name and name in existing:
        raise ValueError(f"A service called '{name}' already exists.")

    if not isinstance(info, dict):
        raise ValueError("The definition must be an object.")
    kind = info.get("type") or service_kind(info)
    if kind not in SERVICE_KINDS:
        raise ValueError(f"Unknown type '{kind}'. Use one of: {', '.join(SERVICE_KINDS)}.")

    apps = info.get("app")
    apps = [apps] if isinstance(apps, str) else list(apps or [])
    if not apps:
        raise ValueError("Pick at least one project for this service.")
    unknown = [str(a) for a in apps if a not in all_app_ids]
    if unknown:
        raise ValueError(f"Unknown project(s): {', '.join(unknown)}. "
                         f"Known: {', '.join(all_app_ids)}.")

    allowed = KIND_FIELDS[kind]
    clean = {k: v for k, v in info.items()
             if k not in SCHEMA_FIELDS or k in allowed}  # hand-added keys survive
    clean["app"] = apps[0] if len(apps) == 1 else apps
    if kind == "process":
        clean.pop("type", None)
    else:
        clean["type"] = kind

    def text(key, required=None):
        value = clean.get(key)
        if value is None or value == "":
            if required:
                raise ValueError(required)
            clean.pop(key, None)
            return
        if not isinstance(value, str):
            raise ValueError(f"'{key}' must be text.")
        clean[key] = value.strip()

    if kind in ("process", "task"):
        command = clean.get("command")
        if isinstance(command, str):
            command = command.split()
        if (not isinstance(command, list) or not command
                or not all(isinstance(part, str) and part for part in command)):
            raise ValueError("Enter the command to run (a list of arguments, or one line).")
        clean["command"] = command
        text("dir")
        clean.setdefault("dir", "")
    if kind == "task":
        text("section")
        if clean.get("interactive") is not None and not isinstance(clean["interactive"], bool):
            raise ValueError("'interactive' must be true or false.")
        if not clean.get("interactive"):
            clean.pop("interactive", None)
    elif kind == "cron":
        text("url", "Enter the URL to call.")
        interval = clean.get("interval", 60)
        if isinstance(interval, str) and interval.strip().isdigit():
            interval = int(interval)
        if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
            raise ValueError("The interval must be a positive whole number of seconds.")
        clean["interval"] = interval
    elif kind == "winservice":
        text("service_name", "Enter the Windows service name, as shown in services.msc.")
    elif kind == "logview":
        text("log_file", "Point the log viewer at a file.")
    if kind in ("process", "winservice"):
        text("log_file")
        redis = clean.get("redis")
        if redis in (None, {}):
            clean.pop("redis", None)
        else:
            if not isinstance(redis, dict) or not str(redis.get("host") or "").strip():
                raise ValueError("Enter a Redis host, or drop 'redis' to remove the Lists panel.")
            try:
                port = int(redis.get("port") or 6379)
            except (TypeError, ValueError):
                raise ValueError("The Redis port must be a whole number.")
            clean["redis"] = {**redis, "host": str(redis["host"]).strip(), "port": port}

    if kind in ("process", "cron", "winservice"):
        start_all = clean.get("include_in_start_all", False)
        # A shared service can carry a per-project object: {"srp": true, "neo": false}.
        if isinstance(start_all, dict):
            if not all(a in all_app_ids and isinstance(v, bool) for a, v in start_all.items()):
                raise ValueError("'include_in_start_all' maps project ids to true/false.")
        elif not isinstance(start_all, bool):
            raise ValueError("'include_in_start_all' must be true/false, or per project.")
        clean["include_in_start_all"] = start_all
        if not isinstance(clean.get("autostart", False), bool):
            raise ValueError("'autostart' must be true or false.")
        clean["autostart"] = clean.get("autostart", False)

    return name, clean


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
    # Local control API (127.0.0.1 only, token in control.json) for Claude Code / smctl.
    "control_api": {"enabled": True, "port": 47615},
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
