# Prompt Vault Desktop

A Windows desktop app for keeping the text prompts you write for image generators,
together with the picture each one produced, in one encrypted file on your own PC.

**v6.8 · build 2026-11-03** · A Smaller Hammer app. No account, no server, no internet
connection needed once it is built.

## Getting it running

You build the program once from these files; the result runs on any Windows 10 or 11 PC
without Python. The [user guide](docs/PromptVaultDesktop_UserGuide_v3.docx) walks through
every step with screenshots. In short:

1. Install **Python 3.10 or newer** from [python.org](https://www.python.org/downloads/).
   On the installer's first screen, tick **Add python.exe to PATH**.
2. On this page, click the green **Code** button, then **Download ZIP**. Extract it, for
   example to `C:\Dev`. You get a folder called `prompt-vault-desktop-main`.
3. Open that folder in File Explorer, click the address bar, type `cmd` and press Enter.
4. In the Command Prompt window that opens, run:

   ```
   py -m pip install -r requirements.txt
   build_exe.bat
   ```

5. Copy the finished folder `dist\PromptVault` somewhere permanent, such as
   `Documents\Prompt Vault`, and double-click `PromptVault.exe` inside it.

Windows may say *Windows protected your PC* the first time, because the program is not
signed. Click **More info**, then **Run anyway**.

To try it without building, double-click `run.bat` after step 4's first line. That runs
the app straight from these files, with a separate test vault in this folder.

## Features

- Prompts with a negative prompt, reference image, seed, size, tags and notes
- Up to 10 variants per card: versions of one idea kept together, each with its own
  image, browsable on the card and in the image viewer
- Drag one card onto another to combine them, or pin a card to the merge tray, queue
  several cards onto it and add them all at once
- A compact editor: picture, seed, size and notes beside the prompt, with prompt boxes
  that grow as you type
- Several vaults, each with its own passphrase; switch between them, with an encrypted
  backup offered on the way out
- Categories, search across every variant, tag filters, adjustable card size
- One-click copy of any prompt
- Encrypted and plain backup export; import by merging (with a preview of what is new,
  changed or overlapping) or by replacing; three automatic safety copies

## Where your data lives

Everything is in one file, `promptvault.vault`, next to `PromptVault.exe`. It is encrypted
(AES-256-GCM) with your passphrase. Extra vaults you create go in a `vaults` folder beside
it, each with its own passphrase. **The passphrase is never stored and cannot be
recovered.** The whole program folder is portable: copy it to another PC or a USB stick
and it runs there.

The vault is the only copy of your prompts until you export a backup. **Export an
encrypted backup to a second drive regularly.**

To upgrade, build the new version and copy its `PromptVault.exe` and `_internal` folder
over the old ones. Your vaults and settings are not touched.

## Files

| File | Purpose |
|---|---|
| `app.py`, `vault.py`, `ui\` | The application |
| `requirements.txt` | The tools to install before building |
| `build_exe.bat`, `promptvault.spec`, `icon\` | Build `dist\PromptVault\PromptVault.exe` |
| `run.bat` | Run from source without building |
| `docs\` | The user guide |

Tip of the day: To ensure no one ever steals your chocolate, store it in your stomach for safe keeping.
Keep safe and its always that weird purple wire.. Chonky
~~~
 (\(\
 (._.)
c(___)
~~~

## Licence

MIT — see [LICENSE](LICENSE). © 2026 Smaller Hammer.
