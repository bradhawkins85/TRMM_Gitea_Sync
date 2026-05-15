#!/usr/bin/env python3
"""TRMM-GitHub Sync

Synchronises scripts stored in a GitHub repository with the Tactical RMM
script library.

Rules
-----
* The top-level folder a script lives in becomes its TRMM *category*.
* The filename (without extension) becomes the TRMM *script name*.
* The file extension determines the TRMM *shell* type.
* GitHub content always wins – the script body is overwritten on every run.
* TRMM-managed settings (args, supported_platforms, run_as_user, env_vars,
  default_timeout, favorite, hidden) are **never** overwritten for scripts
  that already exist in TRMM.
* Scripts removed from GitHub are deleted from TRMM, **but only if** their
  description begins with the ``[GitHub]`` prefix (i.e. they were originally
  created by this sync tool).  Scripts created directly inside TRMM are never
  deleted.

Configuration (.env file or environment variables)
--------------------------------------------------
Values are loaded from a ``.env`` file in the working directory (via
python-dotenv) and then fall back to actual environment variables, so both
approaches work.

TRMM_API_URL    Base URL of the Tactical RMM instance, e.g. https://rmm.example.com
TRMM_API_KEY    Tactical RMM API key
GITHUB_TOKEN    GitHub personal access token (required for private repos;
                recommended for public repos to avoid rate-limiting)
GITHUB_OWNER    GitHub repository owner (user or org)
GITHUB_REPO     GitHub repository name
GITHUB_BRANCH   Branch to read from (default: main)
GITHUB_API_URL  GitHub API base URL (default: https://api.github.com).
                Override for GitHub Enterprise Server, e.g.
                https://github.example.com/api/v3
IGNORE_SSL      Set to "true", "1", or "yes" to disable SSL certificate
                verification for all API calls.  Useful when the script runs
                on the TRMM server itself where the API hostname resolves to
                127.0.0.1 and the certificate CN does not match (default: false)
"""

import base64
import json
import logging
import os
import re
import sys
import tempfile
from typing import Dict, List, Optional, Set, Tuple

import requests
import urllib3
from dotenv import load_dotenv

load_dotenv()

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

GITHUB_API_URL: str = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
GITHUB_TOKEN: str = os.environ.get("GITHUB_TOKEN", "")
GITHUB_OWNER: str = os.environ.get("GITHUB_OWNER", "")
GITHUB_REPO: str = os.environ.get("GITHUB_REPO", "")
GITHUB_BRANCH: str = os.environ.get("GITHUB_BRANCH", "main")

IGNORE_SSL: bool = os.environ.get("IGNORE_SSL", "").lower() in ("1", "true", "yes")
SSL_VERIFY: bool = not IGNORE_SSL

# Path to the persistent JSON state file that tracks the
# repo-relative-path → TRMM-guid mapping (and previous blob SHA, used to
# infer GitHub-side renames) across runs.  Defaults to a ``.trmm_sync_state.json``
# file in the current working directory.
SYNC_STATE_FILE: str = os.environ.get("SYNC_STATE_FILE", "")

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
    ".bat": "cmd",
    ".cmd": "cmd",
}

# Default supported_platforms for newly created scripts, keyed by shell type
DEFAULT_PLATFORMS: Dict[str, List[str]] = {
    "powershell": ["windows"],
    "python": ["windows", "linux", "darwin"],
    "shell": ["linux", "darwin"],
    "cmd": ["windows"],
}

DEFAULT_SCRIPT_TYPE: str = "userdefined"
DEFAULT_TIMEOUT: int = 90

# ---------------------------------------------------------------------------
# Persistent state (path → {guid, sha} mapping)
# ---------------------------------------------------------------------------

STATE_FILE_VERSION: int = 1


def _state_file_path() -> str:
    """Return the absolute path of the state file (env var override or default)."""
    if SYNC_STATE_FILE:
        return os.path.abspath(SYNC_STATE_FILE)
    return os.path.abspath(".trmm_github_sync_state.json")


