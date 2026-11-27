@echo off
REM ============================================================================
REM  tbc_tools - example raster consolidation workflow (Windows)
REM
REM  Copy this to run_consolidation.bat and edit SCAN_DIRS for your setup.
REM  The library path, VCE roots, path mappings, and stale_lock_days all come
REM  from tbc_tools.toml - this script only orchestrates the commands.
REM
REM  Run it from the repo folder:  run_consolidation.bat
REM
REM  Safe by design: it scans, plans, previews, then PAUSES before changing
REM  anything, moves + patches with per-VCE backups, and never deletes.
REM  The space-reclaiming delete pass is left as a manual step at the end.
REM ============================================================================
setlocal EnableExtensions

REM ---- Settings (edit these) -------------------------------------------------
REM Raster directories to scan. Include the working-data root AND the library
REM so files already in the library are recognised as canonical. Quote paths
REM containing spaces; separate multiple directories with spaces.
set SCAN_DIRS="S:\Working Data" "S:\GIS\Basemaps"

REM Stale-lock threshold in days. Lock files older than this (orphaned by a
REM crashed TBC) are cleared before planning. Matches stale_lock_days in
REM tbc_tools.toml; 7 is the default.
set STALE_DAYS=7

REM Working files (all gitignored).
set DUPES=wd_dupes.json
set PLAN=wd_plan.json
set REPORT=report.md
REM ---------------------------------------------------------------------------

REM Optional: preview which stale locks would be cleared, without deleting:
REM   uv run tbc_tools.py clear-locks --dry-run --days %STALE_DAYS%

echo(
echo === [1/4] Scanning for duplicate rasters ===
uv run tbc_tools.py scan %SCAN_DIRS% --consolidate --out "%DUPES%"
if errorlevel 1 goto :error

echo(
echo === [2/4] Planning (clears stale locks older than %STALE_DAYS%d; ===
echo ===        projects open in TBC are deferred and never touched)  ===
uv run tbc_tools.py plan "%DUPES%" --clear-stale-locks --stale-lock-days %STALE_DAYS% --out "%PLAN%"
if errorlevel 1 goto :error

echo(
echo === [3/4] Writing preview report ===
uv run tbc_tools.py report "%PLAN%" --out "%REPORT%"
if errorlevel 1 goto :error

echo(
echo Review %REPORT% now - reclaimable space, locked projects, per-VCE changes.
echo Press any key to MOVE rasters and PATCH VCEs (each VCE is backed up first),
echo or press Ctrl+C / close this window to stop without changing anything.
pause >nul

echo(
echo === [4/4] Executing: move canonical rasters + patch VCEs (no deletion) ===
uv run tbc_tools.py execute "%PLAN%"
if errorlevel 1 goto :error

echo(
echo Move + patch complete. Nothing was deleted.
echo Open a few projects in TBC to confirm the imagery resolves from the library.
echo When you are satisfied, reclaim the space by deleting the duplicates:
echo(
echo     uv run tbc_tools.py execute "%PLAN%" --delete-originals
echo(
echo (Left as a manual step on purpose - deletion is permanent.)
goto :eof

:error
echo(
echo *** A step failed (errorlevel %errorlevel%). Stopping; nothing further runs.
exit /b %errorlevel%
