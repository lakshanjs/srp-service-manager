import os
import re
import time
import ctypes
import shutil
import subprocess
import threading
import requests
import urllib3
import config_manager as config

# Cron services poll local *.test URLs over self-signed TLS; silence the warning spam.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CREATE_NO_WINDOW = 0x08000000

# A one-shot task's prompt ("Username: ", "Delete stale content? (yes/no): ") arrives
# without a trailing newline, so a readline() loop would hang with the prompt invisible.
# The task reader emits the buffer early whenever it looks like it ends in a prompt.
PROMPT_TAIL = re.compile(r"(:|\?|>>>|\.\.\.)\s$")


def is_admin():
    """True if this process is running elevated (able to control Windows services)."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def run_elevated_cmd(cmd):
    """Run a shell command elevated via a UAC prompt. Returns True if the launch was
    accepted. We can't capture output from the elevated child, so callers rely on the
    status poller to reflect the result. Runs through cmd.exe /c so chained commands
    (e.g. `net stop X & net start X`) execute in sequence."""
    try:
        SW_HIDE = 0
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f"/c {cmd}", None, SW_HIDE)
        return int(rc) > 32  # >32 == success; <=32 is an SE_ERR_* code (e.g. UAC declined)
    except Exception:
        return False


class ServiceManager:
    def __init__(self):
        data = config.load_config()
        self.paths = data["paths"]      # machine-specific roots, referenced as ${name}
        self.apps = data["apps"]
        self.services = data["services"]
        self.settings = config.load_settings()

        self.app_id_list = [a["id"] for a in self.apps]
        self.app_by_id = {a["id"]: a for a in self.apps}

        self.processes = {}
        self.stop_flags = {}      # cron services: name -> threading.Event
        self.task_procs = {}      # one-shot tasks: name -> Popen (or None once finished)
        self.winservice_status = {}  # name -> state string for "winservice"-type entries
        self.log_tails = {}       # name -> threading.Event that stops its tail thread
        self._start_winservice_poller()  # keep OS-service status dots current in the background

    # ------------------------------------------------------------ app helpers

    def apps_of(self, name):
        """The app ids a service belongs to (more than one == shared between stacks)."""
        return config.app_ids(self.services.get(name, {}), self.app_id_list)

    def is_shared(self, name):
        return len(self.apps_of(name)) > 1

    def primary_app(self, name):
        """The app definition used to expand ${python}/${project_dir} for this service."""
        ids = self.apps_of(name)
        return self.app_by_id.get(ids[0]) if ids else None

    def services_for_app(self, app_id):
        """Every service visible in an app's sidebar, config order preserved. Shared
        services appear in the list of every app they belong to."""
        return [n for n in self.services if app_id in self.apps_of(n)]

    def scope_for(self, name):
        """The tokens a service can use: the machine's path roots, plus its own app's fields.

        App fields come second so a project can shadow a global root with its own value —
        and so ${python}, which expands to something containing ${envs}, has both halves of
        the chain available in one scope."""
        scope = dict(self.paths)
        app = self.primary_app(name)
        if app:
            scope.update({k: v for k, v in app.items() if isinstance(v, str)})
        return scope

    def app_scope(self, app_id):
        """The same, for values that live on an app itself (its build path)."""
        scope = dict(self.paths)
        app = self.app_by_id.get(app_id) or {}
        scope.update({k: v for k, v in app.items() if isinstance(v, str)})
        return scope

    def resolve(self, name, key, default=None):
        """Read a service field with its ${...} tokens expanded."""
        value = self.services.get(name, {}).get(key, default)
        return config.expand_tokens(value, self.scope_for(name))

    def is_task(self, name):
        return self.services.get(name, {}).get("type") == "task"

    def is_winservice(self, name):
        return self.services.get(name, {}).get("type") == "winservice"

    def is_logview(self, name):
        return self.services.get(name, {}).get("type") == "logview"

    def includes_in_start_all(self, name, app_id):
        """`include_in_start_all` is a bool, or a per-app dict for shared services."""
        info = self.services.get(name, {})
        raw = info.get("include_in_start_all")
        if isinstance(raw, dict):
            return bool(raw.get(app_id, False))
        if raw is None:
            # Windows services default to opt-in: they need UAC and are usually already
            # running as boot-time OS services.
            return not self.is_winservice(name)
        return bool(raw)

    def set_include_in_start_all(self, name, app_id, value):
        """Persist the Start All flag, keeping the per-app dict shape for shared services."""
        info = self.services.setdefault(name, {})
        if self.is_shared(name):
            current = info.get("include_in_start_all")
            if not isinstance(current, dict):
                current = {a: bool(current) if current is not None else False
                           for a in self.apps_of(name)}
            current[app_id] = bool(value)
            info["include_in_start_all"] = current
        else:
            info["include_in_start_all"] = bool(value)

    # ------------------------------------------------------- editing services

    def add_service(self, name, info):
        """Register a new service definition. Returns the stored dict."""
        self.services[name] = info
        self._refresh_winservice(name)
        return info

    def update_service(self, old_name, new_name, info):
        """Replace a service definition, optionally under a new name.

        The entry keeps its position in the config: a plain `del` + re-assign would move a
        renamed service to the end of the file, and the sidebar follows config order."""
        if new_name != old_name:
            self.retire_service(old_name)  # its old process/tail/state is keyed by name
        if new_name == old_name:
            self.services[old_name] = info
        else:
            rebuilt = {}
            for key, value in self.services.items():
                if key == old_name:
                    rebuilt[new_name] = info
                else:
                    rebuilt[key] = value
            if old_name not in self.services:  # nothing replaced — append
                rebuilt[new_name] = info
            self.services.clear()
            self.services.update(rebuilt)
        self._refresh_winservice(new_name)
        return info

    def remove_service(self, name):
        """Delete a service definition, stopping anything it still has running."""
        self.retire_service(name)
        self.services.pop(name, None)

    def retire_service(self, name):
        """Stop and forget everything keyed by this service name, leaving the definition.

        Used both when deleting a service and when renaming one, since the process table,
        stop flags, log tail and cached Windows-service state are all keyed by name."""
        if self.is_task(name):
            if self.task_running(name):
                self.stop_task(name, lambda n, m: None)
        elif not self.is_winservice(name) and self.processes.get(name):
            # Deliberately not stopping a Windows service: it's an OS service that was most
            # likely running before this app existed, exactly as on exit.
            self.stop_service(name, lambda n, m: None)
        self.stop_log_tail(name)
        self.processes.pop(name, None)
        self.stop_flags.pop(name, None)
        self.task_procs.pop(name, None)
        self.winservice_status.pop(name, None)

    # ----------------------------------------------------------- path roots

    def set_paths(self, paths):
        """Replace the path roots in place — the UI holds a reference to this dict."""
        self.paths.clear()
        self.paths.update(paths)

    def missing_paths(self):
        """Roots that don't point at a folder that exists, for the settings page to flag."""
        return [name for name, value in self.paths.items()
                if not os.path.isdir(config.expand_tokens(value, self.paths))]

    def tokenize_paths(self):
        """Rewrite absolute paths through the config as ${root} tokens. Returns the count.

        This is what makes an existing, machine-specific config portable: afterwards only
        the roots name a drive. Longest root first, so a value under `${srp_tools}` isn't
        claimed by the shorter `${oncodes}` it happens to sit inside."""
        roots = sorted(((n, v) for n, v in self.paths.items() if v),
                       key=lambda pair: len(pair[1]), reverse=True)

        def swap(value):
            if not isinstance(value, str) or "${" in value:
                return value, 0
            for name, root in roots:
                if value.lower().startswith(root.lower()):
                    return "${%s}%s" % (name, value[len(root):]), 1
            return value, 0

        changed = 0
        for info in self.services.values():
            for key in ("dir", "log_file"):
                if key in info:
                    info[key], hit = swap(info[key])
                    changed += hit
        for app in self.apps:
            for key, value in list(app.items()):
                if key not in config.APP_RESERVED_KEYS:
                    app[key], hit = swap(value)
                    changed += hit
            build = app.get("build")
            if isinstance(build, dict) and "path" in build:
                build["path"], hit = swap(build["path"])
                changed += hit
        return changed

    # ----------------------------------------------------------- editing apps

    def _reindex_apps(self):
        """Rebuild the id lookups after the app list is edited."""
        self.app_id_list = [a["id"] for a in self.apps]
        self.app_by_id = {a["id"]: a for a in self.apps}

    def add_app(self, app):
        self.apps.append(app)
        self._reindex_apps()

    def update_app(self, app_id, app):
        """Replace an app definition in place (its id is not editable, so nothing that
        references it — the services' `app` keys — needs rewriting)."""
        for i, existing in enumerate(self.apps):
            if existing["id"] == app_id:
                self.apps[i] = app
                break
        self._reindex_apps()

    def services_using_app(self, app_id):
        """Services that name this app — what stops an app from being removed outright."""
        return [n for n, info in self.services.items()
                if app_id in (info.get("app") if isinstance(info.get("app"), (list, tuple))
                              else [info.get("app")])]

    def remove_app(self, app_id):
        self.apps[:] = [a for a in self.apps if a["id"] != app_id]
        self._reindex_apps()

    def save(self):
        config.save_config(self.apps, self.services, self.paths)

    def save_settings(self):
        config.save_settings(self.settings)

    def reload_from_disk(self):
        """Re-read both config files, replacing apps/services/settings in place.

        In place, because the UI and the tray hold references to these very objects.
        Anything that disappeared is retired first, while its definition is still around to
        say how it should be stopped."""
        data = config.load_config()

        for name in [n for n in self.services if n not in data["services"]]:
            self.retire_service(name)
        for name in list(self.log_tails):
            self.stop_log_tail(name)  # restarted by the UI against the new definitions

        self.paths.clear()
        self.paths.update(data["paths"])
        self.apps[:] = data["apps"]
        self._reindex_apps()
        self.services.clear()
        self.services.update(data["services"])
        self.settings.clear()
        self.settings.update(config.load_settings())

    # ---------------------------------------------------------------- startup

    def start_service(self, service_name, service_tabs, append_output, save_config):
        service_info = self.services[service_name]

        # Windows services (e.g. WAMP's Apache/MySQL) aren't child processes we spawn;
        # they're controlled through the Service Control Manager, so branch out early.
        if service_info.get("type") == "winservice":
            self._control_winservice(service_name, "start", append_output)
            return

        if service_info.get("type") in ("task", "logview"):
            return  # nothing long-running to start

        if service_name in self.processes and self.processes[service_name]:
            append_output(service_name, "Service is already running.")
            return

        if "command" in service_info:
            tab = service_tabs.get(service_name, {})
            dir_entry = tab.get("dir_entry")
            directory = dir_entry.get() if dir_entry is not None else service_info.get("dir", "")

            # Any service page may expose an editable command line; persist what's shown.
            command_entry = tab.get("command_entry")
            if command_entry is not None:
                typed = command_entry.get().strip()
                if typed:
                    service_info["command"] = typed.split()

            service_info["dir"] = directory
            scope = self.scope_for(service_name)
            threading.Thread(
                target=self.run_service,
                args=(service_name,
                      config.expand_tokens(service_info["command"], scope),
                      config.expand_tokens(directory, scope),
                      append_output),
                daemon=True,
            ).start()

        elif "url" in service_info:
            # Start a cron-like service (polling a URL)
            tab = service_tabs.get(service_name, {})
            url = tab["url_entry"].get()
            try:
                interval = int(tab["interval_entry"].get())
                if interval <= 0:
                    raise ValueError
            except (KeyError, ValueError):
                append_output(service_name, "Invalid interval: enter a positive whole number of seconds.")
                return
            service_info["url"] = url
            service_info["interval"] = interval
            self.stop_flags[service_name] = threading.Event()
            threading.Thread(
                target=self.run_cron_service,
                args=(service_name, url, interval, append_output),
                daemon=True,
            ).start()

        save_config()

    def run_service(self, service_name, command, service_dir, append_output):
        """Starts the service without opening a new command window and captures its output."""
        try:
            # Resolve the executable's full path. On Windows, cwd= sets the child's working
            # directory but does NOT affect where the executable is looked up (that uses PATH
            # plus the *parent* process's directory). So if the program lives in the service
            # directory (e.g. centrifugo.exe, memcached.exe), find it there first; otherwise
            # fall back to PATH (e.g. php, java, ngrok). shutil.which adds .exe automatically.
            resolved = self._resolve_command(command, service_dir)

            # Launch in the service's own directory via cwd= rather than os.chdir(),
            # which changes the working directory for the entire process and races with
            # other services starting concurrently. Empty dir -> inherit current directory.
            process = subprocess.Popen(
                resolved,
                cwd=service_dir or None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
                env=self._child_env(),
                text=True,
                bufsize=1,
            )

            self.processes[service_name] = process
            append_output(service_name, "Service started.")

            def read_output(pipe):
                for line in iter(pipe.readline, ''):
                    if line:
                        append_output(service_name, line.rstrip())
                pipe.close()

            threading.Thread(target=read_output, args=(process.stdout,), daemon=True).start()
            threading.Thread(target=read_output, args=(process.stderr,), daemon=True).start()

        except FileNotFoundError:
            append_output(
                service_name,
                f"Error: '{command[0]}' was not found in '{service_dir or 'PATH'}' or on PATH. "
                f"Check the Directory and Command settings."
            )
        except Exception as e:
            append_output(service_name, f"Error starting service: {e}")

    def _resolve_command(self, command, service_dir):
        """Absolute-path the executable, looking in the service dir before PATH."""
        exe = command[0]
        if os.path.isabs(exe) and os.path.exists(exe):
            return list(command)
        full_path = shutil.which(exe, path=service_dir) if service_dir else None
        if not full_path:
            full_path = shutil.which(exe)  # search PATH
        return [full_path] + list(command[1:]) if full_path else list(command)

    def _child_env(self):
        """Unbuffered child output, so Python-based services and tasks (Django) stream
        their output live instead of arriving in one lump when the pipe closes."""
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def run_cron_service(self, service_name, url, interval, append_output):
        """Starts a cron-like service that polls a URL at regular intervals."""
        append_output(service_name, f"Starting {service_name}...")
        self.processes[service_name] = True
        append_output(service_name, "Service started")

        stop_flag = self.stop_flags[service_name]

        while not stop_flag.is_set():
            try:
                response = requests.get(url, verify=False, timeout=30)
                append_output(service_name, f"Called {url}: {response.status_code}")
                append_output(service_name, f"Response: {response.text}")
            except requests.RequestException as e:
                append_output(service_name, f"Error calling {url}: {e}")

            stop_flag.wait(interval)  # Pause for the interval, but allow for interruption

    # ------------------------------------------------------------ one-shot tasks

    def task_running(self, name):
        proc = self.task_procs.get(name)
        return proc is not None and proc.poll() is None

    def run_task(self, name, append_output, on_done=None):
        """Run a `type: "task"` entry once, streaming its output; call on_done(exit_code).

        Unlike a service, a task is expected to finish. Its stderr is merged into stdout so
        Django's output reads in the order it was produced."""
        if self.task_running(name):
            append_output(name, "Already running.")
            return

        scope = self.scope_for(name)
        command = config.expand_tokens(self.services[name].get("command", []), scope)
        cwd = config.expand_tokens(self.services[name].get("dir", ""), scope)
        interactive = self.services[name].get("interactive", False)

        if not command:
            append_output(name, "No command configured for this task.")
            return
        if cwd and not os.path.isdir(cwd):
            append_output(name, f"Working directory not found: {cwd}")
            if on_done:
                on_done(-1)
            return

        def worker():
            try:
                resolved = self._resolve_command(command, cwd)
                append_output(name, f"$ {' '.join(resolved)}")
                process = subprocess.Popen(
                    resolved,
                    cwd=cwd or None,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                    env=self._child_env(),
                    text=True,
                    bufsize=0 if interactive else 1,
                )
                self.task_procs[name] = process
                self._stream_task_output(process.stdout, lambda line: append_output(name, line))
                code = process.wait()
                append_output(name, f"— finished (exit code {code}) —")
                if on_done:
                    on_done(code)
            except FileNotFoundError:
                append_output(name, f"Error: '{command[0]}' was not found in '{cwd or 'PATH'}' or on PATH.")
                if on_done:
                    on_done(-1)
            except Exception as e:
                append_output(name, f"Error running task: {e}")
                if on_done:
                    on_done(-1)
            finally:
                self.task_procs[name] = None

        threading.Thread(target=worker, daemon=True).start()

    def _stream_task_output(self, pipe, emit):
        """Read a task's output character-by-character so an unterminated prompt still
        reaches the log. A plain readline() loop would block on `Username: ` forever."""
        buf = ""
        while True:
            try:
                ch = pipe.read(1)
            except Exception:
                break
            if not ch:
                break
            if ch == "\n":
                emit(buf.rstrip("\r"))
                buf = ""
            else:
                buf += ch
                if PROMPT_TAIL.search(buf):
                    emit(buf)
                    buf = ""
        if buf.strip():
            emit(buf)
        try:
            pipe.close()
        except Exception:
            pass

    def send_task_input(self, name, text, append_output):
        """Answer a running task's prompt (createsuperuser, makemigrations questions)."""
        proc = self.task_procs.get(name)
        if proc is None or proc.poll() is not None or proc.stdin is None:
            append_output(name, "(no running task to send input to)")
            return
        try:
            proc.stdin.write(text + "\n")
            proc.stdin.flush()
        except Exception as e:
            append_output(name, f"(could not send input: {e})")

    def stop_task(self, name, append_output):
        proc = self.task_procs.get(name)
        if proc is None or proc.poll() is not None:
            append_output(name, "Task is not running.")
            return
        subprocess.call(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        creationflags=CREATE_NO_WINDOW)
        append_output(name, "Task cancelled.")

    # -------------------------------------------------------------- stopping

    def stop_service(self, service_name, append_output, requesting_app=None):
        """Stop a service. For a service shared between apps, `requesting_app` scopes the
        request: it's left running while another app still has its own services up."""
        if self.is_task(service_name) or self.is_logview(service_name):
            return

        if requesting_app and self.is_shared(service_name):
            holder = self._other_app_holding(service_name, requesting_app)
            if holder:
                append_output(service_name,
                              f"Left running — shared with {holder}, which is still active.")
                return False

        if self.is_winservice(service_name):
            self._control_winservice(service_name, "stop", append_output)
            return True

        if service_name in self.processes and self.processes[service_name]:
            append_output(service_name, f"Stopping {service_name}...")
            process = self.processes[service_name]

            if "url" in self.services[service_name]:  # Cron-like services
                if service_name in self.stop_flags:
                    self.stop_flags[service_name].set()
            elif isinstance(process, subprocess.Popen):
                # Kill the whole process tree by PID. "taskkill /IM <image>" would kill
                # every ngrok.exe / java.exe on the machine, not just the one we started.
                subprocess.call(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    creationflags=CREATE_NO_WINDOW
                )

            self.processes[service_name] = None
            append_output(service_name, "Service stopped")
            return True

        append_output(service_name, "Service is not running.")
        return False

    def _other_app_holding(self, service_name, requesting_app):
        """Name of another app that still needs this shared service, or None.

        'Still needs it' means that app has one of its *own* (non-shared) services running.
        Counting shared services here would let two shared services keep each other alive
        forever after both stacks are down."""
        for app_id in self.apps_of(service_name):
            if app_id == requesting_app:
                continue
            for other in self.services_for_app(app_id):
                if other == service_name or self.is_shared(other):
                    continue
                if self.is_running(other):
                    return self.app_by_id.get(app_id, {}).get("name", app_id)
        return None

    def is_running(self, name):
        """Live run state for any service type (dots, bulk actions and refcounting)."""
        if self.is_winservice(name):
            return self.winservice_status.get(name) == "running"
        if self.is_task(name):
            return self.task_running(name)
        if self.is_logview(name):
            return False
        proc = self.processes.get(name)
        if not proc:
            return False
        if hasattr(proc, "poll"):  # subprocess.Popen
            if proc.poll() is None:
                return True
            self.processes[name] = None  # exited on its own; let Start work again
            return False
        return True  # cron sentinel (True)

    def stop_all_services(self):
        """Stop every child process we started (used on exit).

        Windows services are deliberately untouched — they're OS services that were most
        likely running before this app started."""
        for service_name in list(self.services):
            if self.is_winservice(service_name):
                continue
            if self.processes.get(service_name):
                self.stop_service(service_name, lambda name, msg: None)
        for name in list(self.task_procs):
            if self.task_running(name):
                self.stop_task(name, lambda n, m: None)

    def restart_service(self, service_name, service_tabs, append_output, save_config):
        """Restarts the service by stopping it and starting it again."""
        # For a Windows service, do the stop+start in a single elevated command so the
        # user only sees one UAC prompt and `net` waits for the stop before starting.
        if self.is_winservice(service_name):
            self._control_winservice(service_name, "restart", append_output)
            return

        self.stop_service(service_name, append_output)
        self.start_service(service_name, service_tabs, append_output, save_config)

    # ----------------------------------------------------------- app builds

    def run_build(self, app_id, log, on_done=None):
        """Run an app's configured build command (e.g. `npm run build`) in its project dir.

        `log` is a single-argument callback that receives output lines.
        `on_done(success: bool)` is called when the build finishes (optional)."""
        app = self.app_by_id.get(app_id, {})
        cfg = app.get("build") or {}
        path = config.expand_tokens((cfg.get("path") or "").strip(), self.app_scope(app_id))
        command = (cfg.get("command") or "npm run build").strip()

        if not path or not os.path.isdir(path):
            log(f"Build: project directory not found: '{path}'")
            if on_done:
                on_done(False)
            return

        log(f"Building in {path}")
        log(f"$ {command}")

        def worker():
            try:
                # shell=True so multi-step commands and Windows .cmd shims (npm) resolve.
                process = subprocess.Popen(
                    command, cwd=path, shell=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, creationflags=CREATE_NO_WINDOW
                )
                for line in iter(process.stdout.readline, ''):
                    if line:
                        log(line.rstrip())
                process.stdout.close()
                code = process.wait()
                log(f"Build finished (exit code {code}).")
                if on_done:
                    on_done(code == 0)
            except Exception as e:
                log(f"Build error: {e}")
                if on_done:
                    on_done(False)

        threading.Thread(target=worker, daemon=True).start()

    # --------------------------------------------------------- Windows services

    def _sc_query_state(self, service_name):
        """Query a Windows service's state without needing admin. Returns one of
        'running' | 'stopped' | 'pending' | 'not_found' | 'unknown'."""
        try:
            result = subprocess.run(
                ["sc", "query", service_name],
                capture_output=True, text=True, creationflags=CREATE_NO_WINDOW
            )
        except Exception:
            return "unknown"
        if result.returncode == 1060:  # ERROR_SERVICE_DOES_NOT_EXIST
            return "not_found"
        out = (result.stdout or "").upper()
        if "RUNNING" in out:
            return "running"
        if "STOPPED" in out:
            return "stopped"
        if "PENDING" in out:  # START_PENDING / STOP_PENDING — mid-transition
            return "pending"
        return "not_found" if result.returncode != 0 else "unknown"

    def is_winservice_running(self, name):
        return self.winservice_status.get(name) == "running"

    def _start_winservice_poller(self):
        """Poll each winservice entry's state every few seconds on a daemon thread so the
        UI status dots stay current without an expensive `sc query` on the 1s UI tick.

        The list is re-read every pass rather than captured once, so a Windows service added
        or edited while the app is running is picked up without a restart."""
        def poll():
            while True:
                for name, info in list(self.services.items()):
                    if info.get("type") != "winservice":
                        continue
                    svc = (info.get("service_name") or "").strip()
                    self.winservice_status[name] = self._sc_query_state(svc) if svc else "not_found"
                time.sleep(3)

        threading.Thread(target=poll, daemon=True).start()

    def _refresh_winservice(self, name):
        """Query one Windows service's state immediately, off the main thread.

        Without this a newly added entry would sit on a stale/grey dot until the 3s poller
        came round, which reads as 'stopped' rather than 'not looked yet'."""
        if not self.is_winservice(name):
            return

        def work():
            svc = (self.services.get(name, {}).get("service_name") or "").strip()
            self.winservice_status[name] = self._sc_query_state(svc) if svc else "not_found"

        threading.Thread(target=work, daemon=True).start()

    def _control_winservice(self, service_name, action, append_output):
        """Start/stop/restart the Windows service named in this entry's `service_name`.

        Service control requires elevation. If the app is already running as admin we run
        `net` inline and stream its output; otherwise we relaunch just this command through
        a UAC prompt (output can't be captured, so the status poller reflects the result)."""
        svc = (self.services.get(service_name, {}).get("service_name") or "").strip()
        if not svc:
            append_output(service_name, "No Windows service name configured.")
            return

        # `net start`/`net stop` are synchronous (they wait for the service) and return real
        # exit codes, which makes a stop-then-start restart reliable in a single command.
        if action == "start":
            cmd = f'net start "{svc}"'
        elif action == "stop":
            cmd = f'net stop "{svc}"'
        else:  # restart
            cmd = f'net stop "{svc}" & net start "{svc}"'

        verb = {"start": "Starting", "stop": "Stopping", "restart": "Restarting"}[action]
        append_output(service_name, f"{verb} Windows service '{svc}'...")

        if is_admin():
            threading.Thread(
                target=self._run_admin_cmd, args=(service_name, cmd, append_output), daemon=True
            ).start()
        elif run_elevated_cmd(cmd):
            append_output(service_name, "Elevated request sent — accept the UAC prompt. Status will update shortly.")
        else:
            append_output(service_name, "Could not elevate (UAC declined or unavailable).")

    def start_log_tail(self, name, append_output):
        """Begin streaming a service's log file into its output area (best-effort).

        Windows services write to log files rather than a stdout we can capture, so for an
        entry with a `log_file` we tail that file on a daemon thread — seeding with the last
        chunk of the file, then appending new lines as the service writes."""
        # Expanded, not raw: a log_file is as likely to be written as ${wamp}\logs\... as
        # a service's directory is.
        log_file = (self.resolve(name, "log_file", "") or "").strip()
        if not log_file:
            return
        self.stop_log_tail(name)  # never leave two threads tailing into the same widget
        stop = threading.Event()
        self.log_tails[name] = stop
        threading.Thread(
            target=self._tail_file, args=(name, log_file, append_output, stop), daemon=True
        ).start()

    def stop_log_tail(self, name):
        """Signal a service's tail thread to exit — its output widget is about to go away."""
        stop = self.log_tails.pop(name, None)
        if stop is not None:
            stop.set()

    def _tail_file(self, name, log_file, append_output, stop, seed_lines=80, interval=1.0):
        pos = None            # byte offset already emitted; None until seeded
        warned_missing = False
        while not stop.is_set():
            try:
                if not os.path.exists(log_file):
                    if not warned_missing:
                        append_output(name, f"(waiting for log file: {log_file})")
                        warned_missing = True
                    stop.wait(interval)
                    continue
                warned_missing = False
                size = os.path.getsize(log_file)
                if pos is None:
                    # First pass: show the tail of whatever's already in the file.
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    for line in lines[-seed_lines:]:
                        append_output(name, line.rstrip("\n"))
                    pos = size
                elif size < pos:
                    pos = 0  # file was rotated/truncated — start over from the top
                if size > pos:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(pos)
                        new = f.read()
                        pos = f.tell()
                    for line in new.splitlines():
                        append_output(name, line)
            except Exception as e:
                try:
                    append_output(name, f"(log tail error: {e})")
                except Exception:
                    return  # the window is going away — stop rather than report into it
            stop.wait(interval)  # wait(), not sleep(), so a removed service stops tailing at once

    def _run_admin_cmd(self, service_name, cmd, append_output):
        """Run an already-elevated `net` command and stream its output into the log."""
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW
            )
            for line in (result.stdout or "").splitlines() + (result.stderr or "").splitlines():
                if line.strip():
                    append_output(service_name, line.strip())
            append_output(service_name, f"Done (exit code {result.returncode}).")
        except Exception as e:
            append_output(service_name, f"Error controlling service: {e}")

    def clear_log(self, service_name, service_tabs):
        """Clears the output log for the service."""
        output_area = service_tabs[service_name]["output_area"]
        output_area.delete(1.0, 'end')
