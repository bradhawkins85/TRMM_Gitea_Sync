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
* Scripts removed from Gitea are deleted from TRMM, **but only if** their
  description begins with the ``[Gitea]`` prefix (i.e. they were originally
  created by this sync tool).  Scripts created directly inside TRMM are never
  deleted.

Configuration (.env file or environment variables)
--------------------------------------------------
Values are loaded from a ``.env`` file in the working directory (via
python-dotenv) and then fall back to actual environment variables, so both
approaches work.

TRMM_API_URL      Base URL of the Tactical RMM instance, e.g. https://rmm.example.com
TRMM_API_KEY      Tactical RMM API key
GITEA_URL         Base URL of the Gitea instance, e.g. https://gitea.example.com
GITEA_TOKEN       Gitea access token (required for private repos)
GITEA_OWNER       Gitea repository owner (user or org)
GITEA_REPO        Gitea repository name
GITEA_BRANCH      Branch to read from (default: main)
GITEA_LOCAL_PATH  Path to the local clone of the Gitea repo (default: ./gitea_repo).
                  On first run the repo is cloned here; subsequent runs do a
                  ``git pull`` and only scripts whose files changed are synced.
FULL_SYNC         Set to "true", "1", or "yes" to sync every script regardless
                  of what changed in git.  Useful to force a complete re-sync
                  after editing TRMM scripts manually (default: false).
IGNORE_SSL        Set to "true", "1", or "yes" to disable SSL certificate
                  verification for all API calls.  Useful when the script runs
                  on the TRMM server itself where the API hostname resolves to
                  127.0.0.1 and the certificate CN does not match (default: false)
