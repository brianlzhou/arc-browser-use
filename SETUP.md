# Set up Arc for Codex browser use

These instructions are for an AI coding agent, such as Codex or Claude Code, that runs commands on the user's Mac. A person can follow them too. [README.md](README.md) explains what the project does.

Work through the sections in order. Sections marked **(user)** need the user to click in Arc. For those, send the user the quoted text, wait for their reply, and then continue. Run every command from the repository root.

Terms used here:

- **The Chrome plugin** is the plugin in the ChatGPT desktop app that gives Codex browser use.
- **The native connector** is the program that the Chrome plugin installs. The browser extension talks to the ChatGPT app through it.
- **The ChatGPT extension** is OpenAI's browser extension.
- **The build** is the patched copy of the ChatGPT extension that this tool writes. **The build folder** is `~/Library/Application Support/Arc Browser Use/extension`.

## 1. Get the tool

If the repository isn't on this Mac yet, clone it:

```sh
git clone https://github.com/brianlzhou/arc-browser-use.git ~/arc-browser-use
cd ~/arc-browser-use
```

If it's already there, `cd` into it and run `git pull`.

## 2. Check the prerequisites

```sh
python3 arc_browser_use.py doctor --offline
```

The first three lines must start with `ok` before you continue. For each one that starts with `FAIL`, do this:

| Line | What to do |
| --- | --- |
| `Arc is installed` | Ask the user to install Arc from https://arc.net. |
| `The Chrome plugin's native connector is installed` | Ask the user to install the **Chrome** plugin from the ChatGPT desktop app's plugin list. Google Chrome itself isn't needed. |
| `The ChatGPT app has registered itself with the connector` | Ask the user to open the ChatGPT desktop app once. |

Then run `doctor --offline` again.

