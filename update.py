import os
import threading
from tkinter import messagebox

import requests

try:
    # Proper semantic-version comparison (e.g. 1.10 > 1.9).
    from packaging.version import InvalidVersion, parse as parse_version
except ImportError:
    parse_version = None
    InvalidVersion = ValueError

# Bump this with every build that gets deployed, or a newer release can't be recognised
# as newer. It's also what the About card shows.
CURRENT_VERSION = "1.2"

# The repository releases are published to. This fork was copied out of SRP Service
# Manager and publishes alongside it rather than under a repo of its own. Overridable
# per machine from the settings page (appsettings.json -> updates.repo), so a move or a
# rename doesn't need a rebuild.
DEFAULT_REPO = "lakshanjs/srp-service-manager"

API_ROOT = "https://api.github.com"
TIMEOUT = 10

# Env vars checked when the settings page has no token, so a machine that already has the
# GitHub CLI or CI credentials set up doesn't need the token entered a second time.
TOKEN_ENV_VARS = ("ONCODES_UPDATE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


class UpdateCheck:
    """The outcome of one check, so callers decide what to show rather than update.py.

    `status` is one of:
      update        — a newer release exists (`version`, `notes`, `url` are filled in)
      current       — the latest release is this version or older
      no_releases   — the repo is reachable but has published nothing yet
      no_token      — the repo is private (or missing) and no token is configured
      unauthorized  — the token was rejected, expired, or lacks access to this repo
      not_found     — the repo doesn't exist under that name
      rate_limited  — GitHub's hourly cap, which is only 60/hour unauthenticated
      offline       — the request never completed (no internet, DNS, proxy, timeout)
      error         — anything else, with the HTTP status in `detail`
    """

    def __init__(self, status, version="", notes="", url="", detail=""):
        self.status = status
        self.version = version
        self.notes = notes
        self.url = url
        self.detail = detail

    @property
    def has_update(self):
        return self.status == "update"

    @property
    def is_failure(self):
        """True when the check couldn't answer the question, as opposed to answering 'no'."""
        return self.status not in ("update", "current")


# ------------------------------------------------------------------ settings

def update_settings(settings):
    """The `updates` block of appsettings.json, tolerating an older file without one."""
    block = (settings or {}).get("updates") or {}
    return {"repo": (block.get("repo") or DEFAULT_REPO).strip(),
            "token": (block.get("token") or "").strip()}


def resolve_token(settings=None):
    """The token from the settings page, falling back to the environment.

    Optional while the repo is public — it only buys a higher rate limit, and access if the
    repo is ever made private. Never bundled into the .exe either way: a token compiled into
    a binary that gets copied around is a credential you can't revoke selectively."""
    token = update_settings(settings)["token"]
    if token:
        return token
    for name in TOKEN_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _headers(token):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        # GitHub rejects API requests without one.
        "User-Agent": f"OnCodesDevServiceManager/{CURRENT_VERSION}",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ------------------------------------------------------------------ checking

def check(settings=None):
    """Ask GitHub for the latest release and classify the answer. Never raises."""
    config = update_settings(settings)
    repo = config["repo"]
    token = resolve_token(settings)

    if not repo:
        return UpdateCheck("error", detail="No repository configured.")

    try:
        response = requests.get(f"{API_ROOT}/repos/{repo}/releases/latest",
                                headers=_headers(token), timeout=TIMEOUT)
    except requests.RequestException as error:
        return UpdateCheck("offline", detail=str(error))

    if response.status_code == 200:
        return _classify_release(response.json())

    if response.status_code == 401:
        return UpdateCheck("unauthorized",
                           detail="The token was rejected (expired, revoked or mistyped).")

    if response.status_code == 403:
        if response.headers.get("X-RateLimit-Remaining") == "0":
            return UpdateCheck("rate_limited",
                               detail="GitHub's hourly API limit — unauthenticated requests "
                                      "get only 60 an hour.")
        return UpdateCheck("unauthorized",
                           detail="Access forbidden. If the organisation uses SAML SSO, the "
                                  "token has to be authorised for it.")

    # 404 is deliberately ambiguous on GitHub: a private repo you can't see is
    # indistinguishable from one that doesn't exist, and a repo with no releases yet
    # returns it too. One extra request tells the three apart, which is the difference
    # between "add a token" and "publish a release".
    if response.status_code == 404:
        return _diagnose_404(repo, token)

    return UpdateCheck("error", detail=f"GitHub returned HTTP {response.status_code}.")


def _diagnose_404(repo, token):
    try:
        response = requests.get(f"{API_ROOT}/repos/{repo}",
                                headers=_headers(token), timeout=TIMEOUT)
    except requests.RequestException as error:
        return UpdateCheck("offline", detail=str(error))

    if response.status_code == 200:
        return UpdateCheck("no_releases",
                           detail=f"{repo} exists but has no published releases.")
    if response.status_code == 401:
        return UpdateCheck("unauthorized",
                           detail="The token was rejected (expired, revoked or mistyped).")
    if not token:
        return UpdateCheck("no_token",
                           detail=f"{repo} isn't visible without credentials. If it's "
                                  "private, add a token with repo read access.")
    return UpdateCheck("not_found",
                       detail=f"{repo} doesn't exist, or this token has no access to it.")


def _classify_release(release):
    tag = (release.get("tag_name") or "").strip()
    if not tag:
        return UpdateCheck("no_releases", detail="The latest release has no tag.")
    if is_new_version(tag):
        return UpdateCheck("update", version=tag.lstrip("v"),
                           notes=release.get("body") or "",
                           url=release.get("html_url") or "")
    return UpdateCheck("current", version=tag.lstrip("v"))


def is_new_version(latest_version):
    """True when `latest_version` is ahead of CURRENT_VERSION."""
    latest_version = (latest_version or "").strip().lstrip("vV")
    if not latest_version:
        return False

    if parse_version is not None:
        try:
            return parse_version(latest_version) > parse_version(CURRENT_VERSION)
        except InvalidVersion:
            return False

    # Fallback when 'packaging' isn't installed: compare numeric components as a tuple
    # instead of lexicographically (which would wrongly rank "1.10" below "1.9").
    def to_tuple(version):
        return tuple(int(p) for p in version.split('.') if p.isdigit())

    return to_tuple(latest_version) > to_tuple(CURRENT_VERSION)


# ------------------------------------------------------------ launch check

def check_for_updates(root=None, settings=None, log=None):
    """Background launch check. Speaks up only for a real update.

    A failure here is never a dialog: at launch the user asked to start their services, not
    to hear that GitHub is unreachable. It goes to `log` (the dashboard activity log)
    instead, so a misconfigured token is still discoverable rather than silent — which is
    what the old print() to a console the packaged .exe doesn't have amounted to."""
    def worker():
        result = check(settings)

        if result.has_update:
            if root is not None:
                # Tkinter is not thread-safe: show the dialog on the main thread.
                root.after(0, lambda: show_update_alert(result.version, result.notes, result.url))
            else:
                show_update_alert(result.version, result.notes, result.url)
        elif result.is_failure and log is not None:
            log(f"Update check: {describe(result)}")

    threading.Thread(target=worker, daemon=True).start()


def describe(result):
    """One line explaining a result, for the activity log and the settings page."""
    if result.status == "update":
        return f"v{result.version} is available."
    if result.status == "current":
        return f"Up to date (v{CURRENT_VERSION})."
    if result.status == "offline":
        return "couldn't reach GitHub."
    return result.detail or f"failed ({result.status})."


def show_update_alert(latest_version, release_notes, download_url):
    """Displays an update alert to the user."""
    message = (f"A new version {latest_version} is available!\n\n"
               f"Release notes:\n{release_notes}\n\n"
               f"Download the update: {download_url}")
    messagebox.showinfo("Update Available", message)


def show_message(message):
    """Displays a message to the user."""
    messagebox.showinfo("Information", message)
