@echo off
rem Double-click launcher for the loader window.
rem First the bundled Python (release package, folder python\), otherwise an installed one.
rem The window runs under pythonw, otherwise a black console hangs behind it. But pythonw
rem swallows every error. So first a short test start with a console (import and
rem Tk): if it fails, the error is shown here and in %LOCALAPPDATA%\wolfsdk\start.log.
setlocal
cd /d "%~dp0"
if not exist "wolfsdk\__main__.py" goto entpacken
set "LOG=%LOCALAPPDATA%\wolfsdk\start.log"
if not exist "%LOCALAPPDATA%\wolfsdk\" mkdir "%LOCALAPPDATA%\wolfsdk"

if not exist "python\pythonw.exe" goto installiert
rem Tcl/Python variables from another Python would break the bundled one.
set "TCL_LIBRARY="
set "TK_LIBRARY="
set "PYTHONHOME="
set "PYTHONPATH="
set "PY=python\python.exe"
set "PYW=python\pythonw.exe"
goto probe

:installiert
set "PYW="
for %%p in (pythonw.exe) do set "PYW=%%~$PATH:p"
if not defined PYW goto launcher
for %%i in ("%PYW%") do set "PY=%%~dpipython.exe"
goto probe
:launcher
for %%p in (pyw.exe) do set "PYW=%%~$PATH:p"
if not defined PYW goto konsole
for %%i in ("%PYW%") do set "PY=%%~dpipy.exe"
goto probe

:konsole
where python.exe >nul 2>&1 || goto keinpython
echo pythonw was not found - starting with a console window.
python -m wolfsdk gui
if errorlevel 1 pause
exit /b

:probe
"%PY%" -c "import tkinter, wolfsdk.cli, wolfsdk.gui; r = tkinter.Tk(); r.withdraw(); r.destroy()" >"%LOG%" 2>&1
if errorlevel 1 goto fehler
start "" "%PYW%" -m wolfsdk gui
exit /b

:fehler
type "%LOG%"
echo.
echo The loader could not start. The error is shown above and in:
echo   "%LOG%"
pause
exit /b 1

:entpacken
echo The wolfsdk folder is missing next to this file.
echo Are you running it straight from the ZIP? Then extract the ZIP first:
echo Right-click the ZIP, "Extract All...", then start WolfSDK.cmd from the extracted folder.
pause
exit /b 1

:keinpython
echo No Python found. The release package WolfSDK-*.zip comes with its own (folder python\).
pause
exit /b 1
