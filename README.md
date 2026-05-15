# TRMM_Gitea_Sync

A Python script that synchronises scripts stored in a private **Gitea**
repository with the script library in **Tactical RMM (TRMM)**.

---

## How It Works

1. On the **first run** `sync.py` clones the configured Gitea repository to a
   local directory (`GITEA_LOCAL_PATH`, default `./gitea_repo`).
2. On **subsequent runs** it executes `git pull` and records which files changed.
   Only the scripts whose source files were added, modified, renamed, or deleted
   in that pull are touched in TRMM – everything else is left as-is, making
   incremental syncs very fast regardless of repo size.
3. Set `FULL_SYNC=true` to force every script to be compared and re-synced
   (useful after manually editing scripts inside TRMM).
4. The **top-level folder name** becomes the TRMM **category**.
5. The **filename** (without extension) becomes the TRMM **script name**.
6. The **file extension** determines the TRMM **shell type**:

   | Extension | Shell        |
   |-----------|-------------|
   | `.ps1`    | `powershell` |
   | `.py`     | `python`     |
   | `.sh`     | `shell`      |
   | `.bat`    | `batch`      |
   | `.cmd`    | `batch`      |

5. For each Gitea script the sync script either:
   - **Creates** a new TRMM script (with sensible defaults), or
   - **Updates** the script body of the existing TRMM script.

### What Is Never Changed

- TRMM scripts that have **no counterpart in Gitea** are left untouched.
- For scripts that **already exist in TRMM**, the following TRMM-managed
  settings are always preserved:
  `args`, `supported_platforms`, `run_as_user`, `env_vars`,
  `default_timeout`, `favorite`, `hidden`.
- The `description` field of every synchronised script is prefixed with
  `[Gitea:<trmm-guid>]` (or just `[Gitea]` for legacy scripts) so that
  Gitea-managed scripts are easy to identify in TRMM.  The prefix is
  applied idempotently – repeated sync runs will not accumulate multiple
  prefixes.

---

## How Scripts Are Matched

Gitea files and TRMM scripts are matched on a **stable identifier** so that
renaming or moving a script in Gitea is reflected as a rename/move in TRMM
rather than a delete-and-recreate (which would otherwise discard the TRMM
script `id` and every preserved setting listed above).

The identifier used is TRMM's own `guid` field, which is assigned to every
script when it is first created.  No changes are required on the TRMM side –
the sync tool tracks the binding itself in two places:

1. **A small JSON state file** (`.trmm_sync_state.json`) kept next to the
   local Gitea clone.  It stores a `repo-relative-path → TRMM-guid` map
   that survives across runs.  The file is rewritten atomically at the end
   of every successful sync.
2. **The TRMM `description` field**, which carries a `[Gitea:<guid>]`
   prefix as a belt-and-braces backup.  If the JSON state file is lost or
   corrupted, the binding is re-derived from the description on the next run.

### Matching algorithm

For every Gitea script, the sync resolves the matching TRMM script in this
order:

1. **By `guid`** via the state file (`path → guid` → TRMM script).
2. **By `(name, category)`** against TRMM (used for first-time *adoption* of
   pre-existing TRMM scripts that have not yet been bound to a guid).
3. **By the `[Gitea:<guid>]` description stamp** (used to recover the
   binding when the state file is missing).
4. If no match is found, a new TRMM script is created and the resulting
   guid is recorded in the state file.

### Renames and moves

* For `sync.py` (Gitea), the script asks `git diff --name-status -M50%` to
  detect renames between the previous and current `HEAD`.  Each rename
  pair is processed by updating the existing TRMM script's `name` and/or
  `category` in place, preserving its `id` and all TRMM-managed settings.
* For `sync_github.py` (GitHub), there is no git history to inspect.
  Renames are inferred by matching identical blob SHAs across runs:
  if a path that was present in the previous run disappears and a new
  path with the same blob SHA appears, the binding is carried forward to
  the new path.