"""

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, urlunparse

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

GITEA_URL: str = os.environ.get("GITEA_URL", "").rstrip("/")
GITEA_TOKEN: str = os.environ.get("GITEA_TOKEN", "")
GITEA_OWNER: str = os.environ.get("GITEA_OWNER", "")
GITEA_REPO: str = os.environ.get("GITEA_REPO", "")
GITEA_BRANCH: str = os.environ.get("GITEA_BRANCH", "main")
GITEA_LOCAL_PATH: str = os.environ.get("GITEA_LOCAL_PATH", "./gitea_repo")

FULL_SYNC: bool = os.environ.get("FULL_SYNC", "").lower() in ("1", "true", "yes")

# Path to the persistent JSON state file that tracks the
# repo-relative-path → TRMM-guid mapping across runs.  Defaults to a sibling of
# the local Gitea clone (``<parent of GITEA_LOCAL_PATH>/.trmm_sync_state.json``)
# so that ``rm -rf`` of the clone does not also delete the state file.
SYNC_STATE_FILE: str = os.environ.get("SYNC_STATE_FILE", "")

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
# Persistent state (path → TRMM guid mapping)
# ---------------------------------------------------------------------------

STATE_FILE_VERSION: int = 1


def _default_state_file_path() -> str:
    """Return the default location for the JSON state file.

    Sibling of ``GITEA_LOCAL_PATH`` (i.e. lives in its parent directory) so a
    ``rm -rf`` of the clone does not also delete the mapping file.
    """
    local_abs = os.path.abspath(GITEA_LOCAL_PATH)
    parent = os.path.dirname(local_abs) or "."
    return os.path.join(parent, ".trmm_sync_state.json")


def _state_file_path() -> str:
    """Return the absolute path of the state file (env var override or default)."""
    if SYNC_STATE_FILE:
        return os.path.abspath(SYNC_STATE_FILE)
    return _default_state_file_path()


def load_state() -> Dict[str, str]:
    """Load the ``path → guid`` mapping from disk.

    Returns an empty dict if the file is missing or unreadable.  Logs a warning
    and returns an empty dict on JSON corruption / unexpected schema rather
    than raising, so the sync can rebuild the mapping via the
    ``(name, category)`` + description-stamp fallbacks.
    """
    path = _state_file_path()
    if not os.path.isfile(path):
        return {}

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
        return {}

    if not isinstance(raw, dict) or not isinstance(raw.get("paths"), dict):
        log.warning(
            "Sync state file %s has unexpected schema – rebuilding mapping.",
            path,
        )
        return {}

    paths_obj = raw["paths"]
    mapping: Dict[str, str] = {}
    seen_guids: Set[str] = set()
    duplicate_guids: Set[str] = set()

    for rel_path, guid in paths_obj.items():
        if not isinstance(rel_path, str) or not isinstance(guid, str) or not guid:
            continue
        if guid in seen_guids:
            duplicate_guids.add(guid)
            continue
        seen_guids.add(guid)
        mapping[rel_path] = guid

    if duplicate_guids:
        # Drop every entry tied to a duplicated guid so the matching logic
        # safely falls back to (name, category) adoption for those scripts.
        log.warning(
            "Sync state file contained %d duplicated guid(s); dropping affected "
            "entries and falling back to (name, category) matching.",
            len(duplicate_guids),
        )
        mapping = {
            p: g for p, g in mapping.items() if g not in duplicate_guids
        }

    return mapping


def save_state(state: Dict[str, str]) -> None:
    """Write *state* atomically to the configured state file."""
    path = _state_file_path()
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        log.warning("Could not create directory for state file %s: %s", path, exc)
        return

    payload = {"version": STATE_FILE_VERSION, "paths": state}
    try:
        # Write to a sibling temp file then os.replace for atomicity.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".trmm_sync_state.", suffix=".tmp", dir=parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_path, path)
            # On POSIX, restrict the state file to the owner – it contains
            # TRMM script guids which, while not secret, are internal
            # identifiers that don't need to be world-readable.  Best-effort:
            # silently ignored where chmod is unsupported (e.g. Windows).
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except Exception:
            # Best-effort cleanup of the temp file on failure.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        log.warning("Could not write state file %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Git / local-clone helpers
# ---------------------------------------------------------------------------


def _build_clone_url() -> str:
    """Return the authenticated HTTPS clone URL for the configured Gitea repo."""
    parsed = urlparse(GITEA_URL)
    netloc_with_auth = f"oauth2:{GITEA_TOKEN}@{parsed.netloc}"
    repo_path = f"/{GITEA_OWNER}/{GITEA_REPO}.git"
    return urlunparse((parsed.scheme, netloc_with_auth, repo_path, "", "", ""))


def _git_run(args: List[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a git sub-command and return the CompletedProcess.  Raises RuntimeError on failure."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Avoid leaking the token that may appear in URLs within error messages.
        safe_stderr = result.stderr.replace(GITEA_TOKEN, "***") if GITEA_TOKEN else result.stderr
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {safe_stderr.strip()}")
    return result


def _ensure_local_repo() -> Tuple[bool, List[str], List[str], List[Tuple[str, str]]]:
    """Ensure a local clone of the Gitea repo exists at ``GITEA_LOCAL_PATH``.

    * On the first run (or when the directory is not a git repository) the repo
      is cloned from Gitea.
    * On subsequent runs ``git pull`` is executed and the files that changed
      between the previous HEAD and the new HEAD are recorded.

    Returns:
        (is_fresh_clone, modified_paths, deleted_paths, rename_pairs)

    ``is_fresh_clone`` is ``True`` when the repo was just cloned for the first
    time; in that case ``modified_paths``, ``deleted_paths`` and
    ``rename_pairs`` are all empty (the caller should treat every file as new).

    ``modified_paths`` and ``deleted_paths`` contain repository-relative POSIX
    paths (e.g. ``"Checks/Check Disk Space.ps1"``).  ``rename_pairs`` is a list
    of ``(old_path, new_path)`` tuples for files that git detected as renames.
    Renamed files are *not* duplicated in ``modified_paths`` or
    ``deleted_paths`` – callers must process renames separately so the
    corresponding TRMM script can be moved/renamed in place rather than
    deleted-and-recreated (which would lose its preserved settings).
    """
    local_path = os.path.abspath(GITEA_LOCAL_PATH)

    is_git_repo = os.path.isdir(os.path.join(local_path, ".git"))

    if not is_git_repo:
        non_empty = False
        if os.path.exists(local_path):
            try:
                non_empty = bool(os.listdir(local_path))
            except OSError:
                non_empty = True  # conservative: assume non-empty on access error
        if non_empty:
            log.error(
                "GITEA_LOCAL_PATH '%s' already exists and is not a git repository. "
                "Remove or empty the directory, or set GITEA_LOCAL_PATH to a different path.",
                local_path,
            )
            raise RuntimeError(f"Not a git repository: {local_path}")

        log.info("Cloning Gitea repo to %s …", local_path)
        clone_url = _build_clone_url()
        _git_run(["clone", "--branch", GITEA_BRANCH, clone_url, local_path])
        log.info("  Clone complete")
        return True, [], [], []

    # Existing clone – pull and collect changed paths.
    old_head = _git_run(["rev-parse", "HEAD"], cwd=local_path).stdout.strip()

    # Keep the remote URL up to date in case the token has changed.
    clone_url = _build_clone_url()
    _git_run(["remote", "set-url", "origin", clone_url], cwd=local_path)

    log.info("Pulling latest changes in %s …", local_path)
    _git_run(["pull", "origin", GITEA_BRANCH], cwd=local_path)

    new_head = _git_run(["rev-parse", "HEAD"], cwd=local_path).stdout.strip()

    if old_head == new_head:
        log.info("  Already up to date (HEAD=%s)", new_head[:8])
        return False, [], [], []

    log.info("  Updated %s → %s", old_head[:8], new_head[:8])

    # ``-M50%`` explicitly pins the rename-detection similarity threshold at
    # git's documented default (50%) so that a future change to the default
    # cannot silently regress rename handling.  Files that are both renamed
    # *and* modified are still detected as renames as long as at least half
    # their content survives.
    diff_output = _git_run(
        ["diff", "--name-status", "-M50%", old_head, new_head],
        cwd=local_path,
    ).stdout.strip()

    modified_paths: List[str] = []
    deleted_paths: List[str] = []
    rename_pairs: List[Tuple[str, str]] = []

    for line in diff_output.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R"):
            # Rename: R<score>\t<old_path>\t<new_path>
            if len(parts) >= 3:
                rename_pairs.append((parts[1], parts[2]))
        elif status.startswith("C"):
            # Copy: C<score>\t<src_path>\t<dst_path> – only the new file needs syncing
            if len(parts) >= 3:
                modified_paths.append(parts[2])
        elif status in ("A", "M"):
            if len(parts) >= 2:
                modified_paths.append(parts[1])
        elif status == "D":
            if len(parts) >= 2:
                deleted_paths.append(parts[1])

    return False, modified_paths, deleted_paths, rename_pairs


# ---------------------------------------------------------------------------
# Script discovery (local filesystem)
# ---------------------------------------------------------------------------


def _shell_from_filename(filename: str) -> Optional[str]:
    """Return the TRMM shell type for *filename*, or None if not recognised."""
    _, ext = os.path.splitext(filename.lower())
    return EXTENSION_TO_SHELL.get(ext)


def _append_local_script(
    scripts: List[dict], file_path: str, category: str, rel_path: str
) -> None:
    """Read *file_path* from disk and append a script entry to *scripts*."""
    filename = os.path.basename(file_path)
    shell = _shell_from_filename(filename)
    if shell is None:
        log.debug("Skipping unsupported file type: %s/%s", category, filename)
        return

    name, _ = os.path.splitext(filename)

    try:
        with open(file_path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        log.warning("Could not read file %s: %s", file_path, exc)
        return

    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("latin-1")

    # Normalise Windows-style line endings.
    content = content.replace("\r\n", "\n")

    scripts.append(
        {
            "name": name,
            "category": category,
            "shell": shell,
            "content": content,
            "path": rel_path,
        }
    )


def _parse_script_path(rel_path: str) -> Optional[Tuple[str, str]]:
    """Parse a repo-relative path into ``(script_name, category)``.

    Returns ``None`` for unsupported file types or nested paths (more than one
    directory level deep), as those are outside our one-level category model.
    """
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) == 1:
        filename, category = parts[0], ""
    elif len(parts) == 2:
        category, filename = parts[0], parts[1]
    else:
        return None  # nested path – skip

    if _shell_from_filename(filename) is None:
        return None

    name, _ = os.path.splitext(filename)
    return name, category


def collect_local_scripts(filter_paths: Optional[Set[str]] = None) -> List[dict]:
    """Walk the local clone and return one dict per script.

    Only files that are **direct children of a top-level directory** are
    processed; nested sub-directories are skipped.  Files at the repository
    root are assigned an empty-string category.

    If *filter_paths* is provided (a set of repository-relative POSIX paths),
    only scripts whose path is in the set are returned, enabling incremental
    syncing when only a subset of files changed.
    """
    local_path = os.path.abspath(GITEA_LOCAL_PATH)
    scripts: List[dict] = []

    try:
        root_entries = list(os.scandir(local_path))
    except OSError as exc:
        log.error("Cannot scan local repo at %s: %s", local_path, exc)
        raise

    for entry in root_entries:
        if entry.name.startswith("."):
            continue

        if entry.is_dir(follow_symlinks=False):
            category = entry.name
            try:
                dir_entries = list(os.scandir(entry.path))
            except OSError as exc:
                log.warning("Could not scan directory %s: %s", entry.path, exc)
                continue

            for file_entry in dir_entries:
                if not file_entry.is_file(follow_symlinks=False):
                    # Skip nested sub-directories.
                    continue
                rel_path = f"{category}/{file_entry.name}"
                if filter_paths is not None and rel_path not in filter_paths:
                    continue
                _append_local_script(scripts, file_entry.path, category, rel_path)

        elif entry.is_file(follow_symlinks=False):
            rel_path = entry.name
            if filter_paths is not None and rel_path not in filter_paths:
                continue
            _append_local_script(scripts, entry.path, "", rel_path)

    return scripts


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
# Sync logic
# ---------------------------------------------------------------------------


GITEA_DESCRIPTION_PREFIX: str = "[Gitea]"

# Matches both the legacy ``[Gitea]`` prefix and the guid-stamped
# ``[Gitea:<guid>]`` prefix at the start of a description.  Used to identify
# scripts that were created/managed by this sync tool, and to recover the
# associated TRMM guid when the JSON state file is missing or stale.
GITEA_DESCRIPTION_RE: "re.Pattern[str]" = re.compile(
    r"^\[Gitea(?::(?P<guid>[^\]]+))?\]"
)


def _is_gitea_managed(description: Optional[str]) -> bool:
    """Return True if *description* indicates a Gitea-managed script."""
    if not description:
        return False
    return bool(GITEA_DESCRIPTION_RE.match(description))


def _guid_from_description(description: Optional[str]) -> Optional[str]:
    """Return the embedded TRMM guid from a ``[Gitea:<guid>]`` prefix, if any."""
    if not description:
        return None
    match = GITEA_DESCRIPTION_RE.match(description)
    if not match:
        return None
    return match.group("guid")


def _gitea_description(existing_description: str, guid: Optional[str] = None) -> str:
    """
    Return *existing_description* with the ``[Gitea]`` (or ``[Gitea:<guid>]``)
    prefix applied.

    Idempotent – any existing ``[Gitea]`` / ``[Gitea:<old-guid>]`` prefix is
    replaced rather than accumulated, so repeated sync runs do not produce
    multiple prefixes.  When *guid* is provided, the guid-stamped form is used
    as a belt-and-braces backup that lets the mapping be rebuilt if the JSON
    state file is lost.
    """
    description = existing_description or ""
    new_prefix = (
        f"[Gitea:{guid}]" if guid else GITEA_DESCRIPTION_PREFIX
    )

    match = GITEA_DESCRIPTION_RE.match(description)
    if match:
        # Strip the existing prefix (and any single space following it) so we
        # can replace it with the new one without doubling up.
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
      3. Description-stamp recovery: any TRMM script whose description carries
         a ``[Gitea:<guid>]`` prefix matching the state mapping.

    Returns ``(key, script)`` where ``key`` is the script's current
    ``(name, category)`` in TRMM, or ``(None, None)`` if no match was found.
    """
    # 1. State-file lookup.
    guid = state.get(rel_path)
    if guid and guid in guid_index:
        return guid_index[guid]

    # 2. (name, category) fallback.
    key: Tuple[str, str] = (name, category)
    if key in trmm_index:
        return key, trmm_index[key]

    # 3. Description-stamp recovery (only useful when the state file is gone
    # but TRMM still carries the [Gitea:<guid>] prefix from a prior run).
    if guid:
        for trmm_key, script in trmm_index.items():
            if _guid_from_description(script.get("description")) == guid:
                return trmm_key, script

    return None, None


