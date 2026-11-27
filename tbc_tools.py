"""tbc_tools.py — utilities for Trimble Business Center (.vce) project files.

Commands
--------
  list-paths    List all file-path strings embedded in one or more VCE files.
  verify        Check whether paths referenced in VCE files exist on disk.
  normalize     Apply path-prefix mappings from config to VCE files (fixes
                broken cross-user / cross-machine references).
  patch         Replace a single specific path in one VCE file.
  scan          Find duplicate large raster files across one or more directories.
                With --consolidate, includes ALL rasters (not just duplicates)
                so the plan step can relocate everything to the library.
  plan          Generate a JSON consolidation plan from scan results.
  execute       Carry out a plan: move canonical rasters, patch VCEs, delete dupes.
  report        Write a Markdown preview of a plan (savings, locked files, etc.).
  clear-locks   Delete orphaned TBC lock files older than the stale threshold.
  find-orphans  Find rasters that are no longer referenced by any VCE: files
                inside a project's own working subfolder (Name\\) or inside the
                raster library that zero projects point to. Use --delete to
                remove them (--dry-run is the default safe mode).
  prune-subfolders
                Delete rasters inside project working subfolders (Name\\) whose
                mtime is older than --older-than-days (default 60, ~2 months).
                Defaults to .tif/.tiff only and NEVER touches the raster
                library. Unlike find-orphans this ignores references, so it also
                warns which files are still referenced. --dry-run is the default
                safe mode; pass --delete to actually remove.

Lock files
----------
While a project is open, TBC keeps a sibling 'Name.vce,lk' lock file. These
tools never move, delete, or patch a locked project, and re-check the lock at
the last possible moment before any VCE is overwritten. A lock left by a
crashed session is "stale": clear-locks (or --clear-stale-locks on plan/execute)
removes locks older than stale_lock_days (default 7) so those projects can be
processed.

Background
----------
VCE files are ZIP archives. File paths are stored in the binary ObjectData
entry as UTF-8 strings prefixed with a VLQ-encoded byte length and followed
by a 0x4D 0x46 marker. The format is purely length-delimited (no absolute
offsets), so strings of any length can be replaced safely.

Configuration
-------------
Create a tbc_tools.toml (see the bundled template) and pass it with --config,
or place it alongside this script (default). All command-line options override
the config file.

Usage examples
--------------
  # List all paths in every VCE under S:\\Projects
  uv run tbc_tools.py list-paths

  # Dry-run: show what normalize would change
  uv run tbc_tools.py normalize --dry-run

  # Actually normalize (rewrites VCEs in place)
  uv run tbc_tools.py normalize

  # Find duplicate rasters >= 100 MB under a specific directory
  uv run tbc_tools.py scan S:\\Output --min-mb 100 --out dupes.json

  # Scan multiple directories to find duplicates across locations
  uv run tbc_tools.py scan "C:\\Users\\you\\Documents" "S:\\Basemaps" --out dupes.json

  # Consolidate: include ALL rasters so they can be relocated to the library
  uv run tbc_tools.py scan "C:\\Users\\you\\Documents" "S:\\Basemaps" --consolidate --out dupes.json

  # Generate a consolidation plan
  uv run tbc_tools.py plan dupes.json --out plan.json

  # Dry-run the plan
  uv run tbc_tools.py execute plan.json --dry-run

  # Execute: move files, patch VCEs, delete originals
  uv run tbc_tools.py execute plan.json --delete-originals

WARNING: Always keep backups. VCE patching is reverse-engineered and
         unsupported by Trimble.
"""

from __future__ import annotations

import argparse
import builtins
import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# tomllib is stdlib from Python 3.11; fall back to third-party tomli if needed.
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

DEFAULT_CONFIG = Path(__file__).with_name('tbc_tools.toml')


def print(*args, **kwargs):  # noqa: A001 - intentionally shadow the builtin
    """Module-wide replacement for the builtin print: prefixes every line with
    an [HH:MM:SS] timestamp and flushes immediately, so redirected log files
    show live, time-stamped progress during long scans/patches. Leading
    newlines are emitted before the stamp so blank-line separators stay clean.
    Pass ts=False to print a line without a timestamp."""
    if not kwargs.pop('ts', True):
        return builtins.print(*args, **kwargs)
    kwargs.setdefault('flush', True)
    stamp = f'[{datetime.now():%H:%M:%S}]'
    if args and isinstance(args[0], str):
        first = args[0]
        lead = len(first) - len(first.lstrip('\n'))
        args = (first[:lead] + stamp + ' ' + first[lead:],) + args[1:]
    else:
        args = (stamp, *args)
    builtins.print(*args, **kwargs)

# ── Audit log ─────────────────────────────────────────────────────────────────
#
# An append-only JSONL trail of every mutating action: file moves, deletions
# (with the surviving library copy noted when known), VCE rewrites, backups and
# lock removals. One JSON object per line, each stamped with a local ISO-8601
# time and a short run id, so a later "what happened to this file?" has a
# definitive, greppable answer. Writing never raises into the caller — a failed
# log write warns once and the operation in progress continues regardless.


class AuditLog:
    def __init__(self, path: Path, command: str, argv: list[str], dry: bool,
                 extra: dict | None = None):
        self.path = path
        self.run_id = uuid.uuid4().hex[:8]
        self.command = command
        self.dry = dry
        self._warned = False
        rec = {'event': 'run_start', 'command': command, 'argv': argv,
               'dry_run': dry, 'cwd': os.getcwd()}
        if extra:
            rec.update({k: v for k, v in extra.items() if v is not None})
        self._write(rec)

    def _write(self, rec: dict) -> None:
        line = {'ts': datetime.now().isoformat(timespec='seconds'),
                'run': self.run_id, **rec}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(line, ensure_ascii=False) + '\n')
        except OSError as e:
            if not self._warned:
                builtins.print(f'Warning: could not write audit log '
                               f'{self.path}: {e}', file=sys.stderr)
                self._warned = True

    def record(self, event: str, **fields) -> None:
        fields.setdefault('dry_run', self.dry)
        self._write({'event': event, **{k: v for k, v in fields.items()
                                        if v is not None}})

    def end(self, **summary) -> None:
        self._write({'event': 'run_end', 'command': self.command, **summary})


_AUDIT: AuditLog | None = None


def _audit(event: str, **fields) -> None:
    """Record one mutating action to the active audit log (no-op if logging is
    off, e.g. for read-only commands)."""
    if _AUDIT is not None:
        _AUDIT.record(event, **fields)


def _audit_end(**summary) -> None:
    if _AUDIT is not None:
        _AUDIT.end(**summary)


def _safe_size(p: Path) -> int | None:
    try:
        return p.stat().st_size
    except OSError:
        return None


# ── VLQ helpers ───────────────────────────────────────────────────────────────

def encode_vlq(n: int) -> bytes:
    buf = bytearray()
    while n > 127:
        buf.append((n & 0x7F) | 0x80)
        n >>= 7
    buf.append(n)
    return bytes(buf)


def decode_vlq(data: bytes, offset: int) -> tuple[int, int]:
    """Return (value, bytes_consumed)."""
    value, shift, consumed = 0, 0, 0
    while True:
        b = data[offset + consumed]
        consumed += 1
        value |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return value, consumed


# ── VCE path extraction / patching ────────────────────────────────────────────

_PATH_RE = re.compile(rb'([A-Za-z]:\\|\\\\)([^\x00-\x1f\x7f]{1,500}?)(?=\x4D\x46)')


def _read_object_data(vce: Path) -> bytes:
    with zipfile.ZipFile(vce) as z:
        return z.read('ObjectData')


def _find_paths(data: bytes) -> list[tuple[int, str]]:
    """Return (vlq_offset, path_str) for every verified path in ObjectData."""
    results = []
    for m in _PATH_RE.finditer(data):
        path_bytes = m.group(0)
        path_start = m.start()
        for vlq_size in (1, 2, 3):
            vlq_pos = path_start - vlq_size
            if vlq_pos < 0:
                continue
            try:
                val, consumed = decode_vlq(data, vlq_pos)
            except IndexError:
                continue
            if consumed == vlq_size and val == len(path_bytes):
                try:
                    results.append((vlq_pos, path_bytes.decode('utf-8')))
                except UnicodeDecodeError:
                    pass
                break
    return results


def _apply_replacements(data: bytes, replacements: dict[str, str]) -> tuple[bytes, dict[str, int]]:
    """Apply {old_path: new_path} replacements; return (patched_data, counts)."""
    counts: dict[str, int] = {}
    for old, new in replacements.items():
        old_b = old.encode('utf-8')
        new_b = new.encode('utf-8')
        pattern = encode_vlq(len(old_b)) + old_b
        replacement = encode_vlq(len(new_b)) + new_b
        n = data.count(pattern)
        if n:
            data = data.replace(pattern, replacement)
            counts[old] = n
    return data, counts


def _plan_vce_replacements(data: bytes, raster_repl: dict[str, str],
                           path_mappings: list[PathMapping],
                           ) -> dict[str, tuple[str, str]]:
    """Compute the path replacements `execute` would apply to one VCE's
    ObjectData. Returns an ordered {old: (new, category)} mapping where
    category is 'raster' (a duplicate repointed to the library canonical) or
    'normalize' (a config path-prefix rewrite). Raster repoints take
    precedence; prefix normalization fills in the rest. Used by both `execute`
    (to patch) and `report` (to preview), so the two never drift apart."""
    repl: dict[str, tuple[str, str]] = {}
    for old, new in raster_repl.items():
        if old != new and old.encode('utf-8') in data:
            repl[old] = (new, 'raster')
    for _, p in _find_paths(data):
        if p in repl:
            continue
        new_p = p
        for m in path_mappings:
            if p.lower().startswith(m.old.lower()):
                new_p = m.new + p[len(m.old):]
                break
        if new_p != p:
            repl[p] = (new_p, 'normalize')
    return repl


