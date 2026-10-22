"""MCP server that gives Claude Code the Service Manager's controls.

Register once, for every project, from Settings -> Claude Code in the app, or by hand:

    claude mcp add --scope user oncodes-service-manager -- python D:\\path\\to\\mcp_server.py

The packaged .exe serves the same thing with `--mcp` (see main.py), so it needs no Python.

It speaks MCP over stdio (newline-delimited JSON-RPC) and forwards each tool call to the
running app's control API through smctl. Standard library only, so it runs on any Python
without installing the `mcp` package."""

import sys
import json

import smctl

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "oncodes-service-manager", "version": "1.0.0"}

INSTRUCTIONS = (
    "The user's dev servers (Django runserver, Centrifugo, Celery, PHP, Redis/Memurai, MySQL, "
    "Apache, ...) for SRP and NEO are run by OnCodes Dev Service Manager, which the user "
    "watches while you work. Use these tools to check, start, stop or restart them and to "
    "read their logs. Never start those servers yourself in a shell: that creates a second "
    "copy that fights the managed one for its port. If the app isn't running, tell the user "
    "and ask them to open it. To see what an action caused, pass `wait` (seconds), or note "
    "`cursor_before` and read the service's logs with `since` set to it. You can also add, "
    "edit and remove service definitions: read one with get_service_config first. "
    "Removing asks the user to confirm in the app's window and waits for the answer.")

_SERVICE = {"type": "string",
            "description": "Service name; case-insensitive, or any unique part of it."}
_WAIT = {"type": "number", "minimum": 0, "maximum": 60,
         "description": "Seconds to wait before replying, so the reply carries the output "
                        "the action produced (e.g. 5 for a Django restart). Default 0."}
_APP = {"type": "string", "description": "Project id or name, e.g. SRP or NEO."}
_SINCE = {"type": "integer", "minimum": 0,
          "description": "Only lines after this cursor (from an earlier reply). Default 0."}
_TAIL = {"type": "integer", "minimum": 0,
         "description": "At most this many of the newest lines (0 = all kept). Default 200."}


_DEFINITION_HELP = (
    "A service definition, as in services.json. `app`: project id or list of ids (more than "
    "one makes it shared). `type`: process (default), task, cron, winservice or logview. "
    "process/task: `command` (list of arguments, or one line), `dir`. task: `section`, "
    "`interactive`. cron: `url`, `interval` (seconds). winservice: `service_name`. "
    "logview: `log_file`. process/winservice: optional `log_file`, `redis` {host, port}. "
    "process/cron/winservice: `include_in_start_all` (bool, or per project), `autostart`. "
    "${tokens} like ${python}, ${project_dir} or a path root are expanded at run time.")


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}