The remaining lines are about the build and Arc. On a first install they fail at this point, which is expected. Note whether `Arc loads the build` starts with `ok`. You need that in the section [Load the build in Arc](#4-load-the-build-in-arc-user).

## 3. Build

```sh
python3 arc_browser_use.py build
```

On success, the output ends with the extension version and the build folder.

- If the output contains `Warning: Patch 'arc-display-name'`, the build still works, but Codex will list Arc as "Chrome". Continue, and tell the user about it at the end.
- If the output says `Error: Patch 'skip-tab-groups' expected one match`, go to [When a patch fails](#when-a-patch-fails).
- If the output says `hasn't been tested with this tool`, continue. Take extra care in [Verify](#5-verify).

## 4. Load the build in Arc (user)

If `Arc loads the build` started with `ok` in the section [Check the prerequisites](#2-check-the-prerequisites), you are updating. Send the user this:

> In Arc, open `arc://extensions` and turn the **ChatGPT** extension off and then on again. Don't use the Reload button: it doesn't reconnect to the ChatGPT app. Tell me when it's done.

Otherwise this is a first install. Send the user this:

> In Arc, open `arc://extensions`.
>
> 1. Turn on **Developer mode** at the top right.
> 2. If a **ChatGPT** extension is listed, click **Remove** on it.
> 3. Click **Load unpacked**. Press **⇧⌘G**, paste `~/Library/Application Support/Arc Browser Use/extension`, press Return, and click **Select**.
>
> Tell me when it's done.

## 5. Verify

```sh
python3 arc_browser_use.py doctor
```

Every line must start with `ok`. If one starts with `FAIL`, do what its `Fix:` line says, and then run `doctor` again.

Then confirm that Codex can control Arc:

- **If you are Codex,** use your browser tool. Select the browser named **Arc**. Open `https://example.com` in a new tab, read the page title, and then list the user's open tabs. Each action must finish within a few seconds.
- **Otherwise,** ask the user to send this to Codex: *"Use my Arc browser to open https://example.com and tell me the page title."*

The title is **Example Domain**. If Codex doesn't list Arc, or the tab never finishes loading, see [Troubleshooting](#troubleshooting).

When it works, tell the user that setup is complete and that Developer mode must stay on in Arc.

## Updating

Update when `doctor` reports `FAIL  The build is the latest store version`. Codex might also say "Please update the ChatGPT extension in Google Chrome to the latest version". Codex shows that message whenever the extension rejects a tab request, so it doesn't always mean an update exists. Run `doctor` to check.

To update, follow [Get the tool](#1-get-the-tool), [Build](#3-build), [Load the build in Arc](#4-load-the-build-in-arc-user) and [Verify](#5-verify).

## When a patch fails

`build` refuses to write a build in which a required patch didn't apply. The user's existing build folder is left as it was, so Arc keeps working with the older version.

1. Run `git pull`, and then run `build` again. The fix might already be in this repository.

2. Write the new, unpatched extension to a scratch folder:

   ```sh
   python3 arc_browser_use.py fetch /tmp/chatgpt-extension-new
   ```

3. Find the code that the failed patch targets. `background.js` is minified onto a few very long lines, so search with some context:

   ```sh
   grep -o '.\{0,200\}ensureAgentTabGroup.\{0,200\}' /tmp/chatgpt-extension-new/background.js
   ```

   | Patch | What to look for | What the patch must do |
   | --- | --- | --- |
   | `skip-tab-groups` | The method of the tab-group manager that puts a tab Codex opened into a group. It is near `storageKey="TAB_GROUPS"` and calls `chrome.tabs.group(`. | Return before the method creates or joins a group. |
   | `arc-display-name` | The browser information object that contains `family:this.browserFamily,name:`. | Set `name` to `"Arc"`. |

4. In `arc_browser_use.py`, update that patch's `pattern` and `replacement` in `PATCHES`. Anchor the pattern on method names, property names and string literals. Don't anchor it on one- or two-letter names, because the minifier renames those in every release. The pattern must match exactly once.

5. Check whether a Codex action now waits on a different tab-group call:

   ```sh
   grep -o '.\{0,120\}\(chrome\.tabs\.group\|chrome\.tabs\.ungroup\|chrome\.tabGroups\.\)[a-zA-Z]*(.\{0,120\}' /tmp/chatgpt-extension-new/background.js
   ```

   Some calls are safe: a call inside a `try` block whose `catch` ignores errors, or a call that runs only when a tab's `groupId` is set. Arc tabs never belong to a group. A call that is awaited while Codex opens, claims or lists tabs needs a new patch.

6. If the shape of the code changed, update the `BACKGROUND` sample in `tests/test_arc_browser_use.py` to match. Then run the tests:

   ```sh
   python3 -m unittest discover -s tests
   ```

7. Run `build`, and then follow [Load the build in Arc](#4-load-the-build-in-arc-user) and [Verify](#5-verify).

8. Record the result. Add the new version to `TESTED_VERSIONS` in `arc_browser_use.py`, and add a `CHANGELOG.md` entry that names it.

9. Send the fix to this repository so that other users get it. If you can open a pull request, open one. Otherwise, give the user the output of `git diff` and the address https://github.com/brianlzhou/arc-browser-use/issues.

   Your local changes make later `git pull` commands conflict. After the fix is in this repository, run `git stash`, and then `git pull`.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Codex doesn't list a browser named Arc or Chrome. | The ChatGPT extension is off or isn't connected. Run `doctor`. Ask the user to turn the extension off and on in `arc://extensions`, and then to quit and reopen the ChatGPT app. |
| Codex lists Arc as "Chrome", and opening a tab never finishes. | Arc is running the unpatched store extension, not the build. Check the `Arc loads the build` line of `doctor`, and then repeat [Load the build in Arc](#4-load-the-build-in-arc-user). |
| Codex lists Arc as "Chrome", but tabs work. | The optional `arc-display-name` patch didn't apply. Nothing is broken. See [When a patch fails](#when-a-patch-fails) to restore the name. |
| Arc shows a warning about developer mode extensions, or it turned the extension off. | Developer mode is off. Ask the user to turn it on and to turn the extension on again. |
| `build` says `openssl is required`. | macOS includes `/usr/bin/openssl`. Make sure that `/usr/bin` is in `PATH`. |
| `build` says `The Chrome Web Store didn't return the extension`. | The extension might now need a newer Chromium version than `BROWSER_VERSION` in `arc_browser_use.py`. Arc's Chromium version is shown on `arc://version`. |

## Uninstall

```sh
python3 arc_browser_use.py uninstall
```

Then send the user this:

> In Arc, open `arc://extensions` and click **Remove** on the **ChatGPT** extension. To go back to the official extension, install it from the Chrome Web Store.