def load_state() -> Tuple[Dict[str, str], Dict[str, str]]:
    """Load the persisted state.

    Returns ``(path_to_guid, path_to_sha)``.  Both maps are empty when the
    file is missing, unreadable, or fails schema validation – the sync will
    rebuild the binding via ``[GitHub:<guid>]`` description stamps and
    ``(name, category)`` fallbacks.
    """
    path = _state_file_path()
    if not os.path.isfile(path):
        return {}, {}

    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "Could not read sync state file %s (%s) – rebuilding mapping from "
            "TRMM script descriptions and (name, category) fallbacks.",
            path,
            exc,
        )
        return {}, {}

    if not isinstance(raw, dict) or not isinstance(raw.get("paths"), dict):
        log.warning(
            "Sync state file %s has unexpected schema – rebuilding mapping.",
            path,
        )
        return {}, {}

    guid_map: Dict[str, str] = {}
    sha_map: Dict[str, str] = {}
    seen_guids: Set[str] = set()
    duplicate_guids: Set[str] = set()

    for rel_path, value in raw["paths"].items():
        if not isinstance(rel_path, str):
            continue
        if isinstance(value, str):
            guid = value
        elif isinstance(value, dict):
            guid = value.get("guid") or ""
            sha = value.get("sha") or ""
            if isinstance(sha, str) and sha:
                sha_map[rel_path] = sha
        else:
            continue
        if not guid:
            continue
        if guid in seen_guids:
            duplicate_guids.add(guid)
            continue
        seen_guids.add(guid)
        guid_map[rel_path] = guid

    if duplicate_guids:
        log.warning(
            "Sync state file contained %d duplicated guid(s); dropping affected "
            "entries and falling back to (name, category) matching.",
            len(duplicate_guids),
        )
        guid_map = {p: g for p, g in guid_map.items() if g not in duplicate_guids}

    return guid_map, sha_map


def save_state(guid_map: Dict[str, str], sha_map: Dict[str, str]) -> None:
    """Write the state file atomically."""
    path = _state_file_path()
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        log.warning("Could not create directory for state file %s: %s", path, exc)
        return

    paths_obj: Dict[str, Dict[str, str]] = {}
    for rel_path, guid in guid_map.items():
        entry: Dict[str, str] = {"guid": guid}
        sha = sha_map.get(rel_path)
        if sha:
            entry["sha"] = sha
        paths_obj[rel_path] = entry

    payload = {"version": STATE_FILE_VERSION, "paths": paths_obj}
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".trmm_github_sync_state.",
            suffix=".tmp",
            dir=parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        log.warning("Could not write state file %s: %s", path, exc)


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def _github_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _github_get(path: str, params: Optional[Dict] = None) -> requests.Response:
    url = f"{GITHUB_API_URL}{path}"
    try:
        resp = requests.get(url, headers=_github_headers(), params=params or {}, timeout=30, verify=SSL_VERIFY)
    except requests.exceptions.RequestException as exc:
        log.error("Network error contacting GitHub (%s): %s", url, exc)
        raise
    resp.raise_for_status()
    return resp


def list_github_contents(path: str = "") -> List[dict]:
    """Return the directory listing at *path* in the configured repo."""
    api_path = f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    return _github_get(api_path, {"ref": GITHUB_BRANCH}).json()


def get_github_file_content(path: str) -> str:
    """Return the decoded text content of a file in the configured repo."""
    api_path = f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    data = _github_get(api_path, {"ref": GITHUB_BRANCH}).json()
    # GitHub encodes file content as base64; strip embedded newlines before decoding.
    # Use "utf-8-sig" so that a UTF-8 BOM (common in Windows-authored PowerShell
    # files) is silently stripped rather than included in the script body.
    encoded = data["content"].replace("\n", "")
    raw = base64.b64decode(encoded)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    # Normalize Windows-style line endings so that the body compared against
    # what TRMM stores uses a consistent line-ending style, preventing spurious
    # updates on every sync run when scripts contain \r\n.
    return text.replace("\r\n", "\n")


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


