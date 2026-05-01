#!/usr/bin/env python3
"""TRMM-Gitea Sync

Synchronises scripts stored in a Gitea repository with the Tactical RMM
script library.

Rules
-----
* The top-level folder a script lives in becomes its TRMM *category*.
* The filename (without extension) becomes the TRMM *script name*.
* The file extension determines the TRMM *shell* type.
* Gitea content always wins – the script body is overwritten on every run.
* TRMM-managed settings (args, supported_platforms, run_as_user, env_vars,
  default_timeout, favorite, hidden) are **never** overwritten for scripts
  that already exist in TRMM.
* TRMM scripts that have no counterpart in Gitea are left untouched.

Configuration (environment variables)
--------------------------------------
TRMM_API_URL   Base URL of the Tactical RMM instance, e.g. https://rmm.example.com
TRMM_API_KEY   Tactical RMM API key
GITEA_URL      Base URL of the Gitea instance, e.g. https://gitea.example.com
GITEA_TOKEN    Gitea access token (required for private repos)
GITEA_OWNER    Gitea repository owner (user or org)
GITEA_REPO     Gitea repository name
GITEA_BRANCH   Branch to read from (default: main)
IGNORE_SSL     Set to "true", "1", or "yes" to disable SSL certificate
               verification for all API calls.  Useful when the script runs
               on the TRMM server itself where the API hostname resolves to
               127.0.0.1 and the certificate CN does not match (default: false)
"""

import base64
import logging
import os
import sys
from typing import Dict, List, Optional, Tuple

import requests
import urllib3

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRMM_API_URL: str = os.environ.get("TRMM_API_URL", "").rstrip("/")
TRMM_API_KEY: str = os.environ.get("TRMM_API_KEY", "")

GITEA_URL: str = os.environ.get("GITEA_URL", "").rstrip("/")
GITEA_TOKEN: str = os.environ.get("GITEA_TOKEN", "")
GITEA_OWNER: str = os.environ.get("GITEA_OWNER", "")
GITEA_REPO: str = os.environ.get("GITEA_REPO", "")
GITEA_BRANCH: str = os.environ.get("GITEA_BRANCH", "main")

IGNORE_SSL: bool = os.environ.get("IGNORE_SSL", "").lower() in ("1", "true", "yes")
SSL_VERIFY: bool = not IGNORE_SSL

if IGNORE_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# File extension → TRMM shell type
EXTENSION_TO_SHELL: Dict[str, str] = {
    ".ps1": "powershell",
    ".py": "python",
    ".sh": "shell",
    ".bat": "batch",
    ".cmd": "batch",
}

# Default supported_platforms for newly created scripts, keyed by shell type
DEFAULT_PLATFORMS: Dict[str, List[str]] = {
    "powershell": ["windows"],
    "python": ["windows", "linux", "darwin"],
    "shell": ["linux", "darwin"],
    "batch": ["windows"],
}

DEFAULT_SCRIPT_TYPE: str = "userdefined"
DEFAULT_TIMEOUT: int = 90

# ---------------------------------------------------------------------------
# Gitea API helpers
# ---------------------------------------------------------------------------


def _gitea_headers() -> Dict[str, str]:
    return {"Authorization": f"token {GITEA_TOKEN}"}


def _gitea_get(path: str, params: Optional[Dict] = None) -> requests.Response:
    url = f"{GITEA_URL}/api/v1{path}"
    try:
        resp = requests.get(url, headers=_gitea_headers(), params=params or {}, timeout=30, verify=SSL_VERIFY)
    except requests.exceptions.RequestException as exc:
        log.error("Network error contacting Gitea (%s): %s", url, exc)
        raise
    resp.raise_for_status()
    return resp


def list_gitea_contents(path: str = "") -> List[dict]:
    """Return the directory listing at *path* in the configured repo."""
    api_path = f"/repos/{GITEA_OWNER}/{GITEA_REPO}/contents/{path}"
    return _gitea_get(api_path, {"ref": GITEA_BRANCH}).json()


def get_gitea_file_content(path: str) -> str:
    """Return the decoded text content of a file in the configured repo."""
    api_path = f"/repos/{GITEA_OWNER}/{GITEA_REPO}/contents/{path}"
    data = _gitea_get(api_path, {"ref": GITEA_BRANCH}).json()
    # Gitea encodes file content as base64; strip embedded newlines before decoding
    encoded = data["content"].replace("\n", "")
    return base64.b64decode(encoded).decode("utf-8")


# ---------------------------------------------------------------------------
# TRMM API helpers
# ---------------------------------------------------------------------------


def _trmm_headers() -> Dict[str, str]:
    return {
        "X-API-KEY": TRMM_API_KEY,
        "Content-Type": "application/json",
    }


