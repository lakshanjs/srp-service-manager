# OnCodes Dev Service Manager

A Windows dev-stack controller for **multiple projects at once**. Start, stop and monitor the
long-running services each project needs, run one-shot management commands, and keep the
infrastructure they *share* from being pulled out from under one another.

Forked from SRP Service Manager and generalised: an app rail, per-app scoping, one-shot
tasks, and virtualenv-aware commands.

## Why one app instead of one per project

SRP Live Help and Neobe run at the same time on the same machine, and they share backing
services:

| Service | Port | Used by |
|---|---|---|
| `Memurai` (Redis) | 6379 | SRP + Neobe (`CHANNEL_LAYERS`) |
| `wampmysqld64` (MySQL) | 3306 | Neobe (`DATABASES.default`) + SRP |
| `wampmariadb64` | 3307 | SRP |
| `wampapache64` | 80 | SRP |

With a separate manager per project, hitting **Stop All** in one window would stop Redis and
MySQL under the other. Two processes can't refcount a shared service without a lock file or
IPC, so both stacks live in one app that tracks them together.

## Features

- **App rail** down the left edge — one tile per project (SRP / NEO), each with a live
  running-count badge, and a **settings** gear at the foot. Click to switch; the sidebar and
  dashboard follow.
- **Shared services** appear in *both* apps' sidebars, tagged `shared`. **Stop All** in one
  app leaves them running while the other app still has its own services up.
- Per-app **Dashboard** with scoped Start All / Restart All / Stop All, a live services
  table, and an activity log.
- **Add, edit and remove services** from the UI — no hand-editing needed for day-to-day
  changes. Projects can be added and edited the same way.
- **Settings page** for path roots, startup/close behaviour, window size, project management,
  and a **raw JSON editor** for both config files with validation and a live reload.
- **Portable config** — no absolute path outside the `paths` block, auto-detected on first
  run, so the same `services.json` works on any PC.
- **One-shot tasks** (`migrate`, `makemigrations`, `collectstatic`, …) with streamed output,
  exit codes, and a **Reply** box for interactive prompts like `createsuperuser`.
- **Virtualenv-aware** commands via `${python}` / `${project_dir}` tokens.
- Service types: child processes, Windows services (via the SCM, with UAC), URL-polling cron
  tasks, log-only viewers, and one-shot tasks.
- Live **status dots**, log tailing with rotation handling, a **Redis Lists** panel for
  Memurai, per-app **front-end build** pages, and minimise-to-tray with per-app submenus.

## Managing services

Everything below can also be done by hand in `services.json` — the UI just writes the same
file.

| Where | What |
|---|---|
| Dashboard → Services → **Add Service** | Create a service, pre-assigned to the project in view |
| Service page → **Edit** | Change any field, including its name, type and which projects own it |
| Service page → **Remove** | Delete it, stopping it first if it's running |
| Settings → Services → **Restore missing defaults** | Re-add any shipped default that isn't in the config; existing entries are never overwritten |

The editor shows only the fields the selected type needs, and validates before saving. A few
things worth knowing:

- Ticking more than one project makes the service **shared**. A shared service that has
  per-project `include_in_start_all` values keeps them unless you actually move the checkbox.
- **Renaming** a running service stops it: every process, log tail and status the app tracks
  is keyed by the service's name. The activity log says so when it happens.
- Removing a `winservice` entry only removes *this app's* entry — the Windows service itself
  is left alone, exactly as on exit.
- Keys you added by hand that the form doesn't cover survive a round-trip through it.

## Requirements

- Windows (system tray, `taskkill`, `sc`/`net`, hidden consoles, dark title bar)
- Python 3.x

```bash
pip install -r requirements.txt
```

## Building the executable

```bash
pyinstaller --noconfirm --clean main.spec
```

The spec bundles the icon, the **`sv-ttk`** theme data (`.tcl` + spritesheets — without it the
app launches unthemed) and the Windows tray backend. Output lands in `dist/`.

Deploy by copying it over `D:\OnCodes\Tools\OnCodes Dev Service Manager.exe` — close the
running app first, or the copy fails on the locked file. Config isn't carried along, so a
redeployed .exe picks up the same `%APPDATA%\OnCodes` files it was already using.

`services.json` and `appsettings.json` are deliberately **not** bundled — see below.

## Updates

The app **checks** for a new release and tells you about it; it doesn't install anything.
Deploying is still the copy above.

Releases are published to `lakshanjs/srp-service-manager` — this fork was copied out of SRP
Service Manager and shares its release feed rather than having a repo of its own:

```json
"updates": {
    "repo": "lakshanjs/srp-service-manager",
    "token": ""
}
```