TOOLS = [
    {"name": "service_status",
     "description": "List every project and service with its type, owning projects and "
                    "whether it's running (pid, Windows-service state, task exit code). "
                    "Also returns the current log cursor.",
     "inputSchema": _schema({"app": _APP}),
     "annotations": {"readOnlyHint": True}},
    {"name": "service_logs",
     "description": "Recent output of one service or task — the same lines the user sees "
                    "on its page (the app keeps the last 2000). Use `since` to get only "
                    "new lines.",
     "inputSchema": _schema({"service": _SERVICE, "since": _SINCE, "tail": _TAIL}, ["service"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "app_activity",
     "description": "A project's dashboard activity log (Start All / Stop All progress, "
                    "notices). Defaults to the project shown in the window.",
     "inputSchema": _schema({"app": _APP, "since": _SINCE, "tail": _TAIL}),
     "annotations": {"readOnlyHint": True}},
    {"name": "start_service",
     "description": "Start a service, exactly like its Start button. Windows services "
                    "(Memurai, MySQL, Apache) show the user a UAC prompt.",
     "inputSchema": _schema({"service": _SERVICE, "wait": _WAIT}, ["service"])},
    {"name": "stop_service",
     "description": "Stop a service, exactly like its Stop button (unconditional, even if "
                    "shared with another project).",
     "inputSchema": _schema({"service": _SERVICE, "wait": _WAIT}, ["service"]),
     "annotations": {"destructiveHint": True}},
    {"name": "restart_service",
     "description": "Restart a service, e.g. after changing settings or code the server "
                    "doesn't auto-reload. Pass `wait` to get its startup output back.",
     "inputSchema": _schema({"service": _SERVICE, "wait": _WAIT}, ["service"])},
    {"name": "run_task",
     "description": "Run a one-shot task (migrate, makemigrations, collectstatic, ...) as "
                    "configured in the app. Its output streams into its log; pass `wait` "
                    "or poll service_logs and service_status for the exit code.",
     "inputSchema": _schema({"service": _SERVICE, "wait": _WAIT}, ["service"])},
    {"name": "task_input",
     "description": "Send a line of input to a running task that is waiting for it "
                    "(e.g. a createsuperuser or makemigrations prompt).",
     "inputSchema": _schema({"service": _SERVICE, "text": {"type": "string"}, "wait": _WAIT},
                            ["service", "text"])},
    {"name": "stop_task",
     "description": "Kill a running one-shot task.",
     "inputSchema": _schema({"service": _SERVICE}, ["service"]),
     "annotations": {"destructiveHint": True}},
    {"name": "start_all",
     "description": "A project's Start All: starts the services ticked for it.",
     "inputSchema": _schema({"app": _APP, "wait": _WAIT}, ["app"])},
    {"name": "stop_all",
     "description": "A project's Stop All. Shared services another project still uses are "
                    "left running.",
     "inputSchema": _schema({"app": _APP, "wait": _WAIT}, ["app"]),
     "annotations": {"destructiveHint": True}},
    {"name": "get_service_config",
     "description": "A service's definition as stored (with ${tokens}), the same values "
                    "expanded, the project ids, and which fields each type uses. Read this "
                    "before update_service.",
     "inputSchema": _schema({"service": _SERVICE}, ["service"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "add_service",
     "description": "Add a service. It appears in the sidebar and dashboard right away; it "
                    "isn't started. " + _DEFINITION_HELP,
     "inputSchema": _schema({"name": {"type": "string", "description": "The new service's name."},
                             "definition": {"type": "object", "description": _DEFINITION_HELP}},
                            ["name", "definition"])},
    {"name": "update_service",
     "description": "Change a service: `changes` is merged into its definition (a null "
                    "value deletes that key), `rename` renames it. A running service keeps "
                    "running the old definition until restarted; renaming stops it.",
     "inputSchema": _schema({"service": _SERVICE,
                             "changes": {"type": "object", "description": _DEFINITION_HELP},
                             "rename": {"type": "string"}}, ["service"])},
    {"name": "remove_service",
     "description": "Remove a service from the configuration. The user is asked to confirm "
                    "in the app's window and this waits for their answer (up to `timeout` "
                    "seconds, default 120); nothing is removed without a yes. A running "
                    "service is stopped first; a Windows service itself is left installed.",
     "inputSchema": _schema({"service": _SERVICE,
                             "timeout": {"type": "number", "minimum": 10, "maximum": 300}},
                            ["service"]),
     "annotations": {"destructiveHint": True}},
    {"name": "restart_all",
     "description": "A project's Restart All (the services ticked for Start All).",
     "inputSchema": _schema({"app": _APP, "wait": _WAIT}, ["app"])},
]


def _lines(lines):
    return "\n".join(line["text"] for line in lines) or "(no output)"


def _state(s):
    return s.get("state") or ("running" if s["running"] else "stopped")


def _format_status(r):
    out = []
    for s in r["services"]:
        bits = [_state(s), s["type"], "/".join(s["apps"])]
        if s.get("pid"):
            bits.append(f"pid {s['pid']}")
        if "exit_code" in s:
            bits.append(f"exit code {s['exit_code']}")
        if s["shared"]:
            bits.append("shared")
        out.append(f"- {s['name']}: {', '.join(bits)}")
    return "\n".join(out) + f"\n\ncursor: {r['cursor']}"


def _format_action(r):
    head = (f"{r['action']} '{r['status']['name']}' → now {_state(r['status'])}"
            if "status" in r else f"{r['action']} {r['app']} done")
    parts = [head]
    if r.get("note"):
        parts.append(r["note"])
    if "services" in r:
        parts.append("\n".join(f"- {s['name']}: {_state(s)}" for s in r["services"]))
    lines = r.get("output") if "output" in r else r.get("activity")
    parts.append("Output since the action:\n" + _lines(lines or []))
    parts.append(f"cursor_before: {r['cursor_before']}")
    return "\n\n".join(parts)


def _format_log(r, label):
    return f"[{label}]\n{_lines(r['lines'])}\n\ncursor: {r['cursor']}"


def call_tool(name, args):
    wait = args.get("wait", 0)
    if name == "service_status":
        return _format_status(smctl.status(args.get("app")))
    if name == "service_logs":
        r = smctl.logs(args["service"], args.get("since", 0), args.get("tail", 200))
        return _format_log(r, r["service"])
    if name == "app_activity":
        r = smctl.activity(args.get("app"), args.get("since", 0), args.get("tail", 200))
        return _format_log(r, f"activity: {r['app']}")
    if name in ("start_service", "stop_service", "restart_service"):
        return _format_action(smctl.service_action(name.split("_")[0], args["service"], wait))
    if name in ("run_task", "stop_task"):
        return _format_action(smctl.service_action(name, args["service"], wait))
    if name == "task_input":
        r = smctl.task_input(args["service"], args["text"], wait)
        return "Sent.\n\n" + _lines(r["output"]) + f"\n\ncursor_before: {r['cursor_before']}"
    if name in ("start_all", "stop_all", "restart_all"):
        return _format_action(smctl.app_action(name, args["app"], wait))
    if name == "get_service_config":
        return json.dumps(smctl.service_config(args["service"]), indent=2, ensure_ascii=False)
    if name in ("add_service", "update_service"):
        r = (smctl.add_service(args["name"], args["definition"]) if name == "add_service"
             else smctl.update_service(args["service"], args.get("changes") or {},
                                       args.get("rename")))
        verb = "Added" if name == "add_service" else "Updated"
        head = f"{verb} '{r['service']}' (now {_state(r['status'])})."
        if r.get("note"):
            head += " " + r["note"]
        return head + "\n\n" + json.dumps(r["definition"], indent=2, ensure_ascii=False)
    if name == "remove_service":
        r = smctl.remove_service(args["service"], requester="Claude Code",
                                 timeout=args.get("timeout", 120))
        return (f"Removed '{r['service']}'." if r.get("removed")
                else f"Not removed: {r.get('reason')}")
    raise KeyError(name)


# --------------------------------------------------------------- JSON-RPC

def _send(message):
    sys.stdout.buffer.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _handle(message):
    method, mid = message.get("method"), message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        try:
            text, error = call_tool(name, params.get("arguments") or {}), False
        except smctl.NotRunning as e:
            text, error = str(e), True
        except smctl.ApiError as e:
            extra = {k: v for k, v in e.body.items() if k != "error"}
            text = str(e) + (f"\n{json.dumps(extra, ensure_ascii=False)}" if extra else "")
            error = True
        except KeyError as e:
            text, error = f"Unknown tool or missing argument: {e}", True
        return {"content": [{"type": "text", "text": text}], "isError": error}
    if mid is None:
        return None  # a notification (e.g. notifications/initialized) — no reply
    raise LookupError(method)


def main():
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        try:
            message = json.loads(raw.decode("utf-8"))
        except ValueError:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "Parse error"}})
            continue
        mid = message.get("id")
        try:
            result = _handle(message)
        except LookupError:
            if mid is not None:
                _send({"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32601, "message": f"Unknown method {message.get('method')}"}})
            continue
        except Exception as e:
            if mid is not None:
                _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})
            continue
        if mid is not None and result is not None:
            _send({"jsonrpc": "2.0", "id": mid, "result": result})


if __name__ == "__main__":
    main()
