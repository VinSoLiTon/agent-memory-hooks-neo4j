@echo off
rem njhook dashboard launcher — http://localhost:5000 by default.
rem Forwards args to app.py so e.g. `dashboard.cmd --port 5050` works.
python "%~dp0app.py" %*
