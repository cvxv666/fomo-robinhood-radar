@echo off
rem One-time authorisation of the radar's X app for the bot account. Asks for the two values
rem from the developer console, then hands over to scripts/x_auth.py, which prints one link.
rem The repo path is fixed so this file can be copied anywhere (the Desktop, say).
setlocal
cd /d "C:\Users\Honor\Desktop\FomoRadar"
if not exist ".venv\Scripts\python.exe" (
  echo  Cannot find the repo's Python at C:\Users\Honor\Desktop\FomoRadar\.venv
  pause
  exit /b 1
)
echo.
echo  FOMO Radar - X authorisation
echo  ---------------------------
echo  Paste the two values from the X developer console (they stay on this machine).
echo.
set /p X_CLIENT_ID=  Client ID:
set /p X_CLIENT_SECRET=  Client Secret:
if "%X_CLIENT_ID%"=="" (
  echo  No Client ID - nothing to do.
  pause
  exit /b 1
)
echo.
".venv\Scripts\python.exe" scripts\x_auth.py
echo.
pause