def _trmm_delete(path: str) -> requests.Response:
    url = f"{TRMM_API_URL}{path}"
    try:
        resp = requests.delete(url, headers=_trmm_headers(), timeout=30, verify=SSL_VERIFY)
    except requests.exceptions.RequestException as exc:
        log.error("Network error contacting TRMM (%s): %s", url, exc)
        raise
    resp.raise_for_status()
    return resp


def get_trmm_script_detail(script_id: int) -> dict:
    """Return full metadata for a single TRMM script, including ``script_body``."""
    resp = _trmm_get(f"/scripts/{script_id}/")
    return resp.json()


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


def collect_github_scripts() -> List[dict]:
    """
    Walk the top-level of the GitHub repo and return one dict per script::

        {"name": str, "category": str, "shell": str, "content": str}

    Only files that are **direct children of a top-level directory** are
    processed (nested sub-directories are skipped).  Files at the repository
    root are assigned an empty-string category.
    """
    scripts: List[dict] = []
    root_items = list_github_contents("")

    for item in root_items:
        if item["type"] == "dir":
            category = item["name"]
            try:
                dir_items = list_github_contents(item["path"])
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
        content = get_github_file_content(file_item["path"])
    except requests.HTTPError as exc:
        log.warning("Could not fetch file %s: %s", file_item["path"], exc)
        return

    scripts.append(
        {
            "name": name,
            "category": category,
            "shell": shell,
            "content": content,
            "path": file_item.get("path") or filename,
            "sha": file_item.get("sha") or "",
        }
    )


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------


GITHUB_DESCRIPTION_PREFIX: str = "[GitHub]"

GITHUB_DESCRIPTION_RE: "re.Pattern[str]" = re.compile(
    r"^\[GitHub(?::(?P<guid>[^\]]+))?\]"
)


def _is_github_managed(description: Optional[str]) -> bool:
    """Return True if *description* indicates a GitHub-managed script."""
    if not description:
        return False
    return bool(GITHUB_DESCRIPTION_RE.match(description))


def _guid_from_description(description: Optional[str]) -> Optional[str]:
    """Return the embedded TRMM guid from a ``[GitHub:<guid>]`` prefix, if any."""
    if not description:
        return None
    match = GITHUB_DESCRIPTION_RE.match(description)
    if not match:
        return None
    return match.group("guid")


def _github_description(existing_description: str, guid: Optional[str] = None) -> str:
    """
    Return *existing_description* with the ``[GitHub]`` (or ``[GitHub:<guid>]``)
    prefix applied.

    Idempotent – any existing ``[GitHub]`` / ``[GitHub:<old-guid>]`` prefix is
    replaced rather than accumulated.  When *guid* is provided, the
    guid-stamped form is used as a belt-and-braces backup that lets the
    mapping be rebuilt if the JSON state file is lost.
    """
    description = existing_description or ""
    new_prefix = (
        f"[GitHub:{guid}]" if guid else GITHUB_DESCRIPTION_PREFIX
    )

    match = GITHUB_DESCRIPTION_RE.match(description)
    if match:
        remainder = description[match.end():]
        if remainder.startswith(" "):
            remainder = remainder[1:]
        if remainder:
            return f"{new_prefix} {remainder}"
        return new_prefix

    if description:
        return f"{new_prefix} {description}"
    return new_prefix


def _build_guid_index(
    trmm_index: Dict[Tuple[str, str], dict],
) -> Dict[str, Tuple[Tuple[str, str], dict]]:
    """Return ``guid → ((name, category), script)`` for every script that has a guid."""
    result: Dict[str, Tuple[Tuple[str, str], dict]] = {}
    for key, script in trmm_index.items():
        guid = script.get("guid")
        if guid:
            result[guid] = (key, script)
    return result


