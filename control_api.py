"""Local control API: lets other tools (Claude Code, smctl, scripts) drive the running app.

A small HTTP server on 127.0.0.1 that does exactly what the UI's buttons do — start, stop,
restart, Start All, run a task, read logs — so an assistant works against the same processes
and the same log lines the user is watching, instead of spawning its own copies.

Every request needs the bearer token written to `%APPDATA%\\OnCodes\\control.json` along
with the port. Only processes running as this user can read that file, which is what keeps
other local software (and any web page doing a localhost fetch) out.

Actions are run on the Tk main thread via root.after(): Tkinter isn't thread-safe, and going
through the UI's own handlers means the window reflects what was done, as if clicked."""

import os
import json
import time
import queue
import socket
import secrets
import threading
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import config_manager as config

CONTROL_FILE_NAME = "control.json"
DEFAULT_PORT = 47615
UI_CALL_TIMEOUT = 15   # seconds to wait for the main thread to run an action
MAX_WAIT = 60          # cap on the `wait` a caller can ask for after an action
CONFIRM_TIMEOUT = 120  # how long a removal waits for the user to answer in the window
MAX_CONFIRM_TIMEOUT = 300


def control_file_path():
    return os.path.join(config.config_dir(), CONTROL_FILE_NAME)


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """An HTTP server that owns its port outright.

    http.server sets SO_REUSEADDR, which on Windows lets a *second* process bind a port
    that is already in use. Then a second instance of the app shares the port with the
    first and requests land on either one, and any local program could bind it the same
    way and read the bearer token off incoming requests. SO_EXCLUSIVEADDRUSE refuses both:
    the bind fails, and start() moves on to a free port."""
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class LogBuffer:
    """The last N lines of every log the UI shows, numbered so a reader can ask for only what
    arrived after a point (e.g. "everything since I hit restart").

    Sequence numbers are global across all logs, so one cursor taken before an action
    covers whichever services it touches. Written from output threads; guarded by a lock."""

    def __init__(self, max_lines=2000):
        self.max_lines = max_lines
        self.lock = threading.Lock()
        self.seq = 0
        self.logs = {}   # log key -> deque of (seq, timestamp, text)

    def append(self, key, text):
        with self.lock:
            log = self.logs.get(key)
            if log is None:
                log = self.logs[key] = deque(maxlen=self.max_lines)
            stamp = time.time()
            # One entry per line, so `tail` counts lines the way a reader expects.
            for line in str(text).splitlines() or [""]:
                self.seq += 1
                log.append((self.seq, stamp, line))

    def cursor(self):
        with self.lock:
            return self.seq

    def read(self, key, since=0, tail=200):
        with self.lock:
            lines = [entry for entry in self.logs.get(key, ()) if entry[0] > since]
            seq = self.seq
        if tail:
            lines = lines[-tail:]
        return {"lines": [{"seq": s, "time": round(t, 3), "text": x} for s, t, x in lines],
                "cursor": seq}

    def clear(self, key):
        with self.lock:
            self.logs.pop(key, None)


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.body = {"error": message, **extra}


