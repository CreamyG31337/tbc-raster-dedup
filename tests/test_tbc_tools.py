"""Stdlib tests for tbc_tools (no third-party deps, no real project data).

Run:  uv run python tests/test_tbc_tools.py
"""
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tbc_tools as t  # noqa: E402


def test_vlq_roundtrip():
    for n in (0, 1, 127, 128, 300, 16384, 1_000_000):
        b = t.encode_vlq(n)
        val, consumed = t.decode_vlq(b, 0)
        assert val == n and consumed == len(b), (n, val, consumed)


def test_find_and_replace_path():
    # ObjectData stores paths as: [VLQ byte-length][utf-8 path][0x4D 0x46]
    old = 'C:\\temp\\sample.tif'
    ob = old.encode('utf-8')
    data = t.encode_vlq(len(ob)) + ob + b'\x4D\x46'

    assert [p for _, p in t._find_paths(data)] == [old]

    new = 'S:\\lib\\sample.tif'
    patched, counts = t._apply_replacements(data, {old: new})
    assert counts.get(old) == 1
    nb = new.encode('utf-8')
    assert patched == t.encode_vlq(len(nb)) + nb + b'\x4D\x46'


def _make_vce(path: Path, object_data: bytes):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('ObjectData', object_data)
        z.writestr('other.txt', b'unchanged')


def test_rewrite_applies_when_unlocked():
    tmp = Path(tempfile.mkdtemp())
    vce = tmp / 'proj.vce'
    _make_vce(vce, b'OLD-OBJECT-DATA')
    t._rewrite_vce(vce, vce, b'NEW-OBJECT-DATA')
    assert t._read_object_data(vce) == b'NEW-OBJECT-DATA'
    assert not (tmp / 'proj.vce.patching').exists(), 'temp not cleaned up'


def test_rewrite_aborts_when_locked():
    """The last-possible-moment guard: if a lock file appears before the final
    replace, the live project must be left untouched."""
    tmp = Path(tempfile.mkdtemp())
    vce = tmp / 'proj.vce'
    _make_vce(vce, b'OLD-OBJECT-DATA')
    vce.with_name(vce.name + ',lk').write_bytes(b'')  # simulate TBC opening it

    raised = False
    try:
        t._rewrite_vce(vce, vce, b'SHOULD-NOT-LAND')
    except t.VceLockedError:
        raised = True
    assert raised, 'expected VceLockedError when locked'
    assert t._read_object_data(vce) == b'OLD-OBJECT-DATA', 'project modified despite lock!'
    assert not (tmp / 'proj.vce.patching').exists(), 'temp not cleaned up after abort'


def test_clear_stale_locks_keeps_recent():
    """Stale locks (older than the threshold) are deleted; recent ones kept."""
    tmp = Path(tempfile.mkdtemp())
    stale = tmp / 'stale.vce'
    fresh = tmp / 'fresh.vce'
    for v, age_days in ((stale, 30), (fresh, 1)):
        _make_vce(v, b'OBJ')
        lk = t._lock_path(v)
        lk.write_bytes(b'')
        when = time.time() - age_days * 86400
        os.utime(lk, (when, when))

    vces = t._find_vces([tmp])
    cleared = t._clear_stale_locks(vces, stale_days=7, dry=False)
    assert cleared == 1
    assert not t._is_locked(stale), 'stale lock should be removed'
    assert t._is_locked(fresh), 'recent lock must be kept'

    # dry-run must not delete
    again = t._clear_stale_locks([fresh], stale_days=0, dry=True)
    assert again == 1 and t._is_locked(fresh), 'dry-run must not delete'


def test_parse_exts_normalises():
    assert t._parse_exts(['.tif', 'tiff']) == {'.tif', '.tiff'}
    assert t._parse_exts(['.tif,.tiff .ecw']) == {'.tif', '.tiff', '.ecw'}
    assert t._parse_exts(None) == set()


def test_under_protected():
    pre = t._protected_prefixes([Path(r'S:\Basemaps')])
    assert t._under_protected(r'S:\Basemaps\a.tif', pre)
    assert t._under_protected(r'S:\Basemaps', pre)  # the dir itself
    assert not t._under_protected(r'S:\Projects\x.tif', pre)
    # a sibling that shares a name prefix must NOT be considered inside
    assert not t._under_protected(r'S:\Basemaps-OLD\x.tif', pre)
    assert not t._under_protected(r'S:\anything', [])  # no prefixes -> never