class VceLockedError(Exception):
    """Raised when a VCE became locked (opened in TBC) just before we would
    have overwritten it. The prepared change is discarded and the project on
    disk is left untouched."""

    def __init__(self, vce: Path):
        super().__init__(f'{vce.name} is locked (open in TBC)')
        self.vce = vce


def _rewrite_vce(src: Path, dst: Path, new_object_data: bytes) -> None:
    """Write a new VCE at dst with ObjectData replaced; all other entries
    unchanged.

    The destination's lock file is re-checked at the last possible moment —
    immediately before the atomic replace, after the (potentially slow) temp
    file has been built — so a project opened in TBC while we were preparing
    the new file is never clobbered. If it became locked, the temp file is
    discarded and VceLockedError is raised."""
    tmp = dst.with_suffix('.vce.patching')
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, 'w') as zout:
        for info in zin.infolist():
            if info.filename == 'ObjectData':
                zout.writestr('ObjectData', new_object_data,
                              compress_type=zipfile.ZIP_DEFLATED,
                              compresslevel=9)
            else:
                # Copy raw compressed bytes to preserve original compression
                # exactly, avoiding size drift from re-compression.
                raw = zin.read(info.filename)
                zout.writestr(info, raw, compress_type=info.compress_type)
    # Last-possible-moment guard: if TBC opened the project while we were
    # building the temp file, abort rather than overwrite the live project.
    if _is_locked(dst):
        tmp.unlink(missing_ok=True)
        raise VceLockedError(dst)
    tmp.replace(dst)


def _find_vces(roots: list[Path]) -> list[Path]:
    vces = []
    for root in roots:
        if not root.exists():
            print(f'  Warning: path does not exist: {root}', file=sys.stderr)
            continue
        if root.is_file() and root.suffix.lower() == '.vce':
            vces.append(root)
        elif root.is_dir():
            vces.extend(root.rglob('*.vce'))
    return sorted(set(vces))


def _lock_path(vce: Path) -> Path:
    """The sibling lock file TBC keeps next to an open project: 'Name.vce,lk'."""
    return vce.with_name(vce.name + ',lk')


def _is_locked(vce: Path) -> bool:
    """True if TBC has the project open (its 'Name.vce,lk' lock file exists)."""
    return _lock_path(vce).exists()


def _lock_age_days(vce: Path) -> float | None:
    """Age of the VCE's lock file in days, or None if it isn't locked."""
    try:
        mtime = _lock_path(vce).stat().st_mtime
    except OSError:
        return None
    return (time.time() - mtime) / 86400.0


def _locked_protection(vces: list[Path]):
    """Scan `vces` for ones currently locked (open in TBC) and return a
    `(_protected, _owner_of)` pair of predicates over path strings, true for
    any path an open project relies on — either because it physically lives
    under the project's working folder ('<root>\\Name.vce' <-> '<root>\\Name\\')
    or because the project's own VCE references that exact path. Always
    re-scans live lock state, so callers get an up-to-the-moment answer
    rather than one computed earlier (e.g. at plan time)."""
    locked_dirs: list[tuple[str, str]] = []
    locked_refs: list[tuple[set[str], str]] = []
    in_use: set[str] = set()
    for v in vces:
        if not _is_locked(v):
            continue
        locked_dirs.append((str(v.parent / v.stem).lower() + '\\', v.name))
        refs: set[str] = set()
        try:
            for _, p in _find_paths(_read_object_data(v)):
                refs.add(p.lower())
        except Exception:
            pass
        locked_refs.append((refs, v.name))
        in_use |= refs

    def _protected(path: str) -> bool:
        pl = path.lower()
        return pl in in_use or any(pl.startswith(d) for d, _ in locked_dirs)

    def _owner_of(path: str) -> str:
        pl = path.lower()
        for d, name in locked_dirs:
            if pl.startswith(d):
                return name
        for refs, name in locked_refs:
            if pl in refs:
                return name
        return '(open project)'

    return _protected, _owner_of


# ── Raster-reference collection (used by find-orphans) ────────────────────────

def _collect_all_raster_refs(vces: list[Path], cfg: Config) -> set[str]:
    """Walk every VCE and return the set of normalised (lowercased) absolute
    paths for every raster string embedded in any of them. Only paths whose
    extension matches cfg.raster_extensions are included; the path need not
    exist on disk (we're building a reference set, not validating)."""
    exts = set(cfg.raster_extensions)
    refs: set[str] = set()
    for vce in vces:
        try:
            data = _read_object_data(vce)
        except Exception:
            continue
        for _, p in _find_paths(data):
            if Path(p).suffix.lower() in exts:
                refs.add(p.lower())
    return refs


def _clear_stale_locks(vces: list[Path], stale_days: float, dry: bool) -> int:
    """Delete lock files at least `stale_days` old — orphaned leftovers from a
    crashed or force-killed TBC session. Locks newer than the threshold are
    left alone, since they most likely belong to a project that is genuinely
    open. Returns the number of stale locks cleared."""
    cleared = 0
    for v in vces:
        age = _lock_age_days(v)
        if age is None or age < stale_days:
            continue
        lk = _lock_path(v)
        tag = '[DRY RUN] would clear' if dry else 'cleared'
        print(f'  {tag} stale lock ({age:.0f}d old): {lk.name}')
        if not dry:
            try:
                lk.unlink()
            except OSError as e:
                print(f'    Warning: could not delete {lk.name}: {e}')
                _audit('error', action='clear_lock', path=str(lk), message=str(e))
                continue
        _audit('clear_lock', path=str(lk), vce=str(v),
               age_days=round(age, 1), dry_run=dry)
        cleared += 1
    return cleared


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class PathMapping:
    old: str
    new: str


@dataclass
class Config:
    path_mappings: list[PathMapping] = field(default_factory=list)
    raster_library: str = ''
    raster_extensions: list[str] = field(
        default_factory=lambda: ['.tif', '.tiff', '.ecw', '.img', '.sid',
                                 '.jp2', '.jpg', '.jpeg', '.png', '.dem',
                                 '.adf', '.las', '.laz'])
    min_raster_mb: float = 50.0
    vce_roots: list[str] = field(default_factory=list)
    # Lock files at least this many days old are treated as stale (orphaned by
    # a crashed TBC session) and may be removed with --clear-stale-locks.
    stale_lock_days: float = 7.0
    # Append-only JSONL audit log of every mutating action. Empty = default
    # (tbc_tools.audit.jsonl beside this script). Overridden by --log.
    log_file: str = ''


def load_config(path: Path | None) -> Config:
    cfg = Config()
    if path is None:
        path = DEFAULT_CONFIG
    if not path.exists():
        return cfg
    if tomllib is None:
        print(f'Warning: cannot read {path}: tomllib unavailable '
              '(Python ≥ 3.11 or install tomli). Proceeding with defaults.',
              file=sys.stderr)
        return cfg
    with path.open('rb') as f:
        raw = tomllib.load(f)
    for m in raw.get('path_mappings', []):
        cfg.path_mappings.append(PathMapping(old=m['old'], new=m['new']))
    if 'raster_library' in raw:
        cfg.raster_library = raw['raster_library']
    if 'raster_extensions' in raw:
        cfg.raster_extensions = raw['raster_extensions']
    if 'min_raster_mb' in raw:
        cfg.min_raster_mb = float(raw['min_raster_mb'])
    if 'vce_roots' in raw:
        cfg.vce_roots = raw['vce_roots']
    if 'stale_lock_days' in raw:
        cfg.stale_lock_days = float(raw['stale_lock_days'])
    if 'log_file' in raw:
        cfg.log_file = raw['log_file']
    return cfg


# ── Hashing (for raster dedup) ────────────────────────────────────────────────

FAST_SAMPLE = 512 * 1024  # 512 KB


def fast_hash(path: Path) -> str:
    size = path.stat().st_size
    h = hashlib.sha256()
    h.update(size.to_bytes(8, 'little'))
    with path.open('rb') as f:
        h.update(f.read(FAST_SAMPLE))
        if size > FAST_SAMPLE * 2:
            f.seek(-FAST_SAMPLE, 2)
            h.update(f.read(FAST_SAMPLE))
    return h.hexdigest()


def full_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


# ── Command: list-paths ───────────────────────────────────────────────────────

def cmd_list_paths(args, cfg: Config) -> None:
    roots = [Path(r) for r in (args.paths or cfg.vce_roots)]
    if not roots:
        sys.exit('Provide paths to search, or set vce_roots in config.')
    vces = _find_vces(roots)
    if not vces:
        print('No VCE files found.')
        return
    for vce in vces:
        print(f'\n{vce}')
        try:
            data = _read_object_data(vce)
        except Exception as e:
            print(f'  Error reading: {e}')
            continue
        for _, p in _find_paths(data):
            print(f'  {p}')


# ── Backup ────────────────────────────────────────────────────────────────────

def _ensure_backup(vce: Path, dry: bool) -> None:
    """Create vce.bak alongside the VCE before the first in-place rewrite.
    If a backup already exists it is left untouched — preserves the original."""
    bak = vce.with_suffix('.vce.bak')
    if bak.exists():
        print(f'  (backup exists: {bak.name})')
        return
    if dry:
        print(f'  [DRY RUN] would backup -> {bak.name}')
        return
    shutil.copy2(str(vce), str(bak))
    _audit('backup', vce=str(vce), backup=str(bak), dry_run=False)
    print(f'  backed up -> {bak.name}')


# ── Command: normalize ────────────────────────────────────────────────────────

