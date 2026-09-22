@echo off
setlocal

rem Build Ubic.exe. Safe to double-click from Explorer.

rem Explorer starts batch files in C:\Windows\System32, so move to this file's
rem own folder or every relative path below resolves against the wrong place.
cd /d "%~dp0"

echo ============================================
echo  Building Ubic
echo ============================================
echo.

rem pip installs pyinstaller.exe into Python's Scripts folder, which is often
rem not on PATH. Invoking it as a module uses the interpreter on PATH instead,
rem which sidesteps "pyinstaller is not recognized" entirely.
set "PY="
where python >nul 2>&1 && set "PY=python"
if defined PY goto :found_python
where py >nul 2>&1 && set "PY=py"
:found_python

if not defined PY (
    echo ERROR: Neither "python" nor "py" was found on PATH.
    echo Install Python from python.org, ticking "Add Python to PATH".
    goto :end
)
echo Interpreter: %PY%

if not exist "ubic_tracker.py" (
    echo ERROR: ubic_tracker.py not found in %CD%.
    goto :end
)

if not exist "ubic_icon.ico" (
    echo ERROR: ubic_icon.ico not found in %CD%.
    echo The build references it twice and PyInstaller will refuse to start.
    goto :end
)

rem Regenerate the Windows version resource from UBIC_VERSION so the exe
rem metadata can never drift from what the app reports. Without it, Task
rem Manager and Startup Apps fall back to showing "Ubic.exe".
set "VERFLAG="
if exist "make_version_info.py" (
    %PY% make_version_info.py
    if errorlevel 1 (
        echo ERROR: Could not generate version_info.txt.
        goto :end
    )
)
if exist "version_info.txt" (
    set "VERFLAG=--version-file=version_info.txt"
) else (
    echo WARNING: version_info.txt not found. The exe will appear as
    echo          "Ubic.exe" rather than "Ubic" in Task Manager.
)

%PY% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not installed. Installing it now...
    %PY% -m pip install pyinstaller
    if errorlevel 1 (
        echo ERROR: Could not install PyInstaller.
        goto :end
    )
)

echo.
echo Building...
echo.

%PY% -m PyInstaller --noconfirm --clean ^
    --onefile ^
    --windowed ^
    --name Ubic ^
    --icon=ubic_icon.ico ^
    --add-data "ubic_icon.ico;." ^
    %VERFLAG% ^
    ubic_tracker.py

if errorlevel 1 (
    echo.
    echo ============================================
    echo  BUILD FAILED - see the output above.
    echo ============================================
    echo.
    echo If it could not write dist\Ubic.exe, quit Ubic from its
    echo tray icon first: a running exe cannot be overwritten.
    goto :end
)

echo.
echo ============================================
echo  Done: %CD%\dist\Ubic.exe
echo ============================================
echo.
echo Copy it to a folder of its own - it writes ubic_config.json
echo and ubic_debug.log alongside itself.

:end
echo.
pause
endlocal
