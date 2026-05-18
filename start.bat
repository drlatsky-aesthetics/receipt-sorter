@echo off
title Receipt Sorter
echo.
echo  Starting Receipt Sorter...
echo  Opening browser at http://localhost:5000
echo  Close this window to stop the app.
echo.

:: Install dependencies if needed
pip install -r requirements.txt -q

:: Open browser after 3 seconds
timeout /t 3 /nobreak >nul
start http://localhost:5000

:: Start the app
python app.py

pause