def cmd_normalize(args, cfg: Config) -> None:
    if not cfg.path_mappings:
        sys.exit('No path_mappings defined in config.')
    roots = [Path(r) for r in (args.paths or cfg.vce_roots)]
    if not roots:
        sys.exit('Provide paths to search, or set vce_roots in config.')
    dry = args.dry_run
    cleanup = getattr(args, 'cleanup_rasters', False)
    tag = '[DRY RUN] ' if dry else ''
    raster_exts = set(cfg.raster_extensions)
    min_bytes = cfg.min_raster_mb * 1024 * 1024

    vces = _find_vces(roots)
    print(f'Found {len(vces)} VCE file(s).\n')

    total_files, total_replacements, total_deleted_mb = 0, 0, 0.0
    for vce in vces:
        try:
            data = _read_object_data(vce)
        except Exception as e:
            print(f'  Error reading {vce.name}: {e}')
            continue

        paths = _find_paths(data)
        replacements: dict[str, str] = {}
        for _, p in paths:
            new_p = p
            for mapping in cfg.path_mappings:
                if p.lower().startswith(mapping.old.lower()):
                    new_p = mapping.new + p[len(mapping.old):]
                    break
            if new_p != p:
                replacements[p] = new_p

        if not replacements:
            continue

        print(f'{tag}{vce.name}  ({len(replacements)} path(s) to update)')
        _ensure_backup(vce, dry)
        for old, new in replacements.items():
            print(f'    - {old}')
            print(f'    + {new}')

        if not dry:
            new_data, counts = _apply_replacements(data, replacements)
            try:
                _rewrite_vce(vce, vce, new_data)
            except VceLockedError:
                print(f'  [LOCKED] skipped (opened in TBC): {vce.name}')
                _audit('skip', action='patch', vce=str(vce), reason='locked')
                continue
            applied = sum(counts.values())
            print(f'  -> {applied} occurrence(s) replaced.')
            _audit('patch', vce=str(vce), replacements=len(replacements),
                   occurrences=applied, dry_run=False)
            total_replacements += applied
        total_files += 1

        # Raster cleanup: delete local copy only when S: version is confirmed present
        if cleanup:
            for old, new in replacements.items():
                old_p, new_p = Path(old), Path(new)
                if old_p.suffix.lower() not in raster_exts:
                    continue
                if not old_p.exists():
                    continue
                sz = old_p.stat().st_size
                if sz < min_bytes:
                    continue
                if not new_p.exists():
                    print(f'  [SKIP cleanup] S: copy not found, keeping: {old_p.name}')
                    continue
                try:
                    if old_p.samefile(new_p):
                        print(f'  [SKIP cleanup] old and new are the same file (path alias): {old_p.name}')
                        continue
                except OSError:
                    pass
                mb = sz / 1e6
                print(f'  {tag}delete orphan ({mb:.0f} MB): {old_p}')
                _audit('delete', path=str(old_p), size=sz,
                       surviving_copy=str(new_p), reason='normalize-cleanup')
                total_deleted_mb += mb
                if not dry:
                    old_p.unlink()

    summary = (f'{tag}Done. {total_files} VCE file(s) updated, '
               f'{total_replacements} occurrence(s) replaced.')
    if cleanup:
        summary += f'  {total_deleted_mb:.0f} MB of orphaned rasters deleted.'
    print(f'\n{summary}')


# ── Command: verify ──────────────────────────────────────────────────────────

# Substrings (lowercased) that indicate a TBC temp/working path — always absent
_TEMP_MARKERS = (
    '\\appdata\\local\\temp\\',
    '\\tbctemporal\\',
)

# Month names for detecting TBC import-history log strings (e.g.
# "drawing.dwg 15 January 2026 3:54 PM" — not a real file reference)
_TIMESTAMP_RE = re.compile(
    r'\b\d{1,2}\s+(?:january|february|march|april|may|june|july|august|'
    r'september|october|november|december)\s+\d{4}\b',
    re.IGNORECASE,
)


def _classify_path(p: str, cfg: Config) -> tuple[str, str | None]:
    """Return (label, would_become_after_normalize_or_None)."""
    p_lower = p.lower()
    for marker in _TEMP_MARKERS:
        if marker in p_lower:
            return 'TEMP', None
    if _TIMESTAMP_RE.search(p):
        return 'LOG?', None
    if Path(p).exists():
        return 'OK', None
    for mapping in cfg.path_mappings:
        if p_lower.startswith(mapping.old.lower()):
            return 'FIXABLE', mapping.new + p[len(mapping.old):]
    return 'MISSING', None


def cmd_verify(args, cfg: Config) -> None:
    """Read-only: list every path in VCE(s) and check whether it exists on disk."""
    roots = [Path(r) for r in (args.paths or cfg.vce_roots)]
    if not roots:
        sys.exit('Provide paths to search, or set vce_roots in config.')
    vces = _find_vces(roots)
    if not vces:
        print('No VCE files found.')
        return

    for vce in vces:
        print(f'\n{vce}')
        try:
            data = _read_object_data(vce)
        except Exception as e:
            print(f'  Error reading: {e}')
            continue

        found = _find_paths(data)
        seen: set[str] = set()
        unique = [p for _, p in found if not (p in seen or seen.add(p))]

        counts: dict[str, int] = {'OK': 0, 'FIXABLE': 0, 'MISSING': 0,
                                   'TEMP': 0, 'LOG?': 0}
        for p in unique:
            label, mapped = _classify_path(p, cfg)
            counts[label] += 1
            if label == 'OK':
                print(f'  [OK]      {p}')
            elif label == 'FIXABLE':
                print(f'  [FIXABLE] {p}')
                print(f'            -> {mapped}')
            elif label == 'MISSING':
                print(f'  [MISSING] {p}')
            elif label == 'TEMP':
                print(f'  [TEMP]    {p}')
            else:  # LOG?
                print(f'  [LOG?]    {p}')

        parts = [f'{counts["OK"]} ok', f'{counts["FIXABLE"]} fixable by normalize',
                 f'{counts["MISSING"]} truly missing']
        if counts['TEMP']:
            parts.append(f'{counts["TEMP"]} temp (expected)')
        if counts['LOG?']:
            parts.append(f'{counts["LOG?"]} import-log entries')
        print(f'\n  {len(unique)} unique refs: {", ".join(parts)}')


# ── Command: patch (single VCE) ───────────────────────────────────────────────

def cmd_patch(args, cfg: Config) -> None:
    src = Path(args.src)
    dst = Path(args.dst) if args.dst else src
    if dst == src and not args.in_place:
        sys.exit('Destination is same as source. Pass --in-place to confirm '
                 'overwriting, or provide a different destination.')
    old, new = args.old_path, args.new_path
    data = _read_object_data(src)
    new_data, counts = _apply_replacements(data, {old: new})
    n = counts.get(old, 0)
    if not n:
        sys.exit(f'Path not found in {src.name}.')
    if dst == src:
        _ensure_backup(src, dry=False)
    try:
        _rewrite_vce(src, dst, new_data)
    except VceLockedError:
        sys.exit(f'{dst.name} is open in TBC (locked) — close it and retry.')
    _audit('patch', vce=str(dst), src=(str(src) if dst != src else None),
           old_path=old, new_path=new, occurrences=n, dry_run=False)
    print(f'Replaced {n} occurrence(s) in {dst.name}.')


# ── Command: scan ─────────────────────────────────────────────────────────────

def cmd_scan(args, cfg: Config) -> None:
    roots = [Path(r) for r in args.roots]
    min_bytes = (args.min_mb or cfg.min_raster_mb) * 1024 * 1024
    exts = set(cfg.raster_extensions)
    out = Path(args.out)
    consolidate = getattr(args, 'consolidate', False)

    all_files = []
    for root in roots:
        print(f'Scanning {root} for rasters >= {min_bytes/1e6:.0f} MB ...')
        all_files.extend(p for p in root.rglob('*')
                         if p.suffix.lower() in exts and p.stat().st_size >= min_bytes)
    print(f'Found {len(all_files)} raster file(s). Computing fast hashes ...')

    fast: dict[str, list[str]] = {}
    for i, p in enumerate(all_files, 1):
        print(f'  [{i}/{len(all_files)}] {p.stat().st_size/1e6:.0f} MB  {p.name}',
              flush=True)
        fast.setdefault(fast_hash(p), []).append(str(p))

    candidates = {h: ps for h, ps in fast.items() if len(ps) > 1}
    print(f'\n{len(candidates)} group(s) need full hashing ...')
    full: dict[str, list[str]] = {}
    for ps in candidates.values():
        for p in ps:
            print(f'  Full hash: {Path(p).stat().st_size/1e6:.0f} MB  {Path(p).name}',
                  flush=True)
            full.setdefault(full_hash(Path(p)), []).append(p)

    dupes = {h: ps for h, ps in full.items() if len(ps) > 1}
    wasted = sum(Path(ps[0]).stat().st_size * (len(ps) - 1) for ps in dupes.values())

    print(f'\n{len(dupes)} duplicate group(s).  '
          f'Wasted space: {wasted/1e9:.2f} GB\n')
    for ps in dupes.values():
        mb = Path(ps[0]).stat().st_size / 1e6
        print(f'  {mb:.0f} MB  ×{len(ps)}  {Path(ps[0]).name}')
        for p in ps:
            print(f'    {p}')

    # In consolidate mode, also include unique files (single-copy rasters)
    # so the plan step can relocate them to the library.
    if consolidate:
        unique = {h: ps for h, ps in fast.items() if len(ps) == 1}
        dupes.update(unique)
        print(f'\n[consolidate] Including {len(unique)} unique raster(s) '
              f'for relocation to library.')
        total_size = sum(Path(ps[0]).stat().st_size for ps in unique.values())
        print(f'  Total unique raster size: {total_size/1e9:.2f} GB')

    out.write_text(json.dumps({'roots': [str(r) for r in roots],
                               'duplicate_groups': dupes},
                              indent=2))
    print(f'\nSaved to {out}')


# ── Command: plan ─────────────────────────────────────────────────────────────

