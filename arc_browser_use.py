#!/usr/bin/env python3
"""Let Codex use Arc as its browser.

Codex controls a browser through OpenAI's ChatGPT extension. That extension
stalls in Arc, because Arc never answers Chromium's tab-group calls. This tool
downloads the official extension from the Chrome Web Store, verifies its
signature, and writes a copy with two small patches that Arc can load.
"""

import argparse
import base64
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

__version__ = "0.1.0"

EXTENSION_ID = "hehggadaopoacecdllhhajmbjkdcmajg"
NATIVE_HOST = "com.openai.codexextension"
# Extension versions that passed the live smoke test in SETUP.md.
TESTED_VERSIONS = ("1.26.901.11451",)
# Arc 1.167 runs Chromium 154. The store uses this to pick a compatible build.
BROWSER_VERSION = "154.0.0.0"
STORE_URL = "https://clients2.google.com/service/update2/crx"
BUILD_RECORD = "arc-browser-use.json"

HOME = Path.home()
DEFAULT_OUTPUT = HOME / "Library/Application Support/Arc Browser Use/extension"
ARC_APPS = (Path("/Applications/Arc.app"), HOME / "Applications/Arc.app")
ARC_DATA = HOME / "Library/Application Support/Arc/User Data"
# Arc reads native messaging hosts from Chrome's directory, not its own.
CHROME_HOSTS = HOME / "Library/Application Support/Google/Chrome/NativeMessagingHosts"
UNPACKED = 4  # Chromium's extension location value for "Load unpacked"
NODE_CANDIDATES = ("/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node",)


class Patch:
    def __init__(self, name, reason, pattern, replacement, required=True):
        self.name = name
        self.reason = reason
        self.pattern = re.compile(pattern)
        self.replacement = replacement
        self.required = required


# Each pattern must match exactly once in background.js. Anchors use method
# and property names, which survive minification, rather than minified names.
# A build fails when a required patch doesn't apply and skips an optional one.
PATCHES = (
    Patch("skip-tab-groups",
          "Arc never settles tab-group calls, which stalls every tab Codex opens.",
          r"async ensureAgentTabGroup\([^)]*\)\{",
          lambda match: match.group(0) + "return;"),
    Patch("arc-display-name",
          "Codex lists the browser as Arc instead of Chrome.",
          r"family:this\.browserFamily,name:[\w$]+\[this\.browserFamily\]\.shortDisplayName",
          lambda match: 'family:this.browserFamily,name:"Arc"',
          required=False),
)


class SetupError(Exception):
    pass


def _id_letters(hex_digits):
    return "".join(chr(ord("a") + int(c, 16)) for c in hex_digits)


def extension_id(public_key):
    """Chromium derives an extension ID from the SHA-256 of its public key."""
    return _id_letters(hashlib.sha256(public_key).hexdigest()[:32])


def patch_background(text, log=print):
    """Return the patched text and the names of the patches that applied."""
    applied = []
    for patch in PATCHES:
        matches = list(patch.pattern.finditer(text))
        if len(matches) == 1:
            match = matches[0]
            text = text[:match.start()] + patch.replacement(match) + text[match.end():]
            applied.append(patch.name)
            continue
        problem = (f"Patch {patch.name!r} expected one match in background.js and found {len(matches)}. "
                   f"Its purpose: {patch.reason}")
        if patch.required:
            raise SetupError(f"{problem} The extension code has changed, so nothing was built and your "
                             "current build is untouched. See 'When a patch fails' in SETUP.md.")
        log(f"Warning: {problem} Skipped it, because the build works without it.")
    return text, applied


# CRX3 is a small protobuf header followed by a ZIP archive.
# https://chromium.googlesource.com/chromium/src/+/main/components/crx_file/crx3.proto

def _varint(data, index):
    value = shift = 0
    while True:
        if index >= len(data):
            raise SetupError("Truncated CRX header.")
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, index


def _fields(data):
    index = 0
    while index < len(data):
        key, index = _varint(data, index)
        number, wire_type = key >> 3, key & 7
        if wire_type == 0:
            value, index = _varint(data, index)
        elif wire_type == 2:
            length, index = _varint(data, index)
            value, index = data[index:index + length], index + length
            if len(value) != length:
                raise SetupError("Truncated CRX header.")
        else:
            raise SetupError(f"Unsupported CRX header field type {wire_type}.")
        yield number, value


