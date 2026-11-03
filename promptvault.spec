# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Prompt Vault (desktop).
#
# ONE-FOLDER build (v5.8; v5.0-v5.7 were one-file). Output:
#
#   dist\PromptVault\
#     PromptVault.exe        <- run this
#     _internal\             <- Python runtime, pywebview, ui\  (sys._MEIPASS = BASE_DIR)
#
# Why one-folder: a one-file exe unpacks itself to %TEMP% on EVERY launch
# (~1-3 s, longer while Defender scans the extract) and self-extracting
# executables trip antivirus heuristics more often. One-folder starts
# immediately. The cost is carrying a folder instead of a single exe.
#
# Data still lives beside the exe (HOME_DIR in app.py), never in _internal\:
# the vault, settings, .1/.2/.3 generations and lock file sit next to
# PromptVault.exe. Upgrading = replace PromptVault.exe and _internal\ only.
#
# WARNING: PyInstaller empties dist\PromptVault\ on every build. Never run the
# app from there - copy the folder elsewhere first. build_exe.bat refuses to
# build if it finds a vault in that folder.

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('ui/index.html', 'ui'), ('ui/seed.json', 'ui')],
    hiddenimports=['webview.platforms.edgechromium', 'webview.platforms.winforms'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'PIL'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,  # binaries + datas go to COLLECT below (one-folder)
    name='PromptVault',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # no console window
    disable_windowed_traceback=False,
    icon='icon/promptvault.ico',   # also the window/taskbar icon: pywebview takes it from the exe
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False,
    upx=False,
    name='PromptVault',     # -> dist\PromptVault\
)
