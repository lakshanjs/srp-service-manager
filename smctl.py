"""smctl — drive a running OnCodes Dev Service Manager from the command line.

    python smctl.py status [--app NEO]
    python smctl.py logs "Neo Django" [--since N] [--tail 200] [--follow]
    python smctl.py restart "Neo Django" [--wait 5]
    python smctl.py start|stop NAME
    python smctl.py run-task migrate [--wait 10]
    python smctl.py input createsuperuser "admin"
    python smctl.py start-all|stop-all|restart-all APP
    python smctl.py activity [--app NEO]
    python smctl.py config NAME
    python smctl.py add NAME '{"app": "neo", "command": ["php", "artisan", "queue:work"]}'
    python smctl.py update NAME '{"autostart": true, "log_file": null}' [--rename NEW]
    python smctl.py remove NAME            (asks in the app's window first)

Service names match case-insensitively, or by a unique part ("centrifugo"). Standard library
only, so any Python on the machine can run it — it doesn't need the app's virtualenv.

Also the client library mcp_server.py is built on."""

import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
from urllib.parse import urlencode


class NotRunning(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(body.get("error", f"HTTP {status}"))
        self.status = status
        self.body = body


def control_file_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "OnCodes", "control.json")


def _control_info():
    try:
        with open(control_file_path(), encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        raise NotRunning("OnCodes Dev Service Manager isn't running (no control.json). "
                         "Ask the user to open it; don't start the servers yourself.")


def call(method, path, params=None, body=None, timeout=90):
    """One request to the app's control API; returns the decoded JSON."""
    info = _control_info()
    url = f"http://127.0.0.1:{info['port']}{path}"
    if params:
        url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except ValueError:
            payload = {"error": f"HTTP {e.code}"}
        raise ApiError(e.code, payload)
    except (urllib.error.URLError, ConnectionError) as e:
        raise NotRunning(f"OnCodes Dev Service Manager isn't answering on port {info['port']} "
                         f"({e}). It's probably closed — ask the user to open it.")


# ----------------------------------------------------------------- API wrappers

def status(app=None):
    return call("GET", "/api/status", {"app": app})


def logs(service, since=0, tail=200):
    return call("GET", "/api/logs", {"service": service, "since": since, "tail": tail})


def activity(app=None, since=0, tail=200):
    return call("GET", "/api/activity", {"app": app, "since": since, "tail": tail})


def service_action(action, service, wait=0):
    return call("POST", f"/api/{action}", body={"service": service, "wait": wait})


def task_input(service, text, wait=0):
    return call("POST", "/api/task_input", body={"service": service, "text": text, "wait": wait})


def app_action(action, app, wait=0):
    return call("POST", f"/api/{action}", body={"app": app, "wait": wait})


def service_config(service):
    return call("GET", "/api/service_config", {"service": service})


def add_service(name, definition):
    return call("POST", "/api/add_service", body={"name": name, "definition": definition})


def update_service(service, changes, rename=None):
    return call("POST", "/api/update_service",
                body={"service": service, "changes": changes, "rename": rename})


def remove_service(service, requester="smctl", timeout=120):
    # Held open until the user answers the confirmation in the window.
    return call("POST", "/api/remove_service", timeout=timeout + 15,
                body={"service": service, "requester": requester, "timeout": timeout})


# ----------------------------------------------------------------------- CLI

def _print_lines(lines):
    for line in lines:
        print(line["text"])


def _print_status(result):
    rows = result["services"]
    width = max((len(r["name"]) for r in rows), default=4)
    for r in rows:
        state = r.get("state") or ("running" if r["running"] else "stopped")
        extra = []
        if r.get("pid"):
            extra.append(f"pid {r['pid']}")
        if "exit_code" in r:
            extra.append(f"exit {r['exit_code']}")
        if r["shared"]:
            extra.append("shared")
        print(f"{r['name']:<{width}}  {state:<9} {r['type']:<10} "
              f"{','.join(r['apps']):<9} {' '.join(extra)}")
    print(f"\ncursor: {result['cursor']}")


def _print_action(result):
    if "status" in result:
        s = result["status"]
        print(f"{result['action']} {s['name']}: {s.get('state') or ('running' if s['running'] else 'stopped')}")
    if result.get("note"):
        print(result["note"])
    _print_lines(result.get("output") or result.get("activity") or [])
    print(f"\ncursor before action: {result['cursor_before']} (logs NAME --since {result['cursor_before']})")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="smctl", description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true", help="print the raw JSON response")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status")
    p.add_argument("--app")

    p = sub.add_parser("logs")
    p.add_argument("service")
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--tail", type=int, default=200)
    p.add_argument("--follow", "-f", action="store_true")

    p = sub.add_parser("activity")
    p.add_argument("--app")
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--tail", type=int, default=200)

    for name in ("start", "stop", "restart", "run-task", "stop-task"):
        p = sub.add_parser(name)
        p.add_argument("service")
        p.add_argument("--wait", type=float, default=0,
                       help="seconds to wait and then show the output it produced")

    p = sub.add_parser("input")
    p.add_argument("service")
    p.add_argument("text")
    p.add_argument("--wait", type=float, default=1)

    for name in ("start-all", "stop-all", "restart-all"):
        p = sub.add_parser(name)
        p.add_argument("app")
        p.add_argument("--wait", type=float, default=0)

    p = sub.add_parser("config")
    p.add_argument("service")
    p = sub.add_parser("add")
    p.add_argument("name")
    p.add_argument("definition", help="JSON object, as in services.json")
    p = sub.add_parser("update")
    p.add_argument("service")
    p.add_argument("changes", nargs="?", default="{}",
                   help="JSON object merged into the definition; null deletes a key")
    p.add_argument("--rename")
    p = sub.add_parser("remove")
    p.add_argument("service")
    p.add_argument("--timeout", type=float, default=120,
                   help="seconds to wait for the answer in the window")

    args = parser.parse_args(argv)
    # Service output is often non-ASCII (box drawing, emoji); don't die on a cp1252 console.
    # Piped (e.g. into Claude Code's shell) the reader expects UTF-8, not the ANSI codepage.
    if hasattr(sys.stdout, "reconfigure"):
        if sys.stdout.isatty():
            sys.stdout.reconfigure(errors="replace")
        else:
            sys.stdout.reconfigure(encoding="utf-8")

    try:
        if args.cmd == "status":
            result = status(args.app)
            show = _print_status
        elif args.cmd in ("logs", "activity"):
            fetch = (lambda since: logs(args.service, since, args.tail)) if args.cmd == "logs" \
                else (lambda since: activity(args.app, since, args.tail))
            result = fetch(args.since)
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return 0
            _print_lines(result["lines"])
            if args.cmd == "logs" and args.follow:
                cursor = result["cursor"]
                while True:
                    time.sleep(1)
                    result = fetch(cursor)
                    _print_lines(result["lines"])
                    sys.stdout.flush()
                    cursor = result["cursor"]
            print(f"\ncursor: {result['cursor']}")
            return 0
        elif args.cmd in ("config", "add", "update", "remove"):
            if args.cmd == "config":
                result = service_config(args.service)
            elif args.cmd == "add":
                result = add_service(args.name, json.loads(args.definition))
            elif args.cmd == "update":
                result = update_service(args.service, json.loads(args.changes), args.rename)
            else:
                result = remove_service(args.service, timeout=args.timeout)
            # These answer with a definition or a verdict; JSON is the readable form.
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result.get("ok", True) else 1
        elif args.cmd == "input":
            result = task_input(args.service, args.text, args.wait)
            show = lambda r: _print_lines(r["output"])
        elif args.cmd.endswith("-all"):
            result = app_action(args.cmd.replace("-", "_"), args.app, args.wait)
            show = _print_action
        else:
            result = service_action(args.cmd.replace("-", "_"), args.service, args.wait)
            show = _print_action
    except NotRunning as e:
        print(e, file=sys.stderr)
        return 3
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        extra = {k: v for k, v in e.body.items() if k != "error"}
        if extra:
            print(json.dumps(extra, indent=2, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        show(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
