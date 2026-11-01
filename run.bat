@echo off
REM Prompt Vault (desktop) - run from source.
REM First time only:  py -m pip install -r requirements.txt
py "%~dp0app.py" %*