def test_prune_selects_old_subfolder_tifs():
    """Only .tif/.tiff older than the cutoff, inside a project's Name\\ folder,
    and outside the protected library are selected."""
    tmp = Path(tempfile.mkdtemp())
    _make_vce(tmp / 'ProjA.vce', b'OBJ')
    sub = tmp / 'ProjA'
    sub.mkdir()
    old_tif = sub / 'old.tif'
    new_tif = sub / 'new.tif'
    old_ecw = sub / 'old.ecw'      # wrong extension -> excluded
    for f in (old_tif, new_tif, old_ecw):
        f.write_bytes(b'x' * 10)
    when = time.time() - 90 * 86400  # 90 days old
    for f in (old_tif, old_ecw):
        os.utime(f, (when, when))

    # A loose tif not under any project subfolder is ignored entirely.
    (tmp / 'loose.tif').write_bytes(b'x' * 10)
    os.utime(tmp / 'loose.tif', (when, when))

    vces = t._find_vces([tmp])
    cutoff = time.time() - 60 * 86400  # 2 months
    items, undated = t._old_subfolder_rasters(vces, {'.tif', '.tiff'}, cutoff, [], set())
    assert {p.name for p, _, _, _, _ in items} == {'old.tif'}
    assert undated == []  # mtime mode never produces undated files

    # referenced flag is set when a ref string matches
    refs = {str(old_tif).lower()}
    items, _ = t._old_subfolder_rasters(vces, {'.tif', '.tiff'}, cutoff, [], refs)
    assert items and items[0][3] is True


def test_date_from_name():
    d = t._date_from_name('Site_Dam_Upgrades_Jun_18,_2026_polygon-1_orthophoto.tiff')
    assert (d.year, d.month, d.day) == (2026, 6, 18)
    d = t._date_from_name('Site_Iso_6-24-2026_UTM10_50cm.tif')
    assert (d.year, d.month, d.day) == (2026, 6, 24)
    d = t._date_from_name('ortho_May_29,_2026_polygon-8.tiff')
    assert (d.year, d.month, d.day) == (2026, 5, 29)
    d = t._date_from_name('site_2026-01-03_scan.tif')
    assert (d.year, d.month, d.day) == (2026, 1, 3)
    d = t._date_from_name('site_20260103_scan.tif')
    assert (d.year, d.month, d.day) == (2026, 1, 3)
    # 6-digit YYMMDD
    d = t._date_from_name('Site_Dam_Upgrades_260313_orthophoto.tiff')
    assert (d.year, d.month, d.day) == (2026, 3, 13)
    # day-first numeric still parses via the swap fallback
    d = t._date_from_name('flight_24-06-2026.tif')
    assert (d.year, d.month, d.day) == (2026, 6, 24)
    # no date, and false positives we must NOT treat as dates
    assert t._date_from_name('basemap_UTM10_50cm.tif') is None
    assert t._date_from_name('polygon-1_orthophoto.tif') is None
    # a 6-digit run that is not a valid date must not parse
    assert t._date_from_name('count_123456_x.tif') is None  # month 34 invalid


def test_prune_date_from_name():
    """In 'name' mode age comes from the filename; undated files are reported
    separately and never selected for deletion."""
    tmp = Path(tempfile.mkdtemp())
    _make_vce(tmp / 'ProjA.vce', b'OBJ')
    sub = tmp / 'ProjA'
    sub.mkdir()
    old_named = sub / 'ortho_Jan_10,_2026.tif'     # ~6 months before "now" below
    new_named = sub / 'ortho_Jun_24,_2026.tif'     # recent
    undated_f = sub / 'basemap_UTM10.tif'          # no date in name
    for f in (old_named, new_named, undated_f):
        f.write_bytes(b'x' * 10)
    # Filesystem mtime is deliberately "new" for all, proving name-date wins.
    now_ref = time.mktime(time.strptime('2026-07-10', '%Y-%m-%d'))

    vces = t._find_vces([tmp])
    cutoff = now_ref - 60 * 86400  # 60 days before 2026-07-10 -> 2026-05-11
    items, undated = t._old_subfolder_rasters(
        vces, {'.tif', '.tiff'}, cutoff, [], set(), date_from='name')
    assert {p.name for p, *_ in items} == {'ortho_Jan_10,_2026.tif'}
    assert {p.name for p, _ in undated} == {'basemap_UTM10.tif'}