def _verify_rsa(public_key, signature, message):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pem = "\n".join(["-----BEGIN PUBLIC KEY-----",
                         *re.findall(".{1,64}", base64.b64encode(public_key).decode()),
                         "-----END PUBLIC KEY-----", ""])
        (tmp / "key.pem").write_text(pem)
        (tmp / "signature").write_bytes(signature)
        (tmp / "message").write_bytes(message)
        try:
            result = subprocess.run(
                ["openssl", "dgst", "-sha256", "-verify", str(tmp / "key.pem"),
                 "-signature", str(tmp / "signature"), str(tmp / "message")],
                capture_output=True)
        except FileNotFoundError as error:
            raise SetupError("openssl is required to verify the download.") from error
    return result.returncode == 0


def read_crx(data):
    """Return (public_key, zip_bytes) after checking the publisher signature."""
    if len(data) < 12 or data[:4] != b"Cr24":
        raise SetupError("Not a CRX file.")
    version, header_size = struct.unpack("<II", data[4:12])
    if version != 3:
        raise SetupError(f"Unsupported CRX version {version}.")
    header = data[12:12 + header_size]
    archive = data[12 + header_size:]
    proofs, signed_data = [], None
    for number, value in _fields(header):
        if number == 2:  # sha256_with_rsa
            proof = dict(_fields(value))
            proofs.append((proof.get(1, b""), proof.get(2, b"")))
        elif number == 10000:
            signed_data = value
    if signed_data is None:
        raise SetupError("CRX has no signed header data.")
    crx_id = dict(_fields(signed_data)).get(1, b"")
    if _id_letters(crx_id.hex()) != EXTENSION_ID:
        raise SetupError("CRX is not the ChatGPT extension.")
    message = b"CRX3 SignedData\x00" + struct.pack("<I", len(signed_data)) + signed_data + archive
    for public_key, signature in proofs:
        if extension_id(public_key) == EXTENSION_ID:
            if not _verify_rsa(public_key, signature, message):
                raise SetupError("CRX signature check failed.")
            return public_key, archive
    raise SetupError("CRX is not signed by the ChatGPT extension's publisher key.")


def download_crx():
    query = urllib.parse.urlencode({
        "response": "redirect", "acceptformat": "crx3", "prodversion": BROWSER_VERSION,
        "x": urllib.parse.urlencode({"id": EXTENSION_ID, "uc": ""}),
    })
    with urllib.request.urlopen(f"{STORE_URL}?{query}", timeout=60) as response:
        data = response.read()
    if not data.startswith(b"Cr24"):
        raise SetupError("The Chrome Web Store didn't return the extension. If the extension now "
                         f"requires a browser newer than Chromium {BROWSER_VERSION}, raise BROWSER_VERSION.")
    return data


def latest_store_version():
    query = urllib.parse.urlencode({
        "acceptformat": "crx3", "prodversion": BROWSER_VERSION,
        "x": urllib.parse.urlencode({"id": EXTENSION_ID, "v": "0.0.0", "uc": ""}),
    })
    with urllib.request.urlopen(f"{STORE_URL}?{query}", timeout=20) as response:
        found = re.search(rb'<updatecheck [^>]*version="([^"]+)"', response.read())
    return found.group(1).decode() if found else None


def extract_zip(archive, destination):
    try:
        bundle = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as error:
        raise SetupError("The extension archive is damaged.") from error
    with bundle:
        for entry in bundle.infolist():
            path = Path(entry.filename)
            is_symlink = stat.S_ISLNK(entry.external_attr >> 16)
            if path.is_absolute() or ".." in path.parts or is_symlink:
                raise SetupError(f"Unsafe path in extension archive: {entry.filename}")
        bundle.extractall(destination)


def find_node():
    for candidate in (shutil.which("node"), *NODE_CANDIDATES):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def read_record(directory):
    """Return the build record if this tool created directory, else None."""
    try:
        record = json.loads((directory / BUILD_RECORD).read_text())
    except (OSError, ValueError):
        return None
    if isinstance(record, dict) and record.get("tool") == "arc-browser-use" and "extensionVersion" in record:
        return record
    return None


def previous_build(output):
    """Return the backup path for output, refusing one that this tool didn't create."""
    previous = output.with_name(output.name + ".previous")
    if previous.exists() and read_record(previous) is None:
        raise SetupError(f"{previous} exists and was not created by this tool. Move it, then try again.")
    return previous


