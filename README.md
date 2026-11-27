# TBC Raster Dedup

Tools for working with Trimble Business Center (TBC) `.vce` project files outside of TBC — specifically for deduplicating and consolidating large raster files (TIFFs, ECWs, etc.) that accumulate as copies across multiple projects.

## Background

TBC stores project data in `.vce` files, which are ZIP archives containing a binary `ObjectData` blob. Referenced file paths (rasters, CSVs, alignments, etc.) are stored as plain UTF-8 strings with VLQ-encoded length prefixes, making them patchable without TBC.

Each project tends to copy large raster imagery into its own subfolder, which quickly fills a OneDrive sync with hundreds of GB of duplicates. These tools find those duplicates, move one canonical copy to a shared library folder, and update all VCE references to point there.

## Setup

1. Copy `tbc_tools.example.toml` to `tbc_tools.toml` and edit for your environment (usernames, drive mappings, library path).
2. `tbc_tools.toml` is gitignored — it can safely contain real paths.
3. (Optional) Copy `run_consolidation.example.bat` to `run_consolidation.bat` and edit `SCAN_DIRS` to run the whole scan → plan → preview → move/patch workflow in one go. It pauses for you to review the report before changing anything, and leaves the destructive delete step manual. The working copy is gitignored.

## Commands

Everything runs through a single script: `tbc_tools.py`.

### `list-paths` — inspect VCE references

List all file paths embedded in VCE files (defaults to `vce_roots` from config):
```
uv run tbc_tools.py list-paths
uv run tbc_tools.py list-paths "C:\path\to\project.vce"
```

### `verify` — check for broken references

Read-only scan of VCE files, checking whether each referenced path exists on disk:
```
uv run tbc_tools.py verify
uv run tbc_tools.py verify "C:\path\to\projects"
```

Paths are classified as `OK`, `FIXABLE` (can be fixed by `normalize`), `MISSING`, `TEMP` (expected TBC temp paths), or `LOG?` (import-log timestamps, not real files).

### `normalize` — fix cross-user path prefixes

Apply path-prefix mappings from config to rewrite VCE files. Useful when multiple users share a OneDrive folder mapped as `S:\` but each user's local sync path differs:
```
uv run tbc_tools.py normalize --dry-run
uv run tbc_tools.py normalize
uv run tbc_tools.py normalize --cleanup-rasters   # also delete orphaned local raster copies
```

### `patch` — replace a single path

Replace one specific path string in a VCE file:
```
uv run tbc_tools.py patch project.vce "C:\old\path\file.tif" "C:\new\path\file.tif" --in-place
uv run tbc_tools.py patch src.vce dst.vce "C:\old\path\file.tif" "C:\new\path\file.tif"
```

### `scan` → `plan` → `execute` — bulk deduplication & consolidation

This is the main workflow for cleaning up raster sprawl.

**Step 1 — scan** for duplicate rasters across one or more directories:
```
# Find duplicates within a single directory
uv run tbc_tools.py scan "S:\Projects" --min-mb 50 --out dupes.json

# Find duplicates across multiple directories (e.g. local Documents vs shared library)
uv run tbc_tools.py scan "C:\Users\you\OneDrive\Documents" "S:\Basemaps" --out dupes.json

# Consolidate mode: include ALL rasters, not just duplicates, so the plan
# can relocate everything to the library even if no duplicate exists yet
uv run tbc_tools.py scan "C:\Users\you\OneDrive\Documents" "S:\Basemaps" --consolidate --out dupes.json
```

**Step 2 — plan** where canonical copies should live:
```
uv run tbc_tools.py plan dupes.json --out plan.json

# Override library location (otherwise uses raster_library from config)
uv run tbc_tools.py plan dupes.json --library "S:\Basemaps" --out plan.json

# Specify where to search for VCE files to patch (otherwise uses vce_roots from config)
uv run tbc_tools.py plan dupes.json --vce-paths "C:\Users\you\OneDrive\Documents" --out plan.json