def cmd_plan(args, cfg: Config) -> None:
    data = json.loads(Path(args.scan_results).read_text())
    raw_lib = args.library or cfg.raster_library
    if not raw_lib:
        sys.exit('Specify --library or set raster_library in config.')
    lib = Path(raw_lib)
    out = Path(args.out)
    roots = [Path(r) for r in (getattr(args, 'vce_paths', None) or cfg.vce_roots)]

    vces = _find_vces(roots)
    _maybe_clear_stale_locks(args, cfg, vces)
    locked_set = {v for v in vces if _is_locked(v)}
    unlocked = [v for v in vces if v not in locked_set]
    print(f'Found {len(vces)} VCE file(s): {len(unlocked)} unlocked, '
          f'{len(locked_set)} locked (open in TBC).')

    # A copy is "protected" — never moved or deleted this round — if an open
    # (locked) project relies on it, in either of two ways:
    #   (1) the locked VCE references that exact path string, or
    #   (2) the file physically lives under the locked project's working folder.
    #       TBC's convention is '<root>\Name.vce'  <->  '<root>\Name\', and a
    #       locked project usually references its imagery by a *different* path
    #       string (each user's own OneDrive sync path) that resolves to the
    #       same file — so the folder check is what actually keeps us safe.
    for v in sorted(locked_set):
        print(f'  [LOCKED] skipping (open in TBC): {v.name}')
    _protected, _owner_of = _locked_protection(sorted(locked_set))

    def _size(path: str) -> int:
        try:
            return Path(path).stat().st_size
        except OSError:
            return 0

    moves, deletions = [], []
    vce_patches: dict[str, dict[str, str]] = {}
    claimed: dict[str, str] = {}  # dst filename -> hash that claimed it
    deferred = 0
    protected_kept = 0
    deferred_reclaim = 0          # bytes freeable once locked projects close
    deferred_by_owner: dict[str, int] = {}

    def _defer_reclaim(path: str) -> None:
        nonlocal deferred_reclaim
        sz = _size(path)
        deferred_reclaim += sz
        owner = _owner_of(path)
        deferred_by_owner[owner] = deferred_by_owner.get(owner, 0) + sz

    for h, ps in data['duplicate_groups'].items():
        # Drop protected copies from consideration; never move/delete them.
        free = [p for p in ps if not _protected(p)]
        protected = [p for p in ps if _protected(p)]
        protected_kept += len(protected)
        if not free:
            # Every copy belongs to an open project — defer the whole group.
            # Once unlocked, one becomes canonical and the rest are reclaimable.
            deferred += 1
            for p in sorted(protected, key=_size)[:-1]:
                _defer_reclaim(p)
            continue

        # Protected copies here are duplicates of a free canonical, so each one
        # becomes reclaimable once its project is closed.
        for p in protected:
            _defer_reclaim(p)

        # Prefer a file already inside the library so we don't move it
        in_lib = [p for p in free if Path(p).is_relative_to(lib)]

        # Nothing to do if every copy is already in the library.
        if in_lib and len(in_lib) == len(ps):
            continue

        # --merge-by-name: this group has no copy in the library, but if a file
        # of the same name already exists there (a re-exported/re-compressed
        # version with a different content hash), adopt it as canonical rather
        # than promoting a working copy. All working copies are then deletable
        # and every VCE is repointed to the existing library file. Without this,
        # the move would silently no-op (destination occupied) and a working
        # copy would survive unreferenced.
        if getattr(args, 'merge_by_name', False) and not in_lib:
            name = Path(free[0]).name
            existing = lib / name
            if existing.exists():
                canonical_dst = str(existing)
                claimed[name] = h
                deletions.extend(free)   # delete every working copy
                for old in ps:
                    for vce in unlocked:
                        vce_patches.setdefault(str(vce), {})[old] = canonical_dst
                print(f'  [MERGE] {name}: adopting existing library copy, '
                      f'{len(free)} working copy(ies) reclaimable')
                continue

        canonical = in_lib[0] if in_lib else free[0]
        others = [p for p in free if p != canonical]

        # Detect filename collisions across groups — append _2, _3, etc.
        base = Path(canonical).name
        if base in claimed and claimed[base] != h:
            stem, suf = Path(canonical).stem, Path(canonical).suffix
            i = 2
            while f'{stem}_{i}{suf}' in claimed:
                i += 1
            base = f'{stem}_{i}{suf}'
            print(f'  [WARN] filename collision — renamed to {base}')
        claimed[base] = h

        canonical_dst = str(lib / base)

        if Path(canonical).resolve() != Path(canonical_dst).resolve():
            moves.append({'src': canonical, 'dst': canonical_dst})

        deletions.extend(others)
        # Only patch unlocked VCEs — open projects are left untouched.
        for old in ps:
            for vce in unlocked:
                vce_patches.setdefault(str(vce), {})[old] = canonical_dst

    plan = {'library': str(lib),
            'locked_vces': [str(v) for v in sorted(locked_set)],
            'deferred_groups': deferred,
            'protected_copies': protected_kept,
            'deferred_reclaimable_bytes': deferred_reclaim,
            'deferred_by_owner': deferred_by_owner,
            'moves': moves,
            'deletions': deletions,
            'vce_patches': vce_patches}
    out.write_text(json.dumps(plan, indent=2))
    print(f'Plan written to {out}')
    print(f'  {len(moves)} file(s) to move')
    print(f'  {len(deletions)} duplicate(s) to delete')
    print(f'  up to {len(unlocked)} unlocked VCE(s) to patch')
    if deferred:
        print(f'  {deferred} group(s) deferred — every copy belongs to an open project')
    if protected_kept:
        print(f'  {protected_kept} copy(ies) left in place — owned by an open project')
    if deferred_reclaim:
        print(f'  {_human_bytes(deferred_reclaim)} additional reclaimable once '
              f'these locked projects are closed:')
        for owner, b in sorted(deferred_by_owner.items(),
                               key=lambda kv: kv[1], reverse=True):
            print(f'      {_human_bytes(b):>10}  {owner}')


# ── Command: execute ──────────────────────────────────────────────────────────

def cmd_execute(args, cfg: Config) -> None:
    plan = json.loads(Path(args.plan).read_text())
    dry = args.dry_run
    tag = '[DRY RUN] ' if dry else ''
    lib = Path(plan['library'])

    # Also apply path_mappings from config on top of the raster moves,
    # so one execute pass handles both concerns.
    extra_mappings = cfg.path_mappings  # may be empty

    if not dry:
        _maybe_clear_stale_locks(args, cfg, [Path(p) for p in plan['vce_patches']])
        lib.mkdir(parents=True, exist_ok=True)

    print(f'\n{tag}Moving canonical files to {lib} ...')
    for m in plan['moves']:
        src, dst = Path(m['src']), Path(m['dst'])
        print(f'  {src.name}')
        if not dry and not dst.exists():
            sz = _safe_size(src)
            shutil.move(str(src), str(dst))
            _audit('move', src=str(src), dst=str(dst), size=sz, dry_run=False)
        else:
            _audit('move', src=str(src), dst=str(dst), size=_safe_size(src))

    print(f'\n{tag}Patching VCE files ...')
    for vce_path, raster_replacements in plan['vce_patches'].items():
        vce = Path(vce_path)
        if not vce.exists():
            continue
        # Early lock check: skip open projects before doing any read/backup
        # work. The authoritative guard runs at the last possible moment inside
        # _rewrite_vce, just before the file is replaced.
        if _is_locked(vce):
            print(f'  [LOCKED] skipping (now open in TBC): {vce.name}')
            _audit('skip', action='patch', vce=str(vce), reason='locked')
            continue
        try:
            data = _read_object_data(vce)
        except Exception as e:
            print(f'  Error reading {vce.name}: {e}')
            continue

        # Combine raster path moves with any config prefix mappings.
        repl = _plan_vce_replacements(data, raster_replacements, extra_mappings)
        replacements = {old: new for old, (new, _cat) in repl.items()}

        if not replacements:
            continue

        print(f'  {vce.name}: {len(replacements)} path(s)')
        _ensure_backup(vce, dry)
        if not dry:
            new_data, counts = _apply_replacements(data, replacements)
            try:
                _rewrite_vce(vce, vce, new_data)
            except VceLockedError:
                print(f'  [LOCKED] skipped (opened during run): {vce.name}')
                _audit('skip', action='patch', vce=str(vce), reason='locked')
                continue
            _audit('patch', vce=str(vce), replacements=len(replacements),
                   occurrences=sum(counts.values()), dry_run=False)
        else:
            _audit('patch', vce=str(vce), replacements=len(replacements))

    if args.delete_originals:
        print(f'\n{tag}Deleting duplicate originals ...')
        # Re-scan live lock state right before deleting: a project the plan
        # saw as unlocked may have been opened in TBC since, in which case
        # its working folder (or any path it references) must not be
        # touched — this is the same last-possible-moment guard the VCE
        # patch step applies via VceLockedError, applied to deletions too.
        live_vces = _find_vces([Path(r) for r in cfg.vce_roots]) if not dry else []
        _protected, _ = _locked_protection(live_vces)
        for p in plan['deletions']:
            path = Path(p)
            if not dry and _protected(p):
                print(f'  [LOCKED] skipping deletion (project opened since plan): {path}')
                _audit('skip', action='delete', path=str(path), reason='locked')
                continue
            print(f'  {path}')
            # Note where the identical data survives (the same-name library copy
            # this duplicate was collapsed into), so the record proves nothing
            # was lost — only a redundant copy removed.
            survivor = lib / path.name
            _audit('delete', path=str(path), size=_safe_size(path),
                   surviving_copy=(str(survivor) if survivor.exists() else None),
                   reason='dedup-duplicate')
            if not dry and path.exists():
                path.unlink()
    else:
        print(f'\nSkipping deletion ({len(plan["deletions"])} copies). '
              'Re-run with --delete-originals when ready.')

    print(f'\n{tag}Done.')