def test_scan_rasters_recursive_and_protected():
    """--recursive mode finds rasters in arbitrary nested folders (no .vce
    needed) and never descends into a protected library subtree."""
    tmp = Path(tempfile.mkdtemp())
    design = tmp / 'Design' / 'Area-A' / 'Example - Project'
    design.mkdir(parents=True)
    hit = design / 'Site_Dam_Upgrades_260313_orthophoto.tiff'  # 2026-03-13
    hit.write_bytes(b'x' * 10)
    lib = tmp / 'Output' / 'Basemaps' / '2026'
    lib.mkdir(parents=True)
    (lib / 'basemap_260101.tiff').write_bytes(b'x' * 10)   # protected -> kept

    now_ref = time.mktime(time.strptime('2026-07-10', '%Y-%m-%d'))
    cutoff = now_ref - 60 * 86400
    items, undated = t._scan_rasters([tmp], {'.tif', '.tiff'}, cutoff,
                                     [lib], set(), date_from='name')
    assert {p.name for p, *_ in items} == {
        'Site_Dam_Upgrades_260313_orthophoto.tiff'}
    # Non-recursive mode would miss it entirely (no Name\ subfolder / no .vce).
    vces = t._find_vces([tmp])
    items2, _ = t._old_subfolder_rasters(vces, {'.tif', '.tiff'}, cutoff, [lib],
                                         set(), date_from='name')
    assert items2 == []


def test_audit_log_writes_jsonl():
    """AuditLog appends one JSON object per action, bracketed by run_start/
    run_end, all sharing a run id and carrying timestamps."""
    import json
    tmp = Path(tempfile.mkdtemp())
    logp = tmp / 'sub' / 'audit.jsonl'   # parent dir created on demand
    al = t.AuditLog(logp, 'execute', ['execute', 'plan.json'], dry=False,
                    extra={'library': 'S:\\lib', 'skipme': None})
    al.record('move', src='a', dst='b', size=10)
    al.record('delete', path='c', size=5, surviving_copy='S:\\lib\\c')
    al.end(files=1)

    recs = [json.loads(ln) for ln in logp.read_text(encoding='utf-8').splitlines()]
    assert [r['event'] for r in recs] == ['run_start', 'move', 'delete', 'run_end']
    assert all('ts' in r and r['run'] == recs[0]['run'] for r in recs)
    assert recs[0]['library'] == 'S:\\lib'
    assert 'skipme' not in recs[0]           # None-valued extras are dropped
    d = next(r for r in recs if r['event'] == 'delete')
    assert d['surviving_copy'] == 'S:\\lib\\c' and d['dry_run'] is False


def test_audit_helpers_noop_and_defaults():
    """_audit is a silent no-op with no active log; when active it inherits the
    run-level dry flag and omits None-valued fields."""
    import json
    saved = t._AUDIT
    try:
        t._AUDIT = None
        t._audit('delete', path='x')          # must not raise
        t._audit_end()

        tmp = Path(tempfile.mkdtemp())
        logp = tmp / 'a.jsonl'
        t._AUDIT = t.AuditLog(logp, 'patch', ['patch'], dry=True)
        t._audit('patch', vce='v', src=None, occurrences=3)
        t._audit_end()
        recs = [json.loads(ln) for ln in logp.read_text().splitlines()]
        p = recs[1]
        assert p['event'] == 'patch' and 'src' not in p and p['occurrences'] == 3
        assert p['dry_run'] is True           # inherited from run-level dry
    finally:
        t._AUDIT = saved


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    for fn in tests:
        fn()
        print(f'  PASS  {fn.__name__}')
    print(f'\n{len(tests)} tests passed.')


if __name__ == '__main__':
    main()