In both cases the **state file is updated** so future runs see the new
path bound to the same guid.

### State file location

By default the state file lives **next to** `GITEA_LOCAL_PATH` (i.e. in
its parent directory) so that wiping the local clone with `rm -rf` does
not also delete the mapping.  Override the location via the
`SYNC_STATE_FILE` environment variable.

### What if the state file is lost?

The next run rebuilds it from the `[Gitea:<guid>]` (or `[GitHub:<guid>]`)
description stamps, falling back to `(name, category)` adoption for any
scripts that pre-date the stamping.  No data is lost; you may just see a
batch of "Updated" log lines as the descriptions are re-stamped.

---

## Prerequisites

- Python 3.8 or newer
- Access to both the TRMM REST API and the Gitea API

## Installation

```bash
pip install -r requirements.txt
```

---

## Configuration

All configuration is supplied via **environment variables**.

| Variable           | Required | Description                                                  |
|--------------------|----------|--------------------------------------------------------------|
| `TRMM_API_URL`     | ✅       | Base URL of the TRMM instance, e.g. `https://rmm.example.com` |
| `TRMM_API_KEY`     | ✅       | Tactical RMM API key                                         |
| `GITEA_URL`        | ✅       | Base URL of the Gitea instance, e.g. `https://git.example.com` |
| `GITEA_TOKEN`      | ✅       | Gitea personal access token (required for private repos)     |
| `GITEA_OWNER`      | ✅       | Gitea repository owner (user or organisation name)           |
| `GITEA_REPO`       | ✅       | Gitea repository name                                        |
| `GITEA_BRANCH`     | ❌       | Branch to sync from (default: `main`)                        |
| `GITEA_LOCAL_PATH` | ❌       | Path for the local git clone (default: `./gitea_repo`). The repo is cloned automatically on first run; subsequent runs do `git pull` and only sync changed files. |
| `FULL_SYNC`        | ❌       | Set to `true`, `1`, or `yes` to sync all scripts on every run instead of only changed ones (default: `false`). |
| `IGNORE_SSL`       | ❌       | Set to `true`, `1`, or `yes` to disable SSL certificate verification for all API calls. Useful when running on the TRMM server where the API hostname resolves to `127.0.0.1` and the TLS certificate CN does not match (default: `false`) |
| `SYNC_STATE_FILE`  | ❌       | Path to the persistent JSON state file that stores the `repo-relative-path → TRMM-guid` mapping (used to track renames/moves). Defaults to a sibling of `GITEA_LOCAL_PATH` (e.g. `./.trmm_sync_state.json`). For `sync_github.py`, defaults to `./.trmm_github_sync_state.json` in the working directory. |

---

## Usage

```bash
export TRMM_API_URL="https://rmm.example.com"
export TRMM_API_KEY="your-trmm-api-key"
export GITEA_URL="https://gitea.example.com"
export GITEA_TOKEN="your-gitea-token"
export GITEA_OWNER="myorg"
export GITEA_REPO="rmm-scripts"

python sync.py
```

### Example Gitea Repository Layout

```
rmm-scripts/
├── Checks/
│   ├── Check CPU Age.ps1
│   └── Check Disk Space.ps1
├── Maintenance/
│   ├── Clear Temp Files.ps1
│   └── Restart Service.py
└── Linux/
    └── disk_report.sh
```

This would create/update TRMM scripts with:

| Name              | Category    | Shell        |
|-------------------|-------------|--------------|
| Check CPU Age     | Checks      | powershell   |
| Check Disk Space  | Checks      | powershell   |
| Clear Temp Files  | Maintenance | powershell   |
| Restart Service   | Maintenance | python       |
| disk_report       | Linux       | shell        |

---

## Running as a Scheduled Task in TRMM

You can run `sync.py` as a recurring TRMM task or as a cron job on the
TRMM server itself so that script changes pushed to Gitea are
automatically propagated to TRMM.

---

## License

See [LICENSE](LICENSE).
