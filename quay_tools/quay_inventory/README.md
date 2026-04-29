# Quay Repository Inventory

Generates a CSV inventory of all Quay repositories across your user namespace and organizations, including tag details, image deduplication data, stale tag counts, repo size, permissions, and push/pull history — useful for identifying images that can be cleaned up.

## Prerequisites

- Python 3.x
- `requests` library (`pip install requests`)
- A Quay superuser or OAuth application token with sufficient permissions

---

## Step 1: Create a Quay OAuth Token

1. Log in to your Quay instance
2. Click your username in the top-right corner → **Account Settings**
3. Select the **Docker CLI Password** or navigate to **Applications** in the left sidebar
4. Under **OAuth Applications**, click **Create New Application**
5. Give it a name (e.g. `inventory-script`) and click **Create Application**
6. Click on the application, then select **Generate Token**
7. Grant the following permissions:
   - `Administer Organization`
   - `Administer Repositories`
   - `View all visible repositories`
   - `Read/Write to any accessible repositories`
   - `Read User Information`
8. Click **Authorize Application** and copy the generated token

---

## Step 2: Set Environment Variables

Set your Quay hostname and token as environment variables so the script does not prompt for them on each run:

```bash
export QUAY_HOSTNAME=quay-server.example.com
export ACCESS_TOKEN=your-token-here
```

If the variables are not set, the script will prompt for them interactively at runtime.

---

## Step 3: Run the Script

```bash
python3 quay_inventory.py
```

The script will generate a file called `quay_repository_inventory.csv` in the current directory.

---

## Options

| Flag | Default | Description |
|---|---|---|
| `--stale-days DAYS` | `180` | Number of days since last push after which a tag is considered stale |
| `--resume CSV_FILE` | | Resume from an existing inventory CSV: skip repositories already in the file and append new rows |
| `-h`, `--help` | | Show help message and exit |

### Examples

```bash
# Run with defaults (stale threshold = 180 days / 6 months)
python3 quay_inventory.py

# Flag tags that haven't been updated in 30 days
python3 quay_inventory.py --stale-days 30

# Flag tags that haven't been updated in a year
python3 quay_inventory.py --stale-days 365

# Resume a previous run that timed out partway through
python3 quay_inventory.py --resume quay_repository_inventory.csv
```

---

## Resuming after a timeout

Large Quay instances sometimes time out partway through an inventory run. To make this recoverable, the script writes each repository's row to the CSV as soon as it is processed (rather than buffering everything until the end). If a run dies, the rows already written are preserved on disk.

To pick up where it left off, re-run with `--resume` pointing at the existing CSV:

```bash
python3 quay_inventory.py --resume quay_repository_inventory.csv
```

When `--resume` is used:

- The script reads the existing CSV, builds the set of `(Namespace, Repository)` rows already present, and skips them when iterating.
- New rows are appended to the same file — the existing data is not rewritten.
- The header from the existing file is reused. If the original run used a different `--stale-days` value than the one passed on resume, the script keeps the original threshold so the `Stale Tags (>Nd)` column stays consistent and prints a warning.
- If the path passed to `--resume` does not exist yet, the script falls back to creating a fresh CSV at that path.

You can re-run with `--resume` as many times as needed until the inventory is complete.

---

## CSV Output

The generated CSV includes the following columns:

| Column | Description |
|---|---|
| `Namespace` | User or organization the repository belongs to |
| `Repository` | Repository name |
| `Repo Size` | Total size of the repository (e.g. `1.2 GB`) |
| `Tag Count` | Total number of tags |
| `Unique Image Count` | Number of distinct image digests (tags sharing a digest are the same image) |
| `Stale Tags (>Nd)` | Count of tags not updated within the stale threshold |
| `Duplicate Tags` | Tags that point to the same image digest — safe candidates for pruning |
| `Latest Push Timestamp` | Most recent tag push across the repository |
| `Tags` | All tags with their last modified date and age |
| `Push History` | Recent push activity per user (requires database logs model) |
| `Pull History` | Recent pull activity per user (requires database logs model) |
| `Users with Permissions` | All users with access, their role, and whether it is direct or via a team |

> **Note:** Push/pull history requires Quay's `LOGS_MODEL` to be set to `database`. If Quay is configured to use Splunk as the logs backend, these columns will be empty.
