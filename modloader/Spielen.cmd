@echo off
rem One click: apply the active mods (if needed) and start the game.
rem Deliberately WITH a console window - applying takes a while and should be visible.
rem First the bundled Python (release package, folder python\), otherwise an installed one.
title WolfSDK - Play
setlocal
cd /d "%~dp0"
if not exist "wolfsdk\__main__.py" goto entpacken
set "PY=python"
if not exist "python\python.exe" goto los
rem Tcl/Python variables from another Python would break the bundled one.
set "TCL_LIBRARY="
set "TK_LIBRARY="
set "PYTHONHOME="
set "PYTHONPATH="
set "PY=python\python.exe"
:los
"%PY%" -m wolfsdk play
if errorlevel 1 pause
exit /b

:entpacken
echo The wolfsdk folder is missing next to this file.
echo Are you running it straight from the ZIP? Then extract the ZIP first:
echo Right-click the ZIP, "Extract All...", then start Spielen.cmd from the extracted folder.
pause
exit /b 1
