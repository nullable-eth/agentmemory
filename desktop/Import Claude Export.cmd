@echo off
REM ============================================================================
REM  Import Claude Export.cmd
REM
REM  Double-click me.
REM
REM  New-style export (manifest json): run download_export.py first (or click
REM  the manifest's links yourself) so the zips land in the vault's .imports\
REM  folder -- then double-click me and everything in .imports is staged,
REM  merged, rendered into .staging\ for filing approval, and the zips are
REM  archived to .imports\archive\ so the inbox empties.
REM
REM  You can also drag zips straight onto this file. Multi-part exports
REM  (batch-0000, batch-0001, or the new per-category zips): all parts at
REM  once; they get merged.
REM
REM  Safe to run twice on the same zip -- the second run does nothing.
REM
REM  To preview without writing anything, run from a terminal:
REM      "Import Claude Export.cmd" --dry-run
REM ============================================================================

setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set SCRIPT=%~dp0import_export.py

if not exist "%SCRIPT%" (
    echo.
    echo !! Can't find import_export.py next to this file.
    echo    Expected: %SCRIPT%
    echo.
    pause
    exit /b 1
)

py -3 "%SCRIPT%" %*
set RC=%ERRORLEVEL%

echo.
if %RC% NEQ 0 (
    echo ============================================================================
    echo  FAILED ^(exit code %RC%^). Nothing was filed away; your zip is untouched.
    echo ============================================================================
)
pause
exit /b %RC%