def _trmm_get(path: str) -> requests.Response:
    url = f"{TRMM_API_URL}{path}"
    try:
        resp = requests.get(url, headers=_trmm_headers(), timeout=30, verify=SSL_VERIFY)
    except requests.exceptions.RequestException as exc:
        log.error("Network error contacting TRMM (%s): %s", url, exc)
        raise
    resp.raise_for_status()
    return resp


def _trmm_post(path: str, data: dict) -> requests.Response:
    url = f"{TRMM_API_URL}{path}"
    try:
        resp = requests.post(url, headers=_trmm_headers(), json=data, timeout=30, verify=SSL_VERIFY)
    except requests.exceptions.RequestException as exc:
        log.error("Network error contacting TRMM (%s): %s", url, exc)
        raise
    resp.raise_for_status()
    return resp


def _trmm_put(path: str, data: dict) -> requests.Response:
    url = f"{TRMM_API_URL}{path}"
    try:
        resp = requests.put(url, headers=_trmm_headers(), json=data, timeout=30, verify=SSL_VERIFY)
    except requests.exceptions.RequestException as exc:
        log.error("Network error contacting TRMM (%s): %s", url, exc)
        raise
    resp.raise_for_status()
    return resp


def get_all_trmm_scripts() -> Dict[Tuple[str, str], dict]:
    """
    Return a dict mapping (name, category) → script metadata for every
    script currently in TRMM.
    """
    resp = _trmm_get("/scripts/")
    try:
        data = resp.json()
    except (requests.exceptions.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"TRMM /scripts/ returned non-JSON response "
            f"(status {resp.status_code}): {exc}"
        ) from exc

    if not isinstance(data, list):
        raise RuntimeError(
            f"TRMM /scripts/ returned unexpected response type "
            f"{type(data).__name__!r} – expected a list. "
            f"Response: {str(data)[:200]}"
        )

    index: Dict[Tuple[str, str], dict] = {}
    for script in data:
        name = script.get("name")
        if not name:
            log.warning(
                "Skipping TRMM script with missing name (id=%s)", script.get("id")
            )
            continue
        key = (name, script.get("category") or "")
        index[key] = script
    return index


# ---------------------------------------------------------------------------
# Script discovery
# ---------------------------------------------------------------------------


def _shell_from_filename(filename: str) -> Optional[str]:
    """Return the TRMM shell type for *filename*, or None if not recognised."""
    _, ext = os.path.splitext(filename.lower())
    return EXTENSION_TO_SHELL.get(ext)


def collect_gitea_scripts() -> List[dict]:
    """
    Walk the top-level of the Gitea repo and return one dict per script::

        {"name": str, "category": str, "shell": str, "content": str}

    Only files that are **direct children of a top-level directory** are
    processed (nested sub-directories are skipped).  Files at the repository
    root are assigned an empty-string category.
    """
    scripts: List[dict] = []
    root_items = list_gitea_contents("")

    for item in root_items:
        if item["type"] == "dir":
            category = item["name"]
            try:
                dir_items = list_gitea_contents(item["path"])
            except requests.HTTPError as exc:
                log.warning("Could not list directory %s: %s", item["path"], exc)
                continue

            for file_item in dir_items:
                if file_item["type"] != "file":
                    # Skip nested subdirectories – only the top-level folder
                    # is used as the category.
                    continue
                _append_script(scripts, file_item, category)

        elif item["type"] == "file":
            # Root-level script – category is an empty string
            _append_script(scripts, item, "")

    return scripts


def _append_script(scripts: List[dict], file_item: dict, category: str) -> None:
    """Helper: validate *file_item* and append a script entry to *scripts*."""
    filename = file_item["name"]
    shell = _shell_from_filename(filename)
    if shell is None:
        log.debug("Skipping unsupported file type: %s/%s", category, filename)
        return

    name, _ = os.path.splitext(filename)

    try:
        content = get_gitea_file_content(file_item["path"])
    except requests.HTTPError as exc:
        log.warning("Could not fetch file %s: %s", file_item["path"], exc)
        return

    scripts.append(
        {
            "name": name,
            "category": category,
            "shell": shell,
            "content": content,
        }
    )


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------


GITEA_DESCRIPTION_PREFIX: str = "[Gitea]"


def _gitea_description(existing_description: str) -> str:
    """
    Return *existing_description* with ``[Gitea]`` prepended.

    Idempotent – if the prefix is already present it is not added again,
    so repeated sync runs do not accumulate multiple prefixes.
    """
    description = existing_description or ""
    if description.startswith(GITEA_DESCRIPTION_PREFIX):
        return description
    if description:
        return f"{GITEA_DESCRIPTION_PREFIX} {description}"
    return GITEA_DESCRIPTION_PREFIX