# Adopt an existing same-name library file as canonical even if its content
# hash differs (see "Merging same-name files" below)
uv run tbc_tools.py plan dupes.json --merge-by-name --out plan.json
```

#### Merging same-name files (`--merge-by-name`)

By default, files are deduplicated strictly by content hash — only byte-identical
copies are ever collapsed. Sometimes, though, the library already holds a file with
the **same name but slightly different bytes** than the working copies: typically a
re-exported or re-compressed version of the same raster (same imagery, different
internal tiling/compression/overviews). Without `--merge-by-name` the planner can't
consolidate these — it would leave an orphaned working copy behind and the duplicates
would never clean up.

With `--merge-by-name`, when a duplicate group has no copy in the library but a file
of the **same name** already exists there, the planner adopts that library file as the
canonical target: every VCE is repointed to it and all the working copies become
reclaimable.

> **Verify georeferencing first.** Repointing projects to a different-bytes file is only
> safe if the two files cover the same ground. For GeoTIFFs/ECWs that means identical
> pixel dimensions, pixel scale, and tie-point (bounding box) — if those match, the
> imagery lands in exactly the same place regardless of compression. Confirm this before
> using `--merge-by-name` on imagery you can't easily regenerate.

**Step 3 — dry run** to review what will happen:
```
uv run tbc_tools.py execute plan.json --dry-run
```

**Step 4 — execute** (move files, patch VCEs, optionally delete originals):
```
uv run tbc_tools.py execute plan.json
uv run tbc_tools.py execute plan.json --delete-originals
```

Deletion is opt-in — you can move and patch first, verify TBC opens the projects correctly, then delete later. Each VCE is backed up to `<name>.vce.bak` before it is rewritten.

### `report` — Markdown preview of a plan

Render a human-readable preview of a plan without changing anything:
```
uv run tbc_tools.py report plan.json --out report.md
```
The report includes estimated reclaimable space, the list of locked (skipped) projects, the files to move and delete (with sizes), and every per-VCE path rewrite — each tagged `raster`/`normalize` and flagged for whether the new target resolves on disk (so any regression stands out). `report` and `execute` share the same change-computation logic, so the preview matches what `execute` will do.

### Audit log — a record of every change

Every command that can change files on disk (`execute`, `prune-subfolders`, `normalize`, `find-orphans`, `patch`, `clear-locks`) appends a JSON line per action to an **append-only audit log**, so "what happened to this file?" always has a definitive, greppable answer. Read-only commands (`scan`, `plan`, `report`, `verify`, `list-paths`) never write to it.

Each run is bracketed by `run_start`/`run_end` records (with the full command line), and every action in between is one object with an ISO-8601 timestamp:

```jsonl
{"ts":"2026-07-15T09:12:03","run":"a1b2c3d4","event":"run_start","command":"execute","argv":["execute","plan.json","--delete-originals"],"dry_run":false,"library":"S:\\Basemaps"}
{"ts":"2026-07-15T09:12:04","run":"a1b2c3d4","event":"move","src":"S:\\Projects\\proj\\ortho.tiff","dst":"S:\\Basemaps\\ortho.tiff","size":734003200,"dry_run":false}
{"ts":"2026-07-15T09:12:06","run":"a1b2c3d4","event":"delete","path":"S:\\Asbuilts\\...\\points.laz","size":670380032,"surviving_copy":"S:\\Basemaps\\points.laz","reason":"dedup-duplicate","dry_run":false}
```

Deletions record **where the identical data survives** (`surviving_copy`) for dedup, or the `referenced` flag for age-based prune — so the log proves whether a removed file was truly redundant. Dry runs are logged too, flagged `"dry_run":true`.

**Location:** `tbc_tools.audit.jsonl` beside the script by default; set `log_file` in config (e.g. to a shared path on the library so the whole team writes one trail) or override per-run with `--log FILE`.

Find every real deletion of a file, across all runs:
```
uv run python -c "import json,sys; [print(r['ts'],r.get('reason'),r['path']) for r in map(json.loads,open('tbc_tools.audit.jsonl')) if r.get('event')=='delete' and not r.get('dry_run')]"
```

### Locked projects are skipped automatically

While a project is open in TBC it has a sibling lock file (`<name>.vce,lk`). `plan` and `execute` detect these and never move, delete, or patch anything an open project relies on — including rasters that physically live under the open project's folder. The lock is re-checked at the last possible moment, immediately before any VCE is overwritten, so a project opened mid-run is never clobbered. Deferred files show up in the `report` under "Reclaimable once locked projects close"; close the project (releasing the lock) to include them.

### `clear-locks` — remove orphaned (stale) lock files

If TBC crashes or is force-killed, it can leave a `<name>.vce,lk` lock behind, making the tool treat a project as open forever. Lock files at least `stale_lock_days` old (default **7**, set in config) are considered stale and can be removed:
```
uv run tbc_tools.py clear-locks                 # clear stale locks under vce_roots
uv run tbc_tools.py clear-locks --dry-run       # show what would be cleared
uv run tbc_tools.py clear-locks --days 14       # override the threshold
uv run tbc_tools.py clear-locks "S:\path"       # limit to specific paths
```
Recent locks (newer than the threshold) are always kept, since they likely belong to a project that is genuinely open. The threshold is controllable via the `stale_lock_days` config key or the `--days` flag.

You can also fold stale-lock clearing into a consolidation run with `--clear-stale-locks` (threshold via `--stale-lock-days`, else config):
```
uv run tbc_tools.py plan dupes.json --clear-stale-locks --out plan.json
uv run tbc_tools.py execute plan.json --clear-stale-locks
```

> **Heads up:** clearing a lock that *is* genuinely in use would let two TBC sessions edit the same project. The age threshold makes this safe in practice (a week-old lock is almost certainly orphaned), but if in doubt, confirm nobody has the project open first.

### `prune-subfolders` — delete old rasters from project subfolders

Reclaim space by deleting rasters that have accumulated in project working subfolders (`Name\` next to `Name.vce`) and are older than a cutoff age. Defaults to **`.tif`/`.tiff` only** and **older than 60 days (~2 months)**, and **never touches the raster library** (`raster_library` from config).

```
# Preview only (read-only, the default). Writes prune.json.
uv run tbc_tools.py prune-subfolders