Both fields are on the settings page under **Updates**. The repo is public, so the **token
is optional** — it raises GitHub's 60-an-hour cap on unauthenticated calls, and is what the
check would need if the repo were ever made private. It is stored in `appsettings.json` in
`%APPDATA%\OnCodes` per machine and is **never bundled into the .exe**, which gets copied
around and can't hold a credential safely. Leave it empty to fall back to
`ONCODES_UPDATE_TOKEN`, `GH_TOKEN` or `GITHUB_TOKEN` from the environment. A fine-grained
token needs **Contents: Read**; a classic one needs the `repo` scope.

To ship an update: build, deploy, then publish a release whose tag is above
`update.CURRENT_VERSION`. **Bump `CURRENT_VERSION` in `update.py` with every build you
deploy** — the check compares the release tag against it (via `packaging`, so `1.10`
correctly beats `1.9`), and a build that still claims the old number reports itself as up to
date forever. The newest release there is `v1.0`, from the SRP-era build.

The launch check runs in the background and only opens a dialog for a real update; a failure
goes to the dashboard activity log instead, because at launch you asked to start services,
not to hear that GitHub is unreachable. **Check for updates now** reports every outcome, and
distinguishes them rather than saying "could not reach GitHub" to all of it: a repo with no
releases yet, a wrong `owner/repo`, a rejected token, a private repo with no token, the API
rate limit, and being offline. GitHub answers 404 identically for "private and you can't see
it", "no releases yet" and "doesn't exist", so a second request tells those apart — the
difference between *add a token* and *publish a release*.

## Configuration

Both files live in:

```
%APPDATA%\OnCodes\
    services.json      services and app definitions
    appsettings.json   window size, last selected app
```