# ── Command: report ───────────────────────────────────────────────────────────

def _human_bytes(n: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


_TRANS_ORDER = {'REGRESS': 0, 'REPAIR': 1, 'OK->OK': 2, 'miss->miss': 3}


def cmd_report(args, cfg: Config) -> None:
    """Render a Markdown preview of a plan: estimated savings, locked files,
    files to move/delete, and every per-VCE reference rewrite (with a check of
    whether each rewrite resolves on disk, so regressions stand out)."""
    plan = json.loads(Path(args.plan).read_text())
    out = Path(args.out)
    lib = plan.get('library', '')
    moves = plan.get('moves', [])
    deletions = plan.get('deletions', [])
    locked = plan.get('locked_vces', [])
    vce_patches = plan.get('vce_patches', {})

    size_cache: dict[str, int] = {}
    def size_of(p: str) -> int:
        if p not in size_cache:
            try:
                size_cache[p] = Path(p).stat().st_size
            except OSError:
                size_cache[p] = 0
        return size_cache[p]

    def move_size(m: dict) -> int:
        # After a move the src is gone; fall back to the dst.
        return size_of(m['src']) or size_of(m['dst'])

    move_dst = {m['dst'].lower() for m in moves}
    exist_cache: dict[str, bool] = {}
    def resolves(p: str) -> bool:
        pl = p.lower()
        if pl in move_dst:
            return True  # will exist once the plan's moves run
        if pl not in exist_cache:
            try:
                exist_cache[pl] = Path(p).exists()
            except OSError:
                exist_cache[pl] = False
        return exist_cache[pl]

    # Per-VCE change analysis (mirrors execute via the shared helper).
    per_vce: list[tuple[str, list[tuple[str, str, str, str]]]] = []
    cat_tot = {'raster': 0, 'normalize': 0}
    trans_tot = {'REPAIR': 0, 'OK->OK': 0, 'miss->miss': 0, 'REGRESS': 0}
    for vce_path, raster_repl in sorted(vce_patches.items()):
        vce = Path(vce_path)
        if not vce.exists() or _is_locked(vce):
            continue
        try:
            data = _read_object_data(vce)
        except Exception:
            continue
        repl = _plan_vce_replacements(data, raster_repl, cfg.path_mappings)
        if not repl:
            continue
        rows: list[tuple[str, str, str, str]] = []
        for old, (new, cat) in repl.items():
            o_ok, n_ok = resolves(old), resolves(new)
            kind = ('REGRESS' if o_ok and not n_ok else
                    'REPAIR' if not o_ok and n_ok else
                    'OK->OK' if o_ok else 'miss->miss')
            rows.append((old, new, cat, kind))
            cat_tot[cat] += 1
            trans_tot[kind] += 1
        rows.sort(key=lambda r: (_TRANS_ORDER[r[3]], r[2], r[0]))
        per_vce.append((vce.name, rows))

    reclaim = sum(size_of(d) for d in deletions)
    move_bytes = sum(move_size(m) for m in moves)
    total_rewrites = sum(cat_tot.values())

    L: list[str] = []
    L.append('# TBC Raster Dedup — Plan Report')
    L.append('')
    L.append(f'_Generated {datetime.now():%Y-%m-%d %H:%M} from '
             f'`{Path(args.plan).name}`_')
    L.append('')
    L.append('## Summary')
    L.append('')
    L.append('| Metric | Value |')
    L.append('|---|---|')
    L.append(f'| Library | `{lib}` |')
    L.append(f'| Canonical files to move | {len(moves)} ({_human_bytes(move_bytes)}) |')
    L.append(f'| Duplicate copies to delete | {len(deletions)} |')
    L.append(f'| **Reclaimable space** (on `--delete-originals`) | '
             f'**{_human_bytes(reclaim)}** |')
    L.append(f'| Unlocked VCEs to patch | {len(per_vce)} |')
    L.append(f'| Locked VCEs skipped | {len(locked)} |')
    if 'deferred_groups' in plan:
        L.append(f'| Groups deferred (all copies in open projects) | '
                 f'{plan["deferred_groups"]} |')
    if 'protected_copies' in plan:
        L.append(f'| Copies left in place (owned by open project) | '
                 f'{plan["protected_copies"]} |')
    if plan.get('deferred_reclaimable_bytes'):
        L.append(f'| **Reclaimable once locked projects close** | '
                 f'**{_human_bytes(plan["deferred_reclaimable_bytes"])}** |')
    L.append(f'| Reference rewrites | {total_rewrites} '
             f'({cat_tot["raster"]} raster, {cat_tot["normalize"]} normalize) |')
    L.append(f'| — repaired (broken → resolves) | {trans_tot["REPAIR"]} |')
    L.append(f'| — unchanged (resolves → resolves) | {trans_tot["OK->OK"]} |')
    L.append(f'| — still broken (pre-existing) | {trans_tot["miss->miss"]} |')
    L.append(f'| — **regressions (working → broken)** | '
             f'**{trans_tot["REGRESS"]}** |')
    L.append('')

    L.append('## Locked projects (skipped — open in TBC)')
    L.append('')
    by_owner = plan.get('deferred_by_owner', {})
    if locked:
        if by_owner:
            L.append('Each is left untouched this round. Reclaimable space shown '
                     'is what `--delete-originals` would free **once the project '
                     'is closed and re-run**:')
            L.append('')
            L.append('| Locked project | Reclaimable when closed |')
            L.append('|---|---|')
            for v in locked:
                name = Path(v).name
                L.append(f'| `{name}` | {_human_bytes(by_owner.get(name, 0))} |')
            other = by_owner.get('(open project)', 0)
            if other:
                L.append(f'| _(other open-project references)_ | '
                         f'{_human_bytes(other)} |')
            L.append(f'| **Total** | '
                     f'**{_human_bytes(plan.get("deferred_reclaimable_bytes", 0))}** |')
        else:
            for v in locked:
                L.append(f'- `{Path(v).name}`')
    else:
        L.append('_None._')
    L.append('')

    L.append(f'## Files to move → library ({len(moves)})')
    L.append('')
    if moves:
        L.append('| Size | File | From |')
        L.append('|---|---|---|')
        for m in sorted(moves, key=move_size, reverse=True):
            L.append(f'| {_human_bytes(move_size(m))} | '
                     f'`{Path(m["dst"]).name}` | `{Path(m["src"]).parent}` |')
    else:
        L.append('_None._')
    L.append('')

    L.append(f'## Duplicate copies to delete ({len(deletions)})')
    L.append('')
    L.append(f'Only removed when you re-run with `--delete-originals`. '
             f'Reclaims **{_human_bytes(reclaim)}**.')
    L.append('')
    if deletions:
        L.append('| Size | Path |')
        L.append('|---|---|')
        for d in sorted(deletions, key=size_of, reverse=True):
            L.append(f'| {_human_bytes(size_of(d))} | `{d}` |')
    else:
        L.append('_None._')
    L.append('')

    L.append('## Per-VCE reference changes')
    L.append('')
    if not per_vce:
        L.append('_No reference changes._')
    for name, rows in per_vce:
        nreg = sum(1 for r in rows if r[3] == 'REGRESS')
        warn = f' — ⚠️ {nreg} regression(s)' if nreg else ''
        L.append(f'<details><summary><strong>{name}</strong> — '
                 f'{len(rows)} change(s){warn}</summary>')
        L.append('')
        for old, new, cat, kind in rows:
            L.append(f'- `[{cat}]` `[{kind}]`')
            L.append(f'    - `- {old}`')
            L.append(f'    - `+ {new}`')
        L.append('')
        L.append('</details>')
        L.append('')

    out.write_text('\n'.join(L), encoding='utf-8')
    print(f'Report written to {out}')
    print(f'  reclaimable: {_human_bytes(reclaim)}  |  '
          f'rewrites: {total_rewrites}  |  '
          f'regressions: {trans_tot["REGRESS"]}')


# ── Command: find-orphans ────────────────────────────────────────────────────

def cmd_find_orphans(args, cfg: Config) -> None:
    """Identify raster files that no VCE anywhere references.

    Two categories are checked:

    * **Subfolder orphans** — rasters inside a project's conventional working
      subfolder (``Name\\`` next to ``Name.vce``) that are not referenced by
      *any* VCE under vce_roots.
    * **Library orphans** — rasters inside the configured ``raster_library``
      (or ``--library`` override) that are referenced by zero VCEs.

    The command is read-only by default (``--dry-run`` is the implicit mode).
    Pass ``--delete`` to actually remove the files. A live lock-protection check
    is applied before any deletion so files belonging to open TBC projects are
    never touched.
    """
    roots = [Path(r) for r in (getattr(args, 'vce_paths', None) or cfg.vce_roots)]
    if not roots:
        sys.exit('Provide --vce-paths or set vce_roots in config.')

    raw_lib = getattr(args, 'library', None) or cfg.raster_library
    lib = Path(raw_lib) if raw_lib else None

    delete = getattr(args, 'delete', False)
    dry = getattr(args, 'dry_run', True) or not delete
    tag = '[DRY RUN] ' if dry else ''
    out_path = Path(getattr(args, 'out', 'orphans.json'))
    exts = set(cfg.raster_extensions)

    # ── Collect all VCEs and build the global reference set ───────────────────
    vces = _find_vces(roots)
    print(f'Scanning {len(vces)} VCE file(s) for raster references ...')
    all_refs = _collect_all_raster_refs(vces, cfg)
    print(f'  {len(all_refs)} unique raster path(s) referenced across all projects.\n')

    # Lock protection — used to guard deletions.
    _protected, _owner_of = _locked_protection(vces)

    subfolder_orphans: list[tuple[Path, int]] = []   # (path, size_bytes)
    library_orphans:   list[tuple[Path, int]] = []
    skipped_locked = 0

    # ── 1. Subfolder orphans ──────────────────────────────────────────────────
    # Convention: Name.vce  <->  Name\
    # Walk every project's sibling subfolder and flag rasters not in all_refs.
    checked_subdirs: set[Path] = set()
    for vce in vces:
        subdir = vce.parent / vce.stem          # e.g. MyProject\ next to MyProject.vce
        if not subdir.is_dir() or subdir in checked_subdirs:
            continue
        checked_subdirs.add(subdir)
        for f in subdir.rglob('*'):
            if not f.is_file():
                continue
            if f.suffix.lower() not in exts:
                continue
            if f.stat().st_size == 0:
                continue
            if f.resolve().as_posix().lower().replace('/', '\\') in all_refs:
                continue
            # Also check with the original Windows path form.
            if str(f).lower() in all_refs:
                continue
            subfolder_orphans.append((f, f.stat().st_size))

    # ── 2. Library orphans ────────────────────────────────────────────────────
    if lib and lib.is_dir():
        for f in lib.rglob('*'):
            if not f.is_file():
                continue
            if f.suffix.lower() not in exts:
                continue
            if f.stat().st_size == 0:
                continue
            if str(f).lower() in all_refs:
                continue
            library_orphans.append((f, f.stat().st_size))
    elif lib:
        print(f'Warning: library path does not exist or is not a directory: {lib}',
              file=sys.stderr)

    # ── Report ────────────────────────────────────────────────────────────────
    def _report_group(label: str, items: list[tuple[Path, int]]) -> int:
        """Print a group; return total bytes."""
        total = sum(sz for _, sz in items)
        print(f'{label}: {len(items)} file(s)  ({_human_bytes(total)} total)')
        for f, sz in sorted(items, key=lambda x: x[1], reverse=True):
            print(f'  {_human_bytes(sz):>10}  {f}')
        return total

    print('-' * 72)
    sub_bytes = _report_group('Subfolder orphans (unreferenced inside project folders)',
                              subfolder_orphans)
    print()
    lib_bytes = _report_group(f'Library orphans (unreferenced inside {lib or "(no library)"})',
                              library_orphans)
    grand_total = sub_bytes + lib_bytes
    print('-' * 72)
    print(f'Total reclaimable: {_human_bytes(grand_total)}  '
          f'({len(subfolder_orphans) + len(library_orphans)} file(s))\n')

    # ── JSON output ───────────────────────────────────────────────────────────
    result = {
        'vce_roots': [str(r) for r in roots],
        'library': str(lib) if lib else None,
        'subfolder_orphans': [
            {'path': str(f), 'size_bytes': sz}
            for f, sz in sorted(subfolder_orphans, key=lambda x: x[1], reverse=True)
        ],
        'library_orphans': [
            {'path': str(f), 'size_bytes': sz}
            for f, sz in sorted(library_orphans, key=lambda x: x[1], reverse=True)
        ],
        'total_reclaimable_bytes': grand_total,
    }
    out_path.write_text(json.dumps(result, indent=2))
    print(f'Results written to {out_path}')

    if not delete:
        if subfolder_orphans or library_orphans:
            print('\nRe-run with --delete to remove these files '
                  '(add --dry-run to preview first).')
        return

    # ── Deletion ──────────────────────────────────────────────────────────────
    all_orphans = subfolder_orphans + library_orphans
    print(f'\n{tag}Deleting {len(all_orphans)} orphaned raster(s) ...')
    deleted_bytes = 0
    for f, sz in all_orphans:
        if _protected(str(f)):
            owner = _owner_of(str(f))
            print(f'  [LOCKED] skipping (in use by {owner}): {f.name}')
            _audit('skip', action='delete', path=str(f), reason='locked')
            skipped_locked += 1
            continue
        print(f'  {tag}delete ({_human_bytes(sz)}): {f}')
        _audit('delete', path=str(f), size=sz, reason='unreferenced-orphan')
        if not dry:
            try:
                f.unlink()
                deleted_bytes += sz
            except OSError as e:
                print(f'    Warning: could not delete {f.name}: {e}')
                _audit('error', action='delete', path=str(f), message=str(e))

    action = 'would reclaim' if dry else 'reclaimed'
    print(f'\n{tag}Done. {action} {_human_bytes(deleted_bytes if not dry else grand_total)}.'
          + (f'  {skipped_locked} file(s) skipped (locked).' if skipped_locked else ''))


# ── Command: prune-subfolders ─────────────────────────────────────────────────

def _parse_exts(values: list[str] | None) -> set[str]:
    """Normalise a list of extension tokens (accepts comma- or space-separated
    values, with or without a leading dot) into a lowercased '.ext' set."""
    exts: set[str] = set()
    for v in values or []:
        for part in str(v).replace(',', ' ').split():
            part = part.lower()
            if part:
                exts.add(part if part.startswith('.') else '.' + part)
    return exts


def _protected_prefixes(dirs: list[Path]) -> list[str]:
    """Lowercased, separator-trimmed string prefixes for the protected
    directories. Pure strings (no filesystem calls) so a walk can be pruned
    cheaply; callers pass every spelling that matters (e.g. both the mapped
    `S:\\...` form and its resolved OneDrive target)."""
    return [str(d).lower().rstrip('\\/') for d in dirs]


def _under_protected(path_str: str, prefixes: list[str]) -> bool:
    """True if `path_str` is (or is inside) any protected prefix. String-only
    comparison — keeps the raster library off-limits during pruning without
    resolving every walked path."""
    if not prefixes:
        return False
    pl = path_str.lower().rstrip('\\/')
    return any(pl == p or pl.startswith(p + '\\') or pl.startswith(p + '/')
               for p in prefixes)


# ── Filename date parsing (for prune-subfolders --date-from name) ─────────────

_MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
           'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}

# Month-name form:  Jun_18,_2026 / June 18, 2026 / Jan-3-2026
_DATE_MON_RE = re.compile(
    r'(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*'
    r'[ _,.\-]+(?P<day>\d{1,2})(?:st|nd|rd|th)?[ _,.\-]+(?P<year>20\d{2})',
    re.IGNORECASE)
# ISO form:      2026-06-24 / 2026_06_24 / 2026.06.24
_DATE_ISO_RE = re.compile(r'(?P<year>20\d{2})[ _.\-](?P<mon>\d{1,2})[ _.\-](?P<day>\d{1,2})')
# Compact form:  20260624 (YYYYMMDD)
_DATE_COMPACT_RE = re.compile(r'(?<!\d)(?P<year>20\d{2})(?P<mon>\d{2})(?P<day>\d{2})(?!\d)')
# US numeric:    6-24-2026 / 06_24_2026
_DATE_US_RE = re.compile(r'(?<!\d)(?P<mon>\d{1,2})[ _.\-](?P<day>\d{1,2})[ _.\-](?P<year>20\d{2})')
# Compact 6-digit YYMMDD: 260313 -> 2026-03-13. Most ambiguous, tried last, and
# only accepted when it yields a valid month/day (yy is mapped to 20yy).
_DATE_COMPACT6_RE = re.compile(r'(?<!\d)(?P<yy>[0-3]\d)(?P<mon>\d{2})(?P<day>\d{2})(?!\d)')


def _mk_date(year: int, mon: int, day: int) -> datetime | None:
    try:
        return datetime(year, mon, day)
    except ValueError:
        return None


def _date_from_name(name: str) -> datetime | None:
    """Parse a date embedded in a filename. Handles month-name (Jun_18,_2026),
    ISO (2026-06-24), compact 8-digit (20260624), US-numeric (6-24-2026) and
    compact 6-digit (260313 -> 2026-03-13) forms. For ambiguous numeric forms
    the first component is treated as the month; if that yields an invalid date
    the two are swapped (so 24-06-2026 still parses). The 6-digit YYMMDD form is
    tried last and only accepted when it validates. Returns midnight of the
    parsed day, or None if no valid date is found."""
    m = _DATE_MON_RE.search(name)
    if m:
        d = _mk_date(int(m.group('year')), _MONTHS[m.group('mon').lower()[:3]],
                     int(m.group('day')))
        if d:
            return d
    for rx in (_DATE_ISO_RE, _DATE_COMPACT_RE, _DATE_US_RE):
        for m in rx.finditer(name):
            y, a, b = int(m.group('year')), int(m.group('mon')), int(m.group('day'))
            d = _mk_date(y, a, b) or _mk_date(y, b, a)
            if d:
                return d
    for m in _DATE_COMPACT6_RE.finditer(name):
        d = _mk_date(2000 + int(m.group('yy')), int(m.group('mon')), int(m.group('day')))
        if d:
            return d
    return None


def _scan_rasters(walk_dirs: list[Path], exts: set[str], cutoff: float,
                  protected_dirs: list[Path], all_refs: set[str],
                  date_from: str = 'mtime',
                  ) -> tuple[list[tuple[Path, int, float, bool, str]],
                             list[tuple[Path, int]]]:
    """Walk each directory in `walk_dirs` (recursively) for raster files whose
    extension is in `exts`, skipping any subtree under `protected_dirs`.

    The age of each file is taken from `date_from`: ``'mtime'`` uses the
    filesystem modified time; ``'name'`` uses a date parsed from the filename
    (see `_date_from_name`). Files older than `cutoff` (epoch seconds) are
    returned as ``(path, size, epoch, referenced, source)``. In ``'name'`` mode,
    files with no recognizable date are never aged-out; they are returned
    separately as ``(path, size)`` so the caller can report and skip them.
    `referenced` flags whether any VCE still points at the file (informational).
    Files are de-duplicated by path, so overlapping/nested `walk_dirs` are safe."""
    prefixes = _protected_prefixes(protected_dirs)
    found: list[tuple[Path, int, float, bool, str]] = []
    undated: list[tuple[Path, int]] = []
    seen: set[str] = set()
    for base in walk_dirs:
        if not base.is_dir() or _under_protected(str(base), prefixes):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            # Don't descend into the protected library subtree(s).
            dirnames[:] = [d for d in dirnames
                           if not _under_protected(os.path.join(dirpath, d), prefixes)]
            for fn in filenames:
                if Path(fn).suffix.lower() not in exts:
                    continue
                f = Path(dirpath) / fn
                key = str(f).lower()
                if key in seen:
                    continue
                seen.add(key)
                try:
                    st = f.stat()
                except OSError:
                    continue
                if st.st_size == 0:
                    continue
                if date_from == 'name':
                    d = _date_from_name(fn)
                    if d is None:
                        undated.append((f, st.st_size))
                        continue
                    epoch = d.timestamp()
                else:
                    epoch = st.st_mtime
                if epoch >= cutoff:
                    continue
                referenced = (key in all_refs
                              or f.resolve().as_posix().lower().replace('/', '\\') in all_refs)
                found.append((f, st.st_size, epoch, referenced, date_from))
    return found, undated


def _old_subfolder_rasters(vces: list[Path], exts: set[str], cutoff: float,
                           protected_dirs: list[Path], all_refs: set[str],
                           date_from: str = 'mtime',
                           ) -> tuple[list[tuple[Path, int, float, bool, str]],
                                      list[tuple[Path, int]]]:
    """Scan only project working subfolders (``Name\\`` next to ``Name.vce``).
    Thin wrapper over `_scan_rasters` that restricts the walk to those subdirs;
    see `_scan_rasters` for the return shape and age/`date_from` semantics."""
    subdirs: list[Path] = []
    checked: set[Path] = set()
    for vce in vces:
        subdir = vce.parent / vce.stem          # Name\ next to Name.vce
        if subdir.is_dir() and subdir not in checked:
            checked.add(subdir)
            subdirs.append(subdir)
    return _scan_rasters(subdirs, exts, cutoff, protected_dirs, all_refs, date_from)


def cmd_prune_subfolders(args, cfg: Config) -> None:
    """Delete rasters in project folders older than a cutoff age.

    Unlike ``find-orphans``, this does not care whether a VCE still references
    the file — it prunes purely by age and location. By default it only walks
    each project's ``Name\\`` working subfolder; ``--recursive`` walks the given
    ``--roots`` in full. The raster library is always protected (never pruned),
    and files an open TBC project relies on are skipped at deletion time.
    Read-only by default; pass ``--delete``.
    """
    roots = [Path(r) for r in (getattr(args, 'roots', None)
                               or getattr(args, 'vce_paths', None)
                               or cfg.vce_roots)]
    if not roots:
        sys.exit('Provide --roots (or set vce_roots in config).')
    recursive = getattr(args, 'recursive', False)

    exts = _parse_exts(args.ext) if getattr(args, 'ext', None) else {'.tif', '.tiff'}
    days = args.older_than_days
    cutoff = time.time() - days * 86400.0

    # Protected library: never prune anything inside it. --library "" disables.
    # Keep BOTH the given spelling (e.g. mapped S:\...) and its resolved target,
    # so the string-prefix guard matches whichever form the walk produces.
    raw_lib = getattr(args, 'library', None)
    if raw_lib is None:
        raw_lib = cfg.raster_library
    protected_dirs: list[Path] = []
    if raw_lib:
        lp = Path(raw_lib)
        protected_dirs.append(lp)
        try:
            rp = lp.resolve()
            if rp != lp:
                protected_dirs.append(rp)
        except OSError:
            pass

    delete = getattr(args, 'delete', False)
    dry = getattr(args, 'dry_run', True) or not delete
    tag = '[DRY RUN] ' if dry else ''
    out_path = Path(getattr(args, 'out', 'prune.json'))

    date_from = getattr(args, 'date_from', 'mtime')
    # Discover VCEs across both the scan roots and the configured vce_roots, so
    # lock-protection and reference detection see every relevant project.
    vce_search = {str(r) for r in roots} | set(cfg.vce_roots)
    vces = _find_vces([Path(r) for r in vce_search])
    basis = 'date in filename' if date_from == 'name' else 'filesystem mtime'
    scope = 'recursively under roots' if recursive else 'in project Name\\ subfolders'
    print(f'Scanning for {"/".join(sorted(exts))} file(s) older than {days:g} '
          f'day(s) by {basis}, {scope} ({len(vces)} VCE(s) checked for '
          f'locks/refs) ...')
    all_refs = _collect_all_raster_refs(vces, cfg)
    _protected, _owner_of = _locked_protection(vces)

    if recursive:
        items, undated = _scan_rasters(roots, exts, cutoff, protected_dirs,
                                       all_refs, date_from)
    else:
        items, undated = _old_subfolder_rasters(vces, exts, cutoff, protected_dirs,
                                                all_refs, date_from)
    items.sort(key=lambda x: x[1], reverse=True)

    total = sum(sz for _, sz, _, _, _ in items)
    referenced = [it for it in items if it[3]]
    now = time.time()

    where = 'under roots' if recursive else 'in project subfolders'
    print('-' * 72)
    print(f'{len(items)} file(s) older than {days:g}d {where} '
          f'({_human_bytes(total)} total)')
    if protected_dirs:
        print(f'Protected (never pruned): {str(protected_dirs[0])}')
    for f, sz, ep, ref, _src in items:
        flag = '  [STILL REFERENCED]' if ref else ''
        print(f'  {_human_bytes(sz):>10}  {datetime.fromtimestamp(ep):%Y-%m-%d} '
              f'({(now - ep) / 86400.0:4.0f}d)  {f}{flag}')
    print('-' * 72)
    print(f'Total: {_human_bytes(total)} in {len(items)} file(s).')
    if referenced:
        print(f'WARNING: {len(referenced)} file(s) are STILL REFERENCED by a VCE. '
              f'Deleting them will break those projects unless already repointed '
              f'to the library.')
    if undated:
        u_bytes = sum(sz for _, sz in undated)
        print(f'\n{len(undated)} file(s) had no recognizable date in the filename '
              f'— SKIPPED, never deleted ({_human_bytes(u_bytes)}):')
        for f, sz in sorted(undated, key=lambda x: x[1], reverse=True):
            print(f'  {_human_bytes(sz):>10}  {f}')

    out_path.write_text(json.dumps({
        'vce_roots': [str(r) for r in roots],
        'protected_dirs': [str(d) for d in protected_dirs],
        'older_than_days': days,
        'date_from': date_from,
        'extensions': sorted(exts),
        'files': [{'path': str(f), 'size_bytes': sz,
                   'date': datetime.fromtimestamp(ep).strftime('%Y-%m-%d'),
                   'age_days': round((now - ep) / 86400.0, 1),
                   'source': src, 'referenced': ref}
                  for f, sz, ep, ref, src in items],
        'undated_skipped': [{'path': str(f), 'size_bytes': sz} for f, sz in undated],
        'total_bytes': total,
    }, indent=2))
    print(f'Results written to {out_path}')

    if not delete:
        if items:
            print('\nRe-run with --delete to remove these files '
                  '(add --dry-run to preview first).')
        return

    print(f'\n{tag}Deleting {len(items)} file(s) ...')
    deleted_bytes, skipped_locked = 0, 0
    for f, sz, _ep, _ref, _src in items:
        if _protected(str(f)):
            print(f'  [LOCKED] skipping (in use by {_owner_of(str(f))}): {f.name}')
            _audit('skip', action='delete', path=str(f), reason='locked')
            skipped_locked += 1
            continue
        print(f'  {tag}delete ({_human_bytes(sz)}): {f}')
        # Age-based prune keeps no canonical copy — record the referenced flag so
        # a deletion that could break a project is visible after the fact.
        _audit('delete', path=str(f), size=sz, referenced=bool(_ref),
               age_basis=_src, reason='prune-age')
        if not dry:
            try:
                f.unlink()
                deleted_bytes += sz
            except OSError as e:
                print(f'    Warning: could not delete {f.name}: {e}')
                _audit('error', action='delete', path=str(f), message=str(e))

    action = 'would reclaim' if dry else 'reclaimed'
    print(f'\n{tag}Done. {action} {_human_bytes(total if dry else deleted_bytes)}.'
          + (f'  {skipped_locked} file(s) skipped (locked).' if skipped_locked else ''))


# ── Stale-lock handling ───────────────────────────────────────────────────────

def _resolve_stale_days(args, cfg: Config) -> float:
    """CLI --stale-lock-days overrides the config's stale_lock_days."""
    val = getattr(args, 'stale_lock_days', None)
    return cfg.stale_lock_days if val is None else val


def _maybe_clear_stale_locks(args, cfg: Config, vces: list[Path]) -> None:
    """If --clear-stale-locks was passed, delete lock files older than the
    threshold before the caller inspects lock state. No-op otherwise."""
    if not getattr(args, 'clear_stale_locks', False):
        return
    days = _resolve_stale_days(args, cfg)
    print(f'Clearing stale locks (>= {days:g} day(s) old) ...')
    n = _clear_stale_locks(vces, days, dry=False)
    print(f'  {n} stale lock(s) cleared.\n')


def cmd_clear_locks(args, cfg: Config) -> None:
    """Delete orphaned TBC lock files (older than the stale threshold)."""
    roots = [Path(r) for r in (args.paths or cfg.vce_roots)]
    if not roots:
        sys.exit('Provide paths to search, or set vce_roots in config.')
    days = cfg.stale_lock_days if args.days is None else args.days
    dry = args.dry_run
    vces = _find_vces(roots)
    locked = sorted(v for v in vces if _is_locked(v))
    print(f'{len(locked)} lock file(s) among {len(vces)} VCE(s). '
          f'Stale threshold: {days:g} day(s).')
    for v in locked:
        age = _lock_age_days(v) or 0.0
        if age < days:
            print(f'  [active] {age:5.1f}d  keeping  {_lock_path(v).name}')
    cleared = _clear_stale_locks(locked, days, dry)
    tag = '[DRY RUN] ' if dry else ''
    print(f'\n{tag}{cleared} stale lock(s) {"would be " if dry else ""}cleared.')


# ── Argument parsing ──────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='tbc_tools',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--config', metavar='FILE',
                   help=f'config file (default: {DEFAULT_CONFIG.name} '
                        'alongside this script)')
    p.add_argument('--log', metavar='FILE',
                   help='append a JSONL audit record of every change (moves, '
                        'deletions, VCE rewrites, lock removals) to FILE. '
                        'Default: log_file in config, else tbc_tools.audit.jsonl '
                        'beside this script. Read-only commands are not logged.')

    sub = p.add_subparsers(dest='cmd', required=True)

    # list-paths
    lp = sub.add_parser('list-paths', help='list file paths embedded in VCE files')
    lp.add_argument('paths', nargs='*',
                    help='VCE files or directories to search '
                         '(defaults to vce_roots in config)')

    # verify
    vr = sub.add_parser('verify',
                        help='(read-only) list paths in VCE(s) and check each exists on disk')
    vr.add_argument('paths', nargs='*',
                    help='VCE files or directories (defaults to vce_roots in config)')

    # normalize
    nm = sub.add_parser('normalize',
                        help='apply path-prefix mappings from config to VCE files')
    nm.add_argument('paths', nargs='*',
                    help='VCE files or directories (defaults to vce_roots in config)')
    nm.add_argument('--dry-run', action='store_true')
    nm.add_argument('--cleanup-rasters', action='store_true',
                    help='after patching, delete local raster copies whose S: '
                         'equivalent is confirmed to exist (large files only)')

    # patch
    pt = sub.add_parser('patch', help='replace one specific path in a VCE file')
    pt.add_argument('src', help='source VCE file')
    pt.add_argument('dst', nargs='?', help='destination VCE (omit to patch in place)')
    pt.add_argument('old_path', help='exact path string to replace')
    pt.add_argument('new_path', help='replacement path')
    pt.add_argument('--in-place', action='store_true',
                    help='required when dst is omitted, to confirm overwrite')

    # scan
    sc = sub.add_parser('scan', help='find duplicate rasters in a directory tree')
    sc.add_argument('roots', nargs='+', help='root directories to scan')
    sc.add_argument('--min-mb', type=float,
                    help='minimum file size in MB (overrides config)')
    sc.add_argument('--consolidate', action='store_true',
                    help='include ALL rasters (not just duplicates) so plan '
                         'can relocate everything to the library')
    sc.add_argument('--out', default='dupes.json')

    # plan
    pl = sub.add_parser('plan', help='generate a consolidation plan from scan results')
    pl.add_argument('scan_results', help='dupes.json from scan')
    pl.add_argument('--library', help='destination for canonical copies (overrides config)')
    pl.add_argument('--out', default='plan.json')
    pl.add_argument('--vce-paths', nargs='*', dest='vce_paths',
                    help='VCE files or directories to patch '
                         '(defaults to vce_roots in config)')
    pl.add_argument('--merge-by-name', action='store_true',
                    help='if a duplicate group has no copy in the library but a '
                         'file of the SAME NAME already exists there, adopt that '
                         'library file as canonical even though its content hash '
                         'differs, and delete the working copies. Use when the '
                         'library holds a re-exported/re-compressed version of '
                         'the same raster (verify georeferencing matches first).')
    _add_stale_lock_args(pl)

    # execute
    ex = sub.add_parser('execute', help='carry out a consolidation plan')
    ex.add_argument('plan', help='plan.json from plan step')
    ex.add_argument('--dry-run', action='store_true')
    ex.add_argument('--delete-originals', action='store_true',
                    help='delete duplicate copies after patching VCEs')
    _add_stale_lock_args(ex)

    # report
    rp = sub.add_parser('report',
                        help='write a Markdown preview of a plan (savings, '
                             'locked files, moves/deletes, per-VCE changes)')
    rp.add_argument('plan', help='plan.json from plan step')
    rp.add_argument('--out', default='report.md')

    # clear-locks
    cl = sub.add_parser('clear-locks',
                        help='delete orphaned TBC lock files older than the '
                             'stale threshold (default 7 days)')
    cl.add_argument('paths', nargs='*',
                    help='VCE files or directories (defaults to vce_roots in config)')
    cl.add_argument('--days', type=float,
                    help='stale threshold in days (overrides stale_lock_days in config)')
    cl.add_argument('--dry-run', action='store_true',
                    help='show what would be cleared without deleting')

    # find-orphans
    fo = sub.add_parser('find-orphans',
                        help='find raster files no longer referenced by any VCE '
                             '(subfolder orphans and library orphans)')
    fo.add_argument('--library',
                    help='raster library directory to check for orphans '
                         '(overrides raster_library in config)')
    fo.add_argument('--vce-paths', nargs='*', dest='vce_paths',
                    help='VCE files or directories to scan '
                         '(defaults to vce_roots in config)')
    fo.add_argument('--delete', action='store_true',
                    help='delete the orphaned files (default: report only)')
    fo.add_argument('--dry-run', action='store_true',
                    help='when used with --delete, show what would be removed '
                         'without actually deleting anything')
    fo.add_argument('--out', default='orphans.json',
                    help='write a JSON summary of orphans found (default: orphans.json)')

    # prune-subfolders
    pr = sub.add_parser('prune-subfolders',
                        help='delete rasters in project subfolders older than N '
                             'days (default .tif/.tiff), never touching the library')
    pr.add_argument('--older-than-days', type=float, default=60.0,
                    help='only prune files older than this many days '
                         '(default: 60, ~2 months)')
    pr.add_argument('--date-from', choices=['mtime', 'name'], default='mtime',
                    help="age basis: 'mtime' = filesystem modified time (default); "
                         "'name' = date parsed from the geotiff filename (e.g. "
                         "Jun_18,_2026, 6-24-2026 or 260313). In 'name' mode, files "
                         'with no recognizable date are reported and never deleted.')
    pr.add_argument('--ext', nargs='*',
                    help='extensions to prune (default: .tif .tiff), '
                         'e.g. --ext .tif .tiff .ecw')
    pr.add_argument('--roots', nargs='*',
                    help='directories to scan for old rasters (defaults to '
                         'vce_roots in config). e.g. --roots "S:\\Design" '
                         '"S:\\Projects"')
    pr.add_argument('--recursive', action='store_true',
                    help='walk each root in full instead of only project Name\\ '
                         'subfolders — catches rasters anywhere under the roots '
                         '(the raster library is still protected)')
    pr.add_argument('--vce-paths', nargs='*', dest='vce_paths',
                    help='alias for --roots (kept for backwards compatibility)')
    pr.add_argument('--library',
                    help='directory to protect from pruning (defaults to '
                         'raster_library in config); pass "" to disable the guard')
    pr.add_argument('--delete', action='store_true',
                    help='delete the files (default: report only)')
    pr.add_argument('--dry-run', action='store_true',
                    help='with --delete, show what would be removed without deleting')
    pr.add_argument('--out', default='prune.json',
                    help='write a JSON summary of files found (default: prune.json)')

    return p