# Age by the date embedded in the filename instead of the filesystem mtime
uv run tbc_tools.py prune-subfolders --date-from name

# Older than a different age, or other raster types
uv run tbc_tools.py prune-subfolders --older-than-days 90
uv run tbc_tools.py prune-subfolders --ext .tif .tiff .ecw

# Limit to specific project roots (else uses vce_roots from config)
uv run tbc_tools.py prune-subfolders --vce-paths "S:\Projects"

# Actually delete (dry-run first is recommended)
uv run tbc_tools.py prune-subfolders --date-from name --delete --dry-run
uv run tbc_tools.py prune-subfolders --date-from name --delete
```

Unlike `find-orphans`, this prunes purely by **age and location** — it does *not* require a file to be unreferenced. Files still pointed at by a VCE are listed with `[STILL REFERENCED]` and counted in a warning, because deleting them will break those projects unless you have already consolidated/repointed them to the library (see `scan` → `plan` → `execute`). The configured library is protected by default; override the protected directory with `--library "S:\...path"`, or pass `--library ""` to disable the guard entirely. Files an **open** TBC project relies on are always skipped at deletion time (same lock safety as the other commands).

#### Choosing the age basis (`--date-from`)

- `--date-from mtime` (default): the filesystem modified time. Simple, but **OneDrive re-sync can reset mtimes**, so a freshly re-synced folder may look "new."
- `--date-from name`: a date parsed out of the filename — the reliable signal when files are named after their survey/flight date. Recognized forms include month-name (`Jun_18,_2026`, `June 18 2026`), ISO (`2026-06-24`), compact (`20260624`), and US-numeric (`6-24-2026`). Files with **no recognizable date are reported separately and never deleted**, so nothing is removed on a guess.

The report prints the effective date and age of each file (and, in `name` mode, the list of undated/skipped files). Run the read-only preview first and eyeball the dates before `--delete`.

## Typical workflows

### Deduplication (same file in multiple project subfolders)

When the same raster exists in multiple TBC project subfolders:

```bash
uv run tbc_tools.py scan "S:\Projects" --out dupes.json
uv run tbc_tools.py plan dupes.json --out plan.json
uv run tbc_tools.py execute plan.json --dry-run
uv run tbc_tools.py execute plan.json --delete-originals
```

### Consolidation (move all rasters to the shared library)

When rasters are scattered in local OneDrive project folders and should be centralized on `S:\`:

```bash
uv run tbc_tools.py scan "C:\Users\you\OneDrive\Documents" "S:\Basemaps" --consolidate --out dupes.json
uv run tbc_tools.py plan dupes.json --library "S:\Basemaps" --vce-paths "C:\Users\you\OneDrive\Documents" --out plan.json
uv run tbc_tools.py execute plan.json --dry-run
uv run tbc_tools.py execute plan.json --delete-originals
```

### Normalize cross-user references

When VCE files contain another user's local OneDrive path instead of the shared `S:\`:

```bash
uv run tbc_tools.py normalize --dry-run
uv run tbc_tools.py normalize
```

## How VCE patching works

VCE files are ZIP archives. The `ObjectData` entry is a proprietary binary blob using a sequential, length-delimited encoding. Strings are stored as:

```
[VLQ-encoded byte length] [UTF-8 string bytes] [0x4D 0x46 marker]
```

`tbc_tools.py` searches for the VLQ + string byte sequence, replaces it with the new VLQ + new string, and rezips with maximum compression (level 9). Because the format is length-delimited (no absolute offsets), changing string lengths is safe. TBC validates only standard ZIP CRC32, which Python's `zipfile` recomputes correctly on write.

## Known limitations

- **Windows Controlled Folder Access**: If your IT department has enabled ransomware protection, scripts cannot move/delete files in protected directories like `Documents` or `OneDrive`. You'll need to do those file operations manually via File Explorer, or add Python to the allowed apps list.
- **OneDrive cloud-only files**: Files that are "cloud only" (not synced locally) will fail to hash. Make sure target directories are fully synced before scanning.

## Requirements

- Python via `uv` (no extra dependencies — stdlib only)
- TBC ≥ some version that uses the ZIP-based VCE format (verified on v5.90+)
