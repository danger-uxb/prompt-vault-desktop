@echo off
REM Builds the ONE-FOLDER app into dist\PromptVault\  (v5.8+)
REM   dist\PromptVault\PromptVault.exe  +  dist\PromptVault\_internal\
REM The vault, settings and backups live NEXT TO PromptVault.exe, never in _internal\.
setlocal
cd /d "%~dp0"

REM PyInstaller empties dist\PromptVault\ on every build. If a vault is in
REM there, someone has been running the app from the build folder - stop
REM rather than delete it.
if exist "dist\PromptVault\promptvault.vault" goto :guard
if exist "dist\PromptVault\promptvault-settings.json" goto :guard
if exist "dist\PromptVault\vaults\" goto :guard

py -m PyInstaller --noconfirm --clean promptvault.spec
if errorlevel 1 (
  echo.
  echo BUILD FAILED - see the messages above.
  exit /b 1
)
echo.
echo Built dist\PromptVault\  (PromptVault.exe + _internal\)
echo.
echo  New install : copy the whole dist\PromptVault folder somewhere permanent.
echo                That folder becomes the vault's home.
echo  Upgrade     : in your existing Prompt Vault folder, replace PromptVault.exe
echo                and the _internal folder. Leave promptvault.vault, .vault.1-3,
echo                the vaults folder and promptvault-settings.json alone.
exit /b 0

:guard
echo.
echo STOPPED: dist\PromptVault\ contains a vault, a vaults folder or settings.
echo A build would delete it. Move that folder somewhere permanent first,
echo then run this again.
exit /b 1