def _resolve_existing_script(
    rel_path: str,
    name: str,
    category: str,
    state: Dict[str, str],
    trmm_index: Dict[Tuple[str, str], dict],
    guid_index: Dict[str, Tuple[Tuple[str, str], dict]],
) -> Tuple[Optional[Tuple[str, str]], Optional[dict]]:
    """Find an existing TRMM script that matches *rel_path*.

    Resolution order:
      1. State file: ``rel_path → guid`` → guid index.
      2. ``(name, category)`` lookup against TRMM (adoption of pre-existing
         scripts that have not yet been bound to a guid in the state file).
      3. Description-stamp recovery (``[GitHub:<guid>]``).
    """
    guid = state.get(rel_path)
    if guid and guid in guid_index:
        return guid_index[guid]

    key: Tuple[str, str] = (name, category)
    if key in trmm_index:
        return key, trmm_index[key]

    if guid:
        for trmm_key, script in trmm_index.items():
            if _guid_from_description(script.get("description")) == guid:
                return trmm_key, script

    return None, None


def sync_script(
    github_script: dict,
    trmm_index: Dict[Tuple[str, str], dict],
    guid_index: Dict[str, Tuple[Tuple[str, str], dict]],
    state: Dict[str, str],
) -> str:
    """
    Create or update a single TRMM script from *github_script*.

    Returns ``"created"``, ``"updated"``, ``"skipped"``, or raises on error.

    Matching is guid-first (via *state*) with a ``(name, category)`` fallback
    so that pre-existing TRMM scripts are adopted on first sync.  When a match
    is found the existing TRMM record is updated in place – including its
    ``name`` and ``category`` – so renames/moves in GitHub propagate without
    losing the TRMM script ``id`` or any TRMM-managed settings.
    """
    name: str = github_script["name"]
    category: str = github_script["category"]
    shell: str = github_script["shell"]
    content: str = github_script["content"]
    rel_path: str = github_script["path"]

    existing_key, existing = _resolve_existing_script(
        rel_path, name, category, state, trmm_index, guid_index
    )

    if existing is not None and existing_key is not None:
        script_id: int = existing["id"]

        # The TRMM list endpoint (/scripts/) omits script_body for performance.
        # Fetch the full script detail so we can compare bodies accurately and
        # update the cache so any subsequent reference to this entry is complete.
        if "script_body" not in existing:
            existing = get_trmm_script_detail(script_id)
            trmm_index[existing_key] = existing
            guid = existing.get("guid")
            if guid:
                guid_index[guid] = (existing_key, existing)

        guid = existing.get("guid")
        existing_body: str = existing.get("script_body") or ""
        existing_description: str = existing.get("description") or ""
        new_description: str = _github_description(existing_description, guid)
        existing_name: str = existing.get("name") or ""
        existing_category: str = existing.get("category") or ""

        if guid:
            state[rel_path] = guid

        if (
            existing_body == content
            and existing_description == new_description
            and existing_name == name
            and existing_category == category
        ):
            log.debug("Skipped  : %s [category=%s] (no changes)", name, category)
            return "skipped"

        # Preserve every TRMM-managed field; only replace the script body
        # (and keep name/category/shell consistent with GitHub).
        # Description is updated to carry the [GitHub] / [GitHub:<guid>] prefix.
        payload = {
            "name": name,
            "script_body": content,
            "shell": shell,
            "script_type": existing.get("script_type") or DEFAULT_SCRIPT_TYPE,
            "category": category,
            "description": new_description,
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

        if existing_key != (name, category):
            trmm_index.pop(existing_key, None)
        existing.update(
            {
                "name": name,
                "category": category,
                "script_body": content,
                "description": new_description,
                "shell": shell,
            }
        )
        trmm_index[(name, category)] = existing
        if guid:
            guid_index[guid] = ((name, category), existing)

        if existing_name != name or existing_category != category:
            log.info(
                "Renamed  : %s [category=%s] → %s [category=%s]",
                existing_name,
                existing_category,
                name,
                category,
            )
        else:
            log.info("Updated  : %s [category=%s]", name, category)
        return "updated"

    # Script does not exist in TRMM yet – create it with sensible defaults.
    payload = {
        "name": name,
        "script_body": content,
        "shell": shell,
        "script_type": DEFAULT_SCRIPT_TYPE,
        "category": category,
        "description": GITHUB_DESCRIPTION_PREFIX,
        "args": [],
        "default_timeout": DEFAULT_TIMEOUT,
        "favorite": False,
        "hidden": False,
        "supported_platforms": DEFAULT_PLATFORMS.get(shell, ["windows"]),
        "run_as_user": False,
        "env_vars": [],
    }
    resp = _trmm_post("/scripts/", payload)

    new_guid: Optional[str] = None
    try:
        body = resp.json()
        if isinstance(body, dict):
            new_guid = body.get("guid")
            new_id = body.get("id")
            if new_guid and new_id:
                stamped_description = _github_description("", new_guid)
                try:
                    _trmm_put(
                        f"/scripts/{new_id}/",
                        {**payload, "description": stamped_description},
                    )
                except requests.HTTPError as exc:
                    log.debug(
                        "Could not stamp guid into description for new script "
                        "'%s' [%s]: %s",
                        name,
                        category,
                        exc,
                    )
    except (ValueError, requests.exceptions.JSONDecodeError):
        pass

    if not new_guid:
        try:
            refreshed = get_all_trmm_scripts()
            entry = refreshed.get((name, category))
            if entry:
                new_guid = entry.get("guid")
        except (requests.exceptions.RequestException, RuntimeError):
            pass

    if new_guid:
        state[rel_path] = new_guid

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
        "GITHUB_TOKEN",
        "GITHUB_OWNER",
        "GITHUB_REPO",
    ]
    required_values = [
        TRMM_API_URL,
        TRMM_API_KEY,
        GITHUB_TOKEN,
        GITHUB_OWNER,
        GITHUB_REPO,
    ]
    missing = [name for name, val in zip(required_names, required_values) if not val]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        return False
    return True


