# TRMM_Gitea_Sync

A Python script that synchronises scripts stored in a private **Gitea**
repository with the script library in **Tactical RMM (TRMM)**.

---

## How It Works

1. The script reads every file inside the top-level directories of the
   configured Gitea repository.
2. The **top-level folder name** becomes the TRMM **category**.
3. The **filename** (without extension) becomes the TRMM **script name**.
4. The **file extension** determines the TRMM **shell type**:

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
  `[Gitea]` so that Gitea-managed scripts are easy to identify in TRMM.
  The prefix is applied idempotently – repeated sync runs will not
  accumulate multiple `[Gitea]` tags.

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

| Variable       | Required | Description                                                  |
|----------------|----------|--------------------------------------------------------------|
| `TRMM_API_URL` | ✅       | Base URL of the TRMM instance, e.g. `https://rmm.example.com` |
| `TRMM_API_KEY` | ✅       | Tactical RMM API key                                         |
| `GITEA_URL`    | ✅       | Base URL of the Gitea instance, e.g. `https://git.example.com` |
| `GITEA_TOKEN`  | ✅       | Gitea personal access token (required for private repos)     |
| `GITEA_OWNER`  | ✅       | Gitea repository owner (user or organisation name)           |
| `GITEA_REPO`   | ✅       | Gitea repository name                                        |
| `GITEA_BRANCH` | ❌       | Branch to sync from (default: `main`)                        |

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