def sync_script(gitea_script: dict, trmm_index: Dict[Tuple[str, str], dict]) -> str:
    """
    Create or update a single TRMM script from *gitea_script*.

    Returns ``"created"``, ``"updated"``, or raises on error.
    """
    name: str = gitea_script["name"]
    category: str = gitea_script["category"]
    shell: str = gitea_script["shell"]
    content: str = gitea_script["content"]
    key: Tuple[str, str] = (name, category)

    if key in trmm_index:
        existing = trmm_index[key]
        script_id: int = existing["id"]

        # Preserve every TRMM-managed field; only replace the script body
        # (and keep name/category/shell consistent with Gitea).
        # Description is updated to carry the [Gitea] prefix.
        payload = {
            "name": name,
            "script_body": content,
            "shell": shell,
            "script_type": existing.get("script_type") or DEFAULT_SCRIPT_TYPE,
            "category": category,
            "description": _gitea_description(existing.get("description") or ""),
            "args": existing.get("args") or [],
            "default_timeout": existing.get("default_timeout") or DEFAULT_TIMEOUT,
            "favorite": existing.get("favorite", False),
            "hidden": existing.get("hidden", False),
            "supported_platforms": existing.get("supported_platforms")
            or DEFAULT_PLATFORMS.get(shell, ["windows"]),
            "run_as_user": existing.get("run_as_user", False),
            "env_vars": existing.get("env_vars") or [],
        }
        _trmm_put(f"/scripts/{script_id}/", payload)
        log.info("Updated  : %s [category=%s]", name, category)
        return "updated"

    # Script does not exist in TRMM yet – create it with sensible defaults.
    payload = {
        "name": name,
        "script_body": content,
        "shell": shell,
        "script_type": DEFAULT_SCRIPT_TYPE,
        "category": category,
        "description": GITEA_DESCRIPTION_PREFIX,
        "args": [],
        "default_timeout": DEFAULT_TIMEOUT,
        "favorite": False,
        "hidden": False,
        "supported_platforms": DEFAULT_PLATFORMS.get(shell, ["windows"]),
        "run_as_user": False,
        "env_vars": [],
    }
    _trmm_post("/scripts/", payload)
    log.info("Created  : %s [category=%s]", name, category)
    return "created"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _validate_config() -> bool:
    """Return True only when all required environment variables are set."""
    # Map variable name → module-level value so we can report which are missing
    # without accidentally logging their contents.
    required_names = [
        "TRMM_API_URL",
        "TRMM_API_KEY",
        "GITEA_URL",
        "GITEA_TOKEN",
        "GITEA_OWNER",
        "GITEA_REPO",
    ]
    required_values = [
        TRMM_API_URL,
        TRMM_API_KEY,
        GITEA_URL,
        GITEA_TOKEN,
        GITEA_OWNER,
        GITEA_REPO,
    ]
    missing = [name for name, val in zip(required_names, required_values) if not val]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        return False
    return True


def main() -> None:
    if not _validate_config():
        sys.exit(1)

    log.info("TRMM-Gitea sync starting")
    if IGNORE_SSL:
        log.warning("SSL certificate verification is DISABLED (IGNORE_SSL=true)")
    log.info(
        "Gitea : %s  owner=%s  repo=%s  branch=%s",
        GITEA_URL,
        GITEA_OWNER,
        GITEA_REPO,
        GITEA_BRANCH,
    )
    log.info("TRMM  : %s", TRMM_API_URL)

    log.info("Fetching scripts from TRMM …")
    try:
        trmm_index = get_all_trmm_scripts()
    except (requests.exceptions.RequestException, RuntimeError) as exc:
        log.error("Failed to fetch scripts from TRMM: %s", exc)
        sys.exit(1)
    log.info("  %d script(s) found in TRMM", len(trmm_index))

    log.info("Fetching scripts from Gitea …")
    try:
        gitea_scripts = collect_gitea_scripts()
    except requests.exceptions.RequestException as exc:
        log.error("Failed to fetch scripts from Gitea: %s", exc)
        sys.exit(1)
    log.info("  %d script(s) found in Gitea", len(gitea_scripts))

    created = updated = errors = 0

    for gs in gitea_scripts:
        try:
            result = sync_script(gs, trmm_index)
            if result == "created":
                created += 1
            else:
                updated += 1
        except requests.HTTPError as exc:
            log.error(
                "HTTP error syncing '%s' [%s]: %s",
                gs["name"],
                gs["category"],
                exc,
            )
            errors += 1
        except Exception as exc:  # pylint: disable=broad-except
            log.error(
                "Unexpected error syncing '%s' [%s]: %s",
                gs["name"],
                gs["category"],
                exc,
            )
            errors += 1

    log.info(
        "Sync complete – created: %d  updated: %d  errors: %d",
        created,
        updated,
        errors,
    )

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