def _add_stale_lock_args(sp: argparse.ArgumentParser) -> None:
    """Shared --clear-stale-locks / --stale-lock-days options for plan/execute.

    Clearing stale locks is ON by default: a lock older than the threshold is
    almost always an orphan from a crashed TBC session, and leaving it in place
    needlessly excludes the project. Pass --no-clear-stale-locks to keep them."""
    sp.add_argument('--clear-stale-locks', action=argparse.BooleanOptionalAction,
                    default=True,
                    help='before checking locks, delete lock files older than the '
                         'stale threshold (default: on; --no-clear-stale-locks to keep)')
    sp.add_argument('--stale-lock-days', type=float, default=None,
                    help='stale threshold in days (overrides stale_lock_days in config)')


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    cfg = load_config(Path(args.config) if args.config else None)

    dispatch = {
        'list-paths': cmd_list_paths,
        'verify': cmd_verify,
        'normalize': cmd_normalize,
        'patch': cmd_patch,
        'scan': cmd_scan,
        'plan': cmd_plan,
        'execute': cmd_execute,
        'report': cmd_report,
        'clear-locks': cmd_clear_locks,
        'find-orphans': cmd_find_orphans,
        'prune-subfolders': cmd_prune_subfolders,
    }

    # Enable the audit log for any command that can change files on disk. The
    # effective dry-run state (nothing actually written) is derived per command
    # so each record's dry_run flag is truthful.
    global _AUDIT
    if args.cmd in {'normalize', 'patch', 'execute', 'clear-locks',
                    'find-orphans', 'prune-subfolders'}:
        if args.cmd == 'execute':
            eff_dry = (bool(getattr(args, 'dry_run', False))
                       or not getattr(args, 'delete_originals', False))
        elif args.cmd in {'find-orphans', 'prune-subfolders'}:
            eff_dry = (bool(getattr(args, 'dry_run', False))
                       or not getattr(args, 'delete', False))
        elif args.cmd == 'patch':
            eff_dry = False
        else:
            eff_dry = bool(getattr(args, 'dry_run', False))
        log_path = (Path(args.log) if getattr(args, 'log', None)
                    else Path(cfg.log_file) if cfg.log_file
                    else DEFAULT_CONFIG.with_name('tbc_tools.audit.jsonl'))
        _AUDIT = AuditLog(log_path, args.cmd, sys.argv[1:], eff_dry,
                          extra={'library': cfg.raster_library or None})
        print(f'Audit log: {log_path}')

    try:
        dispatch[args.cmd](args, cfg)
    finally:
        _audit_end()


if __name__ == '__main__':
    main()