def unpack(destination, crx=None, log=print):
    """Write the verified, unpatched extension to destination and return its version."""
    if crx:
        data = Path(crx).expanduser().read_bytes()
    else:
        log("Downloading the ChatGPT extension from the Chrome Web Store...")
        data = download_crx()
    public_key, archive = read_crx(data)
    log("Verified the publisher signature.")
    extract_zip(archive, destination)

    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("background", {}).get("service_worker") != "background.js":
        raise SetupError("Unexpected extension layout: no background.js service worker.")
    # The key keeps the official extension ID, which the ChatGPT app's native host requires.
    manifest["key"] = base64.b64encode(public_key).decode()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest.get("version", "unknown")


def fetch(output, crx=None, log=print):
    """Unpack the official extension without patching it, for inspecting a new release."""
    output = Path(output).expanduser().absolute()
    if output.exists():
        raise SetupError(f"{output} already exists. Choose a new directory.")
    output.mkdir(parents=True)
    try:
        version = unpack(output, crx=crx, log=log)
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    log(f"Wrote the unpatched ChatGPT extension {version} to:\n  {output}")
    return version


def build(output, crx=None, log=print):
    """Download, verify, and patch the extension, then replace the build at output."""
    output = Path(output).expanduser().absolute()
    if output.exists() and read_record(output) is None:
        raise SetupError(f"{output} exists and was not created by this tool. Choose another --output.")
    previous = previous_build(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".arc-browser-use-", dir=output.parent))
    try:
        version = unpack(stage, crx=crx, log=log)
        background = stage / "background.js"
        patched, applied = patch_background(background.read_text(encoding="utf-8"), log=log)
        background.write_text(patched, encoding="utf-8")
        node = find_node()
        if node:
            check = subprocess.run([node, "--check", str(background)], capture_output=True, text=True)
            if check.returncode != 0:
                raise SetupError(f"Patched background.js is not valid JavaScript:\n{check.stderr.strip()}")
        else:
            log("Node.js not found; skipped the JavaScript syntax check.")
        # Store verification data does not apply to a modified copy.
        shutil.rmtree(stage / "_metadata", ignore_errors=True)

        record = {
            "tool": "arc-browser-use",
            "toolVersion": __version__,
            "extensionId": EXTENSION_ID,
            "extensionVersion": version,
            "source": str(crx) if crx else "Chrome Web Store",
            "patches": applied,
            "builtAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        }
        (stage / BUILD_RECORD).write_text(json.dumps(record, indent=2) + "\n")

        if output.exists():
            shutil.rmtree(previous, ignore_errors=True)
            output.rename(previous)
        try:
            stage.rename(output)
        except OSError:
            if previous.exists() and not output.exists():
                previous.rename(output)
            raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    log(f"Built ChatGPT extension {version} for Arc at:\n  {output}")
    if version not in TESTED_VERSIONS:
        log(f"Note: version {version} hasn't been tested with this tool. Every patch applied, "
            "but check that Codex can open a page in Arc.")
    return record


def arc_extension_entries():
    """Yield (profile, settings, developer_mode) for each Arc profile that has the extension."""
    if not ARC_DATA.is_dir():
        return
    for profile in sorted(ARC_DATA.iterdir()):
        settings, developer_mode = {}, False
        for name in ("Preferences", "Secure Preferences"):
            try:
                prefs = json.loads((profile / name).read_text())
            except (OSError, ValueError):
                continue
            extensions = prefs.get("extensions", {})
            settings.update(extensions.get("settings", {}).get(EXTENSION_ID, {}))
            developer_mode = developer_mode or extensions.get("ui", {}).get("developer_mode", False)
        if settings:
            yield profile.name, settings, developer_mode


def registry_paths():
    codex_home = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
    return (codex_home / "chrome-native-hosts-v2.json",
            HOME / "Library/Application Support/OpenAI/Codex/chrome-native-hosts-v2.json")


def doctor(output, offline=False, log=print):
    """Print one line per check and return True when every check passes."""
    output = Path(output).expanduser().absolute()
    problems = 0

    def report(ok, message, fix=None):
        nonlocal problems
        log(f"{'ok  ' if ok else 'FAIL'}  {message}")
        if not ok:
            problems += 1
            if fix:
                log(f"      Fix: {fix}")

    report(any(app.is_dir() for app in ARC_APPS), "Arc is installed",
           "Install Arc from https://arc.net.")

    host_file = CHROME_HOSTS / f"{NATIVE_HOST}.json"
    try:
        host = json.loads(host_file.read_text())
        host_ok = (f"chrome-extension://{EXTENSION_ID}/" in host.get("allowed_origins", [])
                   and os.access(host.get("path", ""), os.X_OK))
    except (OSError, ValueError):
        host_ok = False
    report(host_ok, "The Chrome plugin's native connector is installed",
           "In the ChatGPT desktop app, install the Chrome plugin from the plugin list. "
           "Google Chrome itself does not need to be installed.")

    registered = False
    for path in registry_paths():
        try:
            entries = json.loads(path.read_text()).get("entries", [])
        except (OSError, ValueError):
            continue
        registered = registered or any(EXTENSION_ID in entry.get("extensionIds", []) for entry in entries)
    report(registered, "The ChatGPT app has registered itself with the connector",
           "Open the ChatGPT desktop app once.")

    record = read_record(output)
    report(record is not None,
           f"Arc build {record['extensionVersion']} exists at {output}" if record else f"No build at {output}",
           "Run python3 arc_browser_use.py build.")

    entries = list(arc_extension_entries())
    loaded = [(profile, settings, dev) for profile, settings, dev in entries
              if settings.get("location") == UNPACKED and Path(settings.get("path", "")) == output]
    if loaded:
        profile, settings, developer_mode = loaded[0]
        disabled = bool(settings.get("disable_reasons")) or settings.get("state") == 0
        report(not disabled, f"Arc loads the build (profile {profile!r})",
               "In arc://extensions, turn the ChatGPT extension on.")
        report(developer_mode, "Developer mode is on in Arc",
               "In arc://extensions, turn on Developer mode. Arc disables unpacked extensions without it.")
    else:
        steps = [f"remove the ChatGPT extension that profile {profile!r} loads from "
                 f"{settings.get('path') if settings.get('location') == UNPACKED else 'the Chrome Web Store'}"
                 for profile, settings, _ in entries]
        steps.append(f"turn on Developer mode, click Load unpacked, and choose {output}")
        report(False, "Arc loads the build", "In arc://extensions, " + ". Then ".join(steps) + ".")

    if record and not offline:
        try:
            latest = latest_store_version()
        except OSError:
            latest = None
        if latest is None:
            log("skip  Could not check the Chrome Web Store for updates")
        else:
            report(latest == record["extensionVersion"],
                   f"The build is the latest store version ({latest})",
                   "Run python3 arc_browser_use.py build. Then turn the extension off and on in arc://extensions.")

    log("All checks passed." if problems == 0 else f"{problems} check(s) failed.")
    return problems == 0


def uninstall(output, log=print):
    output = Path(output).expanduser().absolute()
    if output.exists() and read_record(output) is None:
        raise SetupError(f"{output} was not created by this tool; leaving it alone.")
    previous = previous_build(output)
    removed = [path for path in (output, previous) if path.exists()]
    for path in removed:
        shutil.rmtree(path)
    log(f"Removed {output}." if removed else f"Nothing to remove at {output}.")
    log("In arc://extensions, remove the ChatGPT extension. "
        "You can reinstall the official one from the Chrome Web Store.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="arc_browser_use.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    build_parser = commands.add_parser("build", help="download, verify, and patch the extension")
    build_parser.add_argument("--output", default=DEFAULT_OUTPUT, help="where to write the extension")
    fetch_parser = commands.add_parser("fetch", help="download and verify the extension without patching it")
    fetch_parser.add_argument("output", help="a new directory for the unmodified extension")
    for sub in (build_parser, fetch_parser):
        sub.add_argument("--crx", help="use a downloaded .crx file instead of the Chrome Web Store")

    doctor_parser = commands.add_parser("doctor", help="check that everything is in place")
    doctor_parser.add_argument("--output", default=DEFAULT_OUTPUT, help="where the extension was built")
    doctor_parser.add_argument("--offline", action="store_true", help="skip the store update check")

    uninstall_parser = commands.add_parser("uninstall", help="delete the built extension")
    uninstall_parser.add_argument("--output", default=DEFAULT_OUTPUT, help="where the extension was built")

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            build(args.output, crx=args.crx)
            print("Next, in arc://extensions: on a first install, click Load unpacked and choose that folder. "
                  "On an update, turn the ChatGPT extension off and on.")
            return 0
        if args.command == "fetch":
            fetch(args.output, crx=args.crx)
            return 0
        if args.command == "doctor":
            return 0 if doctor(args.output, offline=args.offline) else 1
        uninstall(args.output)
        return 0
    except (SetupError, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
