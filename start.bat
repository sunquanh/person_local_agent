@echo off
rem ============================================================
rem  Local Agent Workbench - one click launcher
rem  All user-facing prompts are printed by start.py (UTF-8),
rem  so this file is kept ASCII-only on purpose.
rem ============================================================

chcp 65001 >nul 2>nul
cd /d "%~dp0"
title Local Agent Workbench

rem keep the project folder clean: no __pycache__ / *.pyc written
set PYTHONDONTWRITEBYTECODE=1

rem ---- 1) portable Python bundled next to this file -------------
if exist "%~dp0python\python.exe" goto :run_portable

rem ---- 2) py launcher (never hits the Store stub) ---------------
where py >nul 2>nul
if errorlevel 1 goto :try_installdir
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 goto :run_py

rem ---- 2.5) installed but not on PATH --------------------------
:try_installdir
set "CAND="
for /f "delims=" %%D in ('dir /b /ad "%LOCALAPPDATA%\Programs\Python\Python3*" 2^>nul') do (
  if not defined CAND if exist "%LOCALAPPDATA%\Programs\Python\%%D\python.exe" set "CAND=%LOCALAPPDATA%\Programs\Python\%%D\python.exe"
)
if defined CAND goto :run_cand

rem ---- 3) python on PATH ---------------------------------------
:try_python
where python >nul 2>nul
if errorlevel 1 goto :try_python3
python -c "import sys" >nul 2>nul
if not errorlevel 1 goto :run_python

rem ---- 4) python3 on PATH --------------------------------------
:try_python3
where python3 >nul 2>nul
if errorlevel 1 goto :nopython
python3 -c "import sys" >nul 2>nul
if not errorlevel 1 goto :run_python3

goto :nopython


:run_portable
"%~dp0python\python.exe" "%~dp0start.py" %*
goto :done

:run_cand
"%CAND%" "%~dp0start.py" %*
goto :done

:run_py
py -3 "%~dp0start.py" %*
goto :done

:run_python
python "%~dp0start.py" %*
goto :done

:run_python3
python3 "%~dp0start.py" %*
goto :done


:done
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo  ------------------------------------------------------------
  echo   [X] Stopped with exit code %RC%
  echo   Read the messages above for the reason.
  echo  ------------------------------------------------------------
  echo.
  pause
)
exit /b %RC%


:nopython
echo.
echo  ============================================================
echo   Python not found
echo  ============================================================
echo   This workbench needs Python 3.9 or newer, but none was found
echo   on this machine.
echo.
echo   Option A - install Python from python.org
echo              remember to tick "Add python.exe to PATH"
echo.
echo   Option B - portable, no install needed (best for offline PC):
echo              unzip a Python embeddable package into
echo                  %~dp0python\
echo              so that this file exists:
echo                  %~dp0python\python.exe
echo              then run this launcher again.
echo  ============================================================
echo.
pause
exit /b 1