def sync_script(
    gitea_script: dict,
    trmm_index: Dict[Tuple[str, str], dict],
    guid_index: Dict[str, Tuple[Tuple[str, str], dict]],
    state: Dict[str, str],
) -> str:
    """
    Create or update a single TRMM script from *gitea_script*.

    Returns ``"created"``, ``"updated"``, ``"skipped"``, or raises on error.

    Matching is guid-first (via *state*) with a ``(name, category)`` fallback
    so that pre-existing TRMM scripts are adopted on first sync.  When a match
    is found the existing TRMM record is updated in place – including its
    ``name`` and ``category`` – so renames/moves in Gitea propagate without
    losing the TRMM script ``id`` or any TRMM-managed settings.
    """
    name: str = gitea_script["name"]
    category: str = gitea_script["category"]
    shell: str = gitea_script["shell"]
    content: str = gitea_script["content"]
    rel_path: str = gitea_script["path"]

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
        new_description: str = _gitea_description(existing_description, guid)
        existing_name: str = existing.get("name") or ""
        existing_category: str = existing.get("category") or ""

        # Record the binding in the state file even when no API write is needed
        # so first-time adoption persists across runs.
        if guid:
            state[rel_path] = guid

        # Skip the PUT when every Gitea-controlled field is already up-to-date,
        # avoiding unnecessary writes to TRMM.
        if (
            existing_body == content
            and existing_description == new_description
            and existing_name == name
            and existing_category == category
        ):
            log.debug("Skipped  : %s [category=%s] (no changes)", name, category)
            return "skipped"

        # Preserve every TRMM-managed field; only replace the script body
        # (and keep name/category/shell consistent with Gitea).  Description
        # is updated to carry the [Gitea] / [Gitea:<guid>] prefix.
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

        # Keep the in-memory indexes consistent with the new name/category so
        # that subsequent lookups in the same run see the moved entry.
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
        "description": GITEA_DESCRIPTION_PREFIX,
        "args": [],
        "default_timeout": DEFAULT_TIMEOUT,
        "favorite": False,
        "hidden": False,
        "supported_platforms": DEFAULT_PLATFORMS.get(shell, ["windows"]),
        "run_as_user": False,
        "env_vars": [],
    }
    resp = _trmm_post("/scripts/", payload)

    # Record the new script in the state file (and index) using the guid TRMM
    # assigned.  The POST response shape varies between TRMM versions, so we
    # fall back to a /scripts/ refresh if the guid is missing.
    new_guid: Optional[str] = None
    try:
        body = resp.json()
        if isinstance(body, dict):
            new_guid = body.get("guid")
            new_id = body.get("id")
            if new_guid and new_id:
                # Re-stamp the description with the guid so a future state-file
                # loss can still rebuild the mapping.
                stamped_description = _gitea_description("", new_guid)
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
        # Fall back to looking the script up by (name, category) – this costs
        # one extra API call but only in the rare case where the POST response
        # did not include the guid.
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
    log.info("Local repo path: %s", os.path.abspath(GITEA_LOCAL_PATH))
    if FULL_SYNC:
        log.info("FULL_SYNC is enabled – all scripts will be synced regardless of changes")

    # ------------------------------------------------------------------
    # Step 1 – Ensure the local git clone is up to date.
    # ------------------------------------------------------------------
    try:
        is_fresh_clone, modified_paths, deleted_paths, rename_pairs = _ensure_local_repo()
    except Exception as exc:  # pylint: disable=broad-except
        log.error("Failed to prepare local Gitea repo: %s", exc)
        sys.exit(1)

    # Decide whether to do a full or incremental sync:
    #  * Always full on the first clone (all files are "new").
    #  * Always full when FULL_SYNC=true.
    #  * Incremental otherwise (only process files that changed).
    is_full_sync: bool = FULL_SYNC or is_fresh_clone

    if (
        not is_full_sync
        and not modified_paths
        and not deleted_paths
        and not rename_pairs
    ):
        log.info("No changes detected in Gitea repo – nothing to sync")
        return

    # ------------------------------------------------------------------
    # Step 2 – Load persistent path → guid state and fetch TRMM scripts.
    # ------------------------------------------------------------------
    state: Dict[str, str] = load_state()
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

    # ------------------------------------------------------------------
    # Step 3 – Apply git-detected renames first so the (name, category)
    # map in TRMM is up to date before any add/modify processing happens.
    # Renames preserve the TRMM script id and all TRMM-managed settings;
    # the rename pair's old path is removed from the state file and the
    # new path is bound to the same guid.
    # ------------------------------------------------------------------
    created = updated = skipped = deleted = renamed = errors = 0
    processed_paths: Set[str] = set()

    for old_path, new_path in rename_pairs:
        # Skip rename pairs whose new path is not a supported script (e.g.
        # README.md → CHANGELOG.md) – nothing to do in TRMM either way.
        new_parsed = _parse_script_path(new_path)
        old_parsed = _parse_script_path(old_path)

        if new_parsed is None and old_parsed is None:
            continue

        # If the *new* path is not a recognised script, treat the rename as a
        # delete of the old path so its TRMM counterpart is removed.
        if new_parsed is None:
            deleted_paths.append(old_path)
            continue

        # If the *old* path was not a recognised script, treat the rename as
        # an add of the new path; sync_script will create or adopt as needed.
        if old_parsed is None:
            modified_paths.append(new_path)
            continue

        # Real rename: collect the new file from disk and let sync_script
        # update the existing TRMM record in place.
        try:
            collected = collect_local_scripts(filter_paths={new_path})
        except OSError as exc:
            log.error("Failed to read renamed file %s: %s", new_path, exc)
            errors += 1
            continue

        if not collected:
            # File missing on disk for some reason – fall back to delete
            # of the old TRMM record so we don't leave a stale script.
            log.warning(
                "Renamed file %s could not be read – treating as a delete of %s",
                new_path,
                old_path,
            )
            deleted_paths.append(old_path)
            continue

        # Carry the previous binding forward so sync_script can find the
        # existing TRMM script via the state mapping.
        if old_path in state and new_path not in state:
            state[new_path] = state.pop(old_path)
        else:
            state.pop(old_path, None)

        gs = collected[0]
        try:
            result = sync_script(gs, trmm_index, guid_index, state)
            processed_paths.add(new_path)
            if result == "created":
                created += 1
            elif result == "skipped":
                skipped += 1
            else:
                # sync_script logs "Renamed" vs "Updated" appropriately.
                if (gs["name"], gs["category"]) != (
                    old_parsed[0],
                    old_parsed[1],
                ):
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
                "HTTP error renaming '%s' → '%s': %s%s",
                old_path,
                new_path,
                exc,
                response_detail,
            )
            errors += 1
        except Exception as exc:  # pylint: disable=broad-except
            log.error("Unexpected error renaming '%s' → '%s': %s", old_path, new_path, exc)
            errors += 1

    # ------------------------------------------------------------------
    # Step 4 – Collect scripts from the local clone (full or incremental).
    # ------------------------------------------------------------------
    if is_full_sync:
        log.info("Collecting all scripts from local Gitea clone …")
        try:
            gitea_scripts = collect_local_scripts()
        except OSError as exc:
            log.error("Failed to read local Gitea repo: %s", exc)
            sys.exit(1)
    else:
        log.info(
            "Collecting %d changed file(s) from local Gitea clone …",
            len(modified_paths),
        )
        try:
            gitea_scripts = collect_local_scripts(filter_paths=set(modified_paths))
        except OSError as exc:
            log.error("Failed to read local Gitea repo: %s", exc)
            sys.exit(1)

    log.info("  %d script(s) to process", len(gitea_scripts))

    # ------------------------------------------------------------------
    # Step 5 – Create / update scripts in TRMM.
    # ------------------------------------------------------------------
    for gs in gitea_scripts:
        if gs["path"] in processed_paths:
            # Already handled as part of a rename pair above.
            continue
        try:
            result = sync_script(gs, trmm_index, guid_index, state)
            if result == "created":
                created += 1
            elif result == "skipped":
                skipped += 1
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
    # Step 6 – Delete TRMM scripts that were removed from Gitea.
    # ------------------------------------------------------------------
    if is_full_sync:
        # Full sync: remove any TRMM script (with the [Gitea] prefix) whose
        # counterpart no longer exists anywhere in the repo.  Match by guid
        # via the state file as well as by (name, category) so we don't
        # accidentally delete a script that was just renamed in TRMM by the
        # current run.
        gitea_keys: Set[Tuple[str, str]] = {
            (gs["name"], gs["category"]) for gs in gitea_scripts
        }
        gitea_guids: Set[str] = {state[p] for p in state if state.get(p)}
        for key, script in list(trmm_index.items()):
            description = script.get("description") or ""
            if not _is_gitea_managed(description):
                continue
            if key in gitea_keys:
                continue
            if script.get("guid") and script["guid"] in gitea_guids:
                continue
            script_id = script["id"]
            name, category = key
            try:
                _trmm_delete(f"/scripts/{script_id}/")
                log.info("Deleted  : %s [category=%s]", name, category)
                deleted += 1
                # Drop any stale state entries that pointed at this guid.
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
    else:
        # Incremental sync: only delete TRMM scripts for files that git
        # explicitly reported as deleted in this pull.
        for rel_path in deleted_paths:
            # Resolve via state first (handles the case where the TRMM
            # script was previously renamed in Gitea and the (name, category)
            # no longer matches the deleted path).
            guid = state.pop(rel_path, None)
            target_key: Optional[Tuple[str, str]] = None
            target_script: Optional[dict] = None

            if guid and guid in guid_index:
                target_key, target_script = guid_index[guid]
            else:
                parsed = _parse_script_path(rel_path)
                if parsed is None:
                    continue
                name, category = parsed
                target_key = (name, category)
                target_script = trmm_index.get(target_key)

            if target_script is None or target_key is None:
                continue

            description = target_script.get("description") or ""
            if not _is_gitea_managed(description):
                continue

            script_id = target_script["id"]
            name, category = target_key
            try:
                _trmm_delete(f"/scripts/{script_id}/")
                log.info("Deleted  : %s [category=%s]", name, category)
                deleted += 1
                trmm_index.pop(target_key, None)
                if target_script.get("guid"):
                    guid_index.pop(target_script["guid"], None)
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
    # Step 7 – Persist the updated path → guid state mapping.
    # ------------------------------------------------------------------
    save_state(state)

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