One location for **every** way of launching — running from source and running the packaged
`.exe` read and write the same files, so the `.exe` can be copied anywhere without carrying
config alongside it. Both are created on first run from the shipped defaults, with the
[path roots](#path-roots) detected for that machine.

Files are read as **UTF-8 with or without a BOM**, so editing them in Notepad won't break
them. If `services.json` ever fails to parse it's preserved as `services.json.bak` rather than
silently discarded.

> The app rewrites `services.json` whenever you toggle a checkbox, edit a command, or save
> build settings — so hand-edit it while the app is closed, or use the **JSON editor** on the
> settings page, which saves and reloads in one step.

### `services.json`

```json
{
    "paths": {
        "oncodes": "D:\\OnCodes",
        "srp_tools": "D:\\OnCodes\\SRP_Tools",
        "wamp": "D:\\wamp",
        "envs": "D:\\ENVS",
        "neolution": "D:\\Neolution"
    },
    "apps": [
        {
            "id": "neo",
            "name": "Neobe",
            "short": "NEO",
            "color": "#3fb950",
            "python": "${envs}\\neobe\\Scripts\\python.exe",
            "project_dir": "${neolution}\\neobe\\neobe"
        }
    ],
    "services": { }
}
```

**App fields** — `id` (referenced by services), `name` (sidebar heading), `short` (rail tile
monogram, 2–3 chars), `color` (active tile), plus any scalar key you want to use as a
`${token}`. An optional `build` block (`label`, `path`, `command`) adds a front-end build page.

**Service fields** — every entry takes an `app`, either one id or a list to share it:

```json
"Memurai": {
    "app": ["srp", "neo"],
    "type": "winservice",
    "service_name": "Memurai",
    "include_in_start_all": { "srp": false, "neo": true }
}
```

- **`app`** — `"srp"` or `["srp", "neo"]`. More than one id makes it a shared service.
- **`include_in_start_all`** — a bool, or a per-app object for shared services.
- **`autostart`** — start on launch (a single flag; shared services show it in both apps).

Service types:

| `type` | Behaviour |
|---|---|
| *(none)* + `command` | Child process launched with `dir` as its cwd, stdout/stderr streamed |
| `cron` | Polls `url` every `interval` seconds |
| `winservice` | Controls `service_name` through the SCM; needs UAC. Optional `log_file` is tailed |
| `logview` | No controls — just tails `log_file` |
| `task` | Runs once on demand. Optional `section` groups it in the sidebar; `interactive` adds the Reply box |

### Path roots

Nothing outside the `paths` block names a drive. Every directory, log file and virtualenv is
written as `${root}\rest\of\path`, so **the same config works on any PC once these few values
are right** — which is the whole point of them.

| Root | Holds |
|---|---|
| `${oncodes}` | `srplivehelp`, and `SRP_Tools` beneath it |
| `${srp_tools}` | centrifugo, memcached, tika, elasticsearch |
| `${wamp}` | WAMP, and the `logs\` the Windows services write to |
| `${envs}` | virtualenvs |
| `${neolution}` | the Neobe checkout |

On **first run** each root is looked for at its default, then at the same path on the other
fixed drives, then under your user profile — so `D:\OnCodes` is found on a machine where it's
`C:\OnCodes`. Anything not found keeps its default and is flagged **not found** on the
settings page.

Roots are edited under **Settings → Path roots**, with **Browse**, a live found/not-found
marker per row, and **Detect** to re-run the search. Add your own roots by adding keys to the
`paths` block — any `${name}` in it is expandable.

### `${python}` and `${project_dir}`

A command or directory may also reference any scalar key on its **owning app**, so a
virtualenv path is written once — and that value may itself use a root:

```json
"migrate": {
    "app": "neo",
    "type": "task",
    "section": "Migrations",
    "dir": "${project_dir}",
    "command": ["${python}", "manage.py", "migrate"]
}
```

with `"python": "${envs}\\neobe\\Scripts\\python.exe"` on the app. Expansion repeats until
nothing changes, so a token pointing at a token resolves in one go; a token that points at
itself stops after five passes rather than hanging. An app field **shadows** a root of the
same name, letting one project override a machine-wide value.

The service page shows the expanded command under the Command field. Tokens are kept
unexpanded in the config, so moving the venv only means editing one value.

> Commands are split on whitespace, so a token expanding to a path **with spaces** won't
> work — keep virtualenvs somewhere unspaced (e.g. `D:\ENVS\...`).

### Moving an existing setup to another PC

**Settings → Path roots → Convert paths to tokens** rewrites every absolute path already in
the config — service directories, log files, app fields, build paths — as `${root}` tokens,
matching the longest root first. Copy `services.json` to the new machine and only the roots
need correcting; **Detect** usually does even that.

### `appsettings.json`

Everything on the **Settings** page, plus the app selected on launch:

```json
{
    "window": { "width": 1120, "height": 680 },
    "last_app": "neo",
    "start_minimized": false,
    "autostart_enabled": true,
    "check_updates": true,
    "minimize_to_tray_on_close": false,
    "confirm_on_exit": false
}
```

| Key | Effect |
|---|---|
| `window` | Size the window opens at, in logical pixels — scaled up on high-DPI displays |
| `start_minimized` | Launch straight into the tray without showing the window |
| `autostart_enabled` | Master switch for the per-service `autostart` flags |
| `check_updates` | Ask GitHub for the latest release on launch |
| `minimize_to_tray_on_close` | `[X]` hides to the tray instead of exiting |
| `confirm_on_exit` | Ask before quitting, naming how many services will be stopped |

## Settings page

The gear at the foot of the app rail. Preferences save the moment you change them.

- **Path roots** — the machine-specific folders every other path is written against. See
  [Path roots](#path-roots).
- **Startup / Window / Closing** — the settings above. Window size applies next launch;
  **Use current size** fills it in from the window as it is now.
- **Projects** — add, edit and remove the entries that become rail tiles. The editor covers
  the name, monogram, colour, the `${token}` values services expand, and the optional build
  page. A project's `id` is fixed once created, since services refer to it, and a project
  that still has services can't be removed.
- **Services** — service count, **Add Service**, and **Restore missing defaults**.
- **Configuration files** — a JSON editor for `services.json` and `appsettings.json`.
  **Save & apply** validates the JSON (and, for `services.json`, that every project has an
  `id` and every service points at one that exists), writes the file, then reloads the whole
  app — so it's safe to use while services are running. **Format** re-indents,
  **Reload from disk** discards edits, **Open folder** opens `%APPDATA%\OnCodes`.
- **Updates** — the repository releases come from, its token, and an on-demand check that
  reports being up to date as well as behind. See [Updates](#updates).
- **About** — the running version.

## Usage notes

- **Stop All is scoped**; the per-service **Stop** button is not — it always stops that
  service, shared or not.
- Windows services are never stopped on exit: they were most likely running beforehand, and
  the other stack may still need them. Closing the app stops only the processes it started.
- Controlling a Windows service prompts for **UAC** per action unless the app is already
  elevated. Bulk actions skip services already in the target state to avoid pointless prompts.
- Neobe's `settings_local.py` sets `CELERY_ALWAYS_EAGER`, so **Celery Worker** ships excluded
  from Start All — enable it if you turn eager mode off.
- Neobe's Vue + Vite PWA (`${project_dir}\app`, served by Django under `/pms/app/`) has both
  halves: **Vite Dev Server** (`npm run dev`) as a long-running service on port 5173, and the
  **NeoHotelier App** build page (`npm run build`) for regenerating the committed bundle. Django
  serves the page either way — Vite only serves the modules — so the dev server is what you
  run while working on the front end, and the build page is what you run before committing.
- The shipped SRP defaults follow the `SRP_Tools` layout the tools actually run from, and
  include **Elasticsearch** (excluded from Start All). Defaults only seed a fresh install —
  an existing `services.json` is never rewritten from them, so use **Restore missing
  defaults** to pick up entries added in a later version — services, and a project's `build`
  block when the version that wrote your config shipped without one.
- A `services.json` written before path roots existed keeps working untouched: its absolute
  paths simply contain no tokens. **Convert paths to tokens** opts it in.