def main() -> None:
    if not _validate_config():
        sys.exit(1)

    log.info("TRMM-GitHub sync starting")
    if IGNORE_SSL:
        log.warning("SSL certificate verification is DISABLED (IGNORE_SSL=true)")
    log.info(
        "GitHub: %s  owner=%s  repo=%s  branch=%s",
        GITHUB_API_URL,
        GITHUB_OWNER,
        GITHUB_REPO,
        GITHUB_BRANCH,
    )
    log.info("TRMM  : %s", TRMM_API_URL)

    # ------------------------------------------------------------------
    # Load persistent path → guid (and previous blob SHA) state.
    # ------------------------------------------------------------------
    state, previous_shas = load_state()
    if state:
        log.info(
            "Loaded %d path → guid mapping(s) from %s",
            len(state),
            _state_file_path(),
        )

    log.info("Fetching scripts from TRMM …")
    try:
        trmm_index = get_all_trmm_scripts()
    except (requests.exceptions.RequestException, RuntimeError) as exc:
        log.error("Failed to fetch scripts from TRMM: %s", exc)
        sys.exit(1)
    log.info("  %d script(s) found in TRMM", len(trmm_index))
    guid_index = _build_guid_index(trmm_index)

    log.info("Fetching scripts from GitHub …")
    try:
        github_scripts = collect_github_scripts()
    except requests.exceptions.RequestException as exc:
        log.error("Failed to fetch scripts from GitHub: %s", exc)
        sys.exit(1)
    log.info("  %d script(s) found in GitHub", len(github_scripts))

    # ------------------------------------------------------------------
    # Infer GitHub-side renames so the path → guid binding can be carried
    # forward (preserving the TRMM script id and all TRMM-managed
    # settings) instead of looking like a delete + create pair.
    #
    # A rename is detected when a previously known path is no longer
    # present *and* a new path appears whose blob SHA matches the missing
    # path's previous blob SHA.  Identical content is the strongest
    # signal we can derive without git history.
    # ------------------------------------------------------------------
    current_paths: Set[str] = {gs["path"] for gs in github_scripts}
    current_shas: Dict[str, str] = {
        gs["path"]: gs["sha"] for gs in github_scripts if gs.get("sha")
    }
    missing_paths: Set[str] = set(state.keys()) - current_paths
    new_paths: Set[str] = current_paths - set(state.keys())

    # Build a reverse lookup of previously-seen SHAs that no longer have
    # their original path present in the repo.
    candidate_renames: Dict[str, str] = {}  # sha → old_path
    for old_path in missing_paths:
        sha = previous_shas.get(old_path)
        if sha:
            candidate_renames.setdefault(sha, old_path)

    rename_map: Dict[str, str] = {}  # new_path → old_path
    for new_path in new_paths:
        sha = current_shas.get(new_path)
        if sha and sha in candidate_renames:
            rename_map[new_path] = candidate_renames.pop(sha)

    if rename_map:
        log.info(
            "Detected %d rename(s) by matching blob SHAs against the previous run",
            len(rename_map),
        )
        for new_path, old_path in rename_map.items():
            # Carry the previous binding forward so sync_script finds the
            # existing TRMM record via the state mapping.
            if old_path in state and new_path not in state:
                state[new_path] = state.pop(old_path)
            else:
                state.pop(old_path, None)

    created = updated = skipped = renamed = errors = 0

    for gs in github_scripts:
        try:
            old_path = rename_map.get(gs["path"])
            old_parsed = None
            if old_path:
                # Mirror sync.py's rename log line by recovering the previous
                # (name, category) so we can detect a "renamed" outcome.
                base = os.path.basename(old_path)
                old_name, _ = os.path.splitext(base)
                old_category = (
                    os.path.dirname(old_path).split("/")[0]
                    if "/" in old_path
                    else ""
                )
                old_parsed = (old_name, old_category)

            result = sync_script(gs, trmm_index, guid_index, state)
            if result == "created":
                created += 1
            elif result == "skipped":
                skipped += 1
            else:
                if old_parsed and old_parsed != (gs["name"], gs["category"]):
                    renamed += 1
                else:
                    updated += 1
        except requests.HTTPError as exc:
            response_detail = ""
            if exc.response is not None:
                try:
                    response_detail = f"  Response body: {exc.response.text}"
                except Exception:  # pylint: disable=broad-except
                    pass
            log.error(
                "HTTP error syncing '%s' [%s]: %s%s",
                gs["name"],
                gs["category"],
                exc,
                response_detail,
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

    # ------------------------------------------------------------------
    # Delete TRMM scripts that were removed from GitHub.  Match by guid
    # (via the state file) as well as by (name, category) so we don't
    # accidentally delete a script that was just renamed in TRMM.
    # ------------------------------------------------------------------
    github_keys: Set[Tuple[str, str]] = {
        (gs["name"], gs["category"]) for gs in github_scripts
    }
    github_guids: Set[str] = {state[p] for p in state if state.get(p)}

    deleted = 0
    for key, script in list(trmm_index.items()):
        description = script.get("description") or ""
        if not _is_github_managed(description):
            continue
        if key in github_keys:
            continue
        if script.get("guid") and script["guid"] in github_guids:
            continue
        script_id = script["id"]
        name, category = key
        try:
            _trmm_delete(f"/scripts/{script_id}/")
            log.info("Deleted  : %s [category=%s]", name, category)
            deleted += 1
            guid = script.get("guid")
            if guid:
                for sp in [p for p, g in state.items() if g == guid]:
                    state.pop(sp, None)
        except requests.HTTPError as exc:
            log.error(
                "HTTP error deleting '%s' [%s]: %s",
                name,
                category,
                exc,
            )
            errors += 1
        except Exception as exc:  # pylint: disable=broad-except
            log.error(
                "Unexpected error deleting '%s' [%s]: %s",
                name,
                category,
                exc,
            )
            errors += 1

    # ------------------------------------------------------------------
    # Persist the updated path → guid + sha state for the next run.
    # ------------------------------------------------------------------
    save_state(state, current_shas)

    log.info(
        "Sync complete – created: %d  updated: %d  renamed: %d  skipped: %d  deleted: %d  errors: %d",
        created,
        updated,
        renamed,
        skipped,
        deleted,
        errors,
    )

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