class ControlServer:
    def __init__(self, root, service_manager, ui):
        self.root = root
        self.sm = service_manager
        self.ui = ui
        self.token = secrets.token_urlsafe(32)
        self.httpd = None

    # --------------------------------------------------------------- lifecycle

    def start(self, port=DEFAULT_PORT):
        """Bind and serve in the background. Falls back to any free port if the configured
        one is taken (e.g. a second instance); clients read the real one from control.json."""
        handler = self._make_handler()
        for candidate in (port, 0):
            try:
                self.httpd = ExclusiveHTTPServer(("127.0.0.1", candidate), handler)
                break
            except OSError:
                continue
        if self.httpd is None:
            return None
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self._write_control_file()
        return self.httpd.server_address[1]

    def _write_control_file(self):
        info = {"port": self.httpd.server_address[1], "token": self.token, "pid": os.getpid()}
        with open(control_file_path(), "w", encoding="utf-8") as file:
            json.dump(info, file, indent=2)

    def stop(self):
        if self.httpd is None:
            return
        self.httpd.shutdown()
        # Only remove the file if it's still ours; a newer instance may have replaced it.
        try:
            with open(control_file_path(), encoding="utf-8") as file:
                if json.load(file).get("pid") == os.getpid():
                    os.remove(control_file_path())
        except (OSError, ValueError):
            pass

    # ----------------------------------------------------------------- helpers

    def on_ui(self, fn):
        """Run fn on the Tk main thread and return its result (or raise its exception)."""
        result = queue.Queue(maxsize=1)

        def run():
            try:
                result.put((True, fn()))
            except Exception as e:  # handed back to the request thread
                result.put((False, e))

        self.root.after(0, run)
        try:
            ok, value = result.get(timeout=UI_CALL_TIMEOUT)
        except queue.Empty:
            raise ApiError(504, "The app's main thread didn't respond (is a dialog open?).")
        if not ok:
            raise value
        return value

    def resolve_service(self, name):
        """Exact name, else case-insensitive, else a unique substring — so "neo django" or
        "centrifugo" work without the exact casing, but an ambiguous guess is refused."""
        if not name:
            raise ApiError(400, "Missing 'service'.")
        names = list(self.sm.services)
        if name in names:
            return name
        low = name.lower()
        exact = [n for n in names if n.lower() == low]
        if len(exact) == 1:
            return exact[0]
        partial = [n for n in names if low in n.lower()]
        if len(partial) == 1:
            return partial[0]
        if partial:
            raise ApiError(409, f"'{name}' matches several services.", candidates=partial)
        raise ApiError(404, f"No service named '{name}'.", services=names)

    def resolve_app(self, app_id):
        if not app_id:
            raise ApiError(400, "Missing 'app'.")
        for app in self.sm.apps:
            if app_id.lower() in (app["id"].lower(), str(app.get("name", "")).lower()):
                return app["id"]
        raise ApiError(404, f"No app '{app_id}'.", apps=[a["id"] for a in self.sm.apps])

    def service_kind(self, name):
        info = self.sm.services[name]
        if info.get("type"):
            return info["type"]
        return "cron" if "url" in info else "process"

    def service_status(self, name):
        sm = self.sm
        kind = self.service_kind(name)
        status = {"name": name, "type": kind, "apps": sm.apps_of(name),
                  "shared": sm.is_shared(name), "running": sm.is_running(name)}
        if name in sm.starting:
            status["state"] = "starting"
        if kind == "winservice":
            status["state"] = sm.winservice_status.get(name, "unknown")
            status["service_name"] = sm.services[name].get("service_name")
        proc = sm.processes.get(name) if kind == "process" else None
        if hasattr(proc, "pid") and status["running"]:
            status["pid"] = proc.pid
        if kind == "process" and not status["running"] and sm.exit_codes.get(name) is not None:
            status["exit_code"] = sm.exit_codes[name]  # exited on its own, i.e. crashed
            status["state"] = "crashed" if sm.exit_codes[name] else "exited"
        if hasattr(proc, "pid") and status["running"] and sm.started_at.get(name):
            status["uptime_s"] = int(time.time() - sm.started_at[name])
        if kind == "task" and not status["running"] and name in sm.task_exit_codes:
            status["exit_code"] = sm.task_exit_codes[name]
        if kind == "task":
            status["interactive"] = bool(sm.services[name].get("interactive", False))
        return status

    # ----------------------------------------------------------------- routes

    def handle(self, method, path, query, body):
        route = (method, path.rstrip("/") or "/")
        routes = {
            ("GET", "/api/health"): self.get_health,
            ("GET", "/api/status"): self.get_status,
            ("GET", "/api/logs"): self.get_logs,
            ("GET", "/api/activity"): self.get_activity,
            ("POST", "/api/start"): lambda q, b: self.service_action("start", b),
            ("POST", "/api/stop"): lambda q, b: self.service_action("stop", b),
            ("POST", "/api/restart"): lambda q, b: self.service_action("restart", b),
            ("POST", "/api/run_task"): lambda q, b: self.service_action("run_task", b),
            ("POST", "/api/stop_task"): lambda q, b: self.service_action("stop_task", b),
            ("POST", "/api/task_input"): self.task_input,
            ("POST", "/api/start_all"): lambda q, b: self.app_action("start_all", b),
            ("POST", "/api/stop_all"): lambda q, b: self.app_action("stop_all", b),
            ("POST", "/api/restart_all"): lambda q, b: self.app_action("restart_all", b),
            ("GET", "/api/service_config"): self.get_service_config,
            ("POST", "/api/add_service"): self.add_service,
            ("POST", "/api/update_service"): self.update_service,
            ("POST", "/api/remove_service"): self.remove_service,
        }
        fn = routes.get(route)
        if fn is None:
            raise ApiError(404, f"No route {method} {path}.")
        return fn(query, body)

    def get_health(self, query, body):
        return {"ok": True, "pid": os.getpid(), "cursor": self.ui.log_buffer.cursor()}

    def get_status(self, query, body):
        def collect():
            app = query.get("app")
            names = self.sm.services_for_app(self.resolve_app(app)) if app else list(self.sm.services)
            return {"apps": [{"id": a["id"], "name": a.get("name", a["id"]),
                              "services": self.sm.services_for_app(a["id"])}
                             for a in self.sm.apps],
                    "services": [self.service_status(n) for n in names],
                    "cursor": self.ui.log_buffer.cursor()}
        return self.on_ui(collect)

    def get_logs(self, query, body):
        name = self.resolve_service(query.get("service"))
        since, tail = int(query.get("since", 0)), int(query.get("tail", 200))
        return {"service": name, **self.ui.log_buffer.read(name, since, tail)}

    def get_activity(self, query, body):
        app_id = self.resolve_app(query.get("app") or self.ui.current_app)
        since, tail = int(query.get("since", 0)), int(query.get("tail", 200))
        return {"app": app_id, **self.ui.log_buffer.read(f"activity:{app_id}", since, tail)}

    def _wait_then_report(self, body, cursor, report):
        """Optionally hold the response for up to `wait` seconds so the caller gets the
        first output an action produced (e.g. a traceback, or "Listening on :8000")."""
        wait = min(float(body.get("wait") or 0), MAX_WAIT)
        if wait > 0:
            time.sleep(wait)
        return report()

    def service_action(self, action, body):
        name = self.resolve_service(body.get("service"))
        kind = self.service_kind(name)
        if action in ("run_task", "stop_task") and kind != "task":
            raise ApiError(400, f"'{name}' is a {kind}, not a task; use start/stop/restart.")
        if action in ("start", "stop", "restart") and kind in ("task", "logview"):
            hint = "use run_task" if kind == "task" else "a log view has nothing to run"
            raise ApiError(400, f"'{name}' is a {kind} ({hint}).")

        # A child process launches on a background thread after a stray-process scan; a
        # stop arriving in that gap would find nothing to stop and the start would win.
        if action in ("stop", "restart"):
            self._await_start(name)
        # The UI disables Restart on a stopped service; asked to restart one, just start it.
        if action == "restart" and not self.on_ui(lambda: self.sm.is_running(name)):
            action = "start"

        cursor = self.ui.log_buffer.cursor()
        handlers = {
            "start": self.ui._do_start,
            "stop": self.ui._do_stop,
            "restart": self.ui._do_restart,
            "run_task": self.ui._do_run_task,
            "stop_task": lambda n: self.sm.stop_task(n, self.ui.append_output),
        }
        self.on_ui(lambda: handlers[action](name))

        def report():
            if body.get("wait"):
                self._await_start(name)  # a caller who waited wants the launch, not "starting"
            status = self.on_ui(lambda: self.service_status(name))
            out = {"ok": True, "action": action, "status": status, "cursor_before": cursor}
            if kind == "winservice" and action != "stop":
                out["note"] = "Windows services need the user to accept a UAC prompt."
            out["output"] = self.ui.log_buffer.read(name, since=cursor, tail=200)["lines"]
            return out
        return self._wait_then_report(body, cursor, report)

    def _await_start(self, name, timeout=30):
        deadline = time.time() + timeout
        while name in self.sm.starting and time.time() < deadline:
            time.sleep(0.1)

    # ------------------------------------------------------- editing services

    def get_service_config(self, query, body):
        name = self.resolve_service(query.get("service"))

        def collect():
            sm = self.sm
            info = sm.services[name]
            resolved = {key: sm.resolve(name, key) for key in ("command", "dir", "log_file")
                        if key in info}
            return {"service": name, "type": config.service_kind(info),
                    "definition": json.loads(json.dumps(info)), "resolved": resolved,
                    "apps": [a["id"] for a in sm.apps],
                    "fields": {k: sorted(v) for k, v in config.KIND_FIELDS.items()}}
        return self.on_ui(collect)

    def _commit(self, old_name, new_name, info):
        """Validate, then save through the editor's own path (rebuilds the page and nav)."""
        def run():
            sm = self.sm
            try:
                name, clean = config.validate_service(new_name, info, sm.services,
                                                      sm.app_id_list, old_name)
            except ValueError as e:
                raise ApiError(400, str(e))
            was_running = old_name is not None and sm.is_running(old_name)
            self.ui.commit_service(old_name, name, clean)
            out = {"ok": True, "service": name, "definition": clean,
                   "status": self.service_status(name)}
            if was_running and old_name != name and not sm.is_winservice(name):
                out["note"] = "Renaming stopped it; start it again under the new name."
            elif was_running and not sm.is_winservice(name):
                out["note"] = "It's still running the old definition; restart it to apply."
            return out
        return self.on_ui(run)

    def add_service(self, query, body):
        definition = body.get("definition")
        if not isinstance(definition, dict):
            raise ApiError(400, "Missing 'definition' (an object, as in services.json).")
        return self._commit(None, str(body.get("name") or ""), definition)

    def update_service(self, query, body):
        """Merge `changes` into the current definition; a null value deletes that key.
        `rename` gives the service a new name."""
        name = self.resolve_service(body.get("service"))
        changes = body.get("changes") or {}
        if not isinstance(changes, dict):
            raise ApiError(400, "'changes' must be an object.")
        if not changes and not body.get("rename"):
            raise ApiError(400, "Nothing to change: pass 'changes' and/or 'rename'.")
        info = json.loads(json.dumps(self.sm.services[name]))
        for key, value in changes.items():
            if value is None:
                info.pop(key, None)
            else:
                info[key] = value
        return self._commit(name, str(body.get("rename") or name), info)

    def remove_service(self, query, body):
        """Ask the user in the window, and only remove on a yes. Holds the request open
        until they answer, or `timeout` seconds pass (which withdraws the question)."""
        name = self.resolve_service(body.get("service"))
        requester = str(body.get("requester") or "A script using the control API")
        timeout = min(float(body.get("timeout") or CONFIRM_TIMEOUT), MAX_CONFIRM_TIMEOUT)
        answered = threading.Event()
        result = {}

        def on_answer(yes):  # runs on the Tk thread, from the dialog
            if yes and name in self.sm.services:
                self.ui.remove_service_now(name)
            result["removed"] = bool(yes)
            answered.set()

        dialog = self.on_ui(lambda: self.ui.ask_to_remove(name, requester, on_answer))
        if not answered.wait(timeout):
            self.on_ui(dialog.close)
            answered.wait(5)
            return {"ok": False, "removed": False, "service": name,
                    "reason": f"No answer in the window within {int(timeout)}s; nothing removed."}
        if not result.get("removed"):
            return {"ok": False, "removed": False, "service": name,
                    "reason": "The user chose to keep it."}
        return {"ok": True, "removed": True, "service": name}

    def task_input(self, query, body):
        name = self.resolve_service(body.get("service"))
        if "text" not in body:
            raise ApiError(400, "Missing 'text'.")
        cursor = self.ui.log_buffer.cursor()
        self.on_ui(lambda: self.sm.send_task_input(name, str(body["text"]), self.ui.append_output))
        return self._wait_then_report(body, cursor, lambda: {
            "ok": True, "cursor_before": cursor,
            "output": self.ui.log_buffer.read(name, since=cursor)["lines"]})

    def app_action(self, action, body):
        app_id = self.resolve_app(body.get("app"))
        cursor = self.ui.log_buffer.cursor()
        self.on_ui(lambda: getattr(self.ui, action)(app_id))

        def report():
            return {"ok": True, "action": action, "app": app_id, "cursor_before": cursor,
                    "activity": self.ui.log_buffer.read(f"activity:{app_id}", since=cursor)["lines"],
                    "services": self.on_ui(lambda: [self.service_status(n)
                                                    for n in self.sm.services_for_app(app_id)])}
        return self._wait_then_report(body, cursor, report)

    # ------------------------------------------------------------- HTTP glue

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # no console in the frozen app; stderr writes would fail

            def _reply(self, status, payload):
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _dispatch(self, method):
                try:
                    if not secrets.compare_digest(self.headers.get("Authorization", ""),
                                                  f"Bearer {server.token}"):
                        raise ApiError(401, "Bad or missing token (see control.json).")
                    url = urlparse(self.path)
                    query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                    body = {}
                    if method == "POST":
                        length = int(self.headers.get("Content-Length") or 0)
                        raw = self.rfile.read(length) if length else b""
                        body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
                        if not isinstance(body, dict):
                            raise ApiError(400, "Body must be a JSON object.")
                    self._reply(200, server.handle(method, url.path, query, body))
                except ApiError as e:
                    self._reply(e.status, e.body)
                except (ValueError, KeyError) as e:
                    self._reply(400, {"error": f"Bad request: {e}"})
                except Exception as e:
                    self._reply(500, {"error": f"{type(e).__name__}: {e}"})

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

        return Handler
