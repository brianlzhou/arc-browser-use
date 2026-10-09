import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("arc_browser_use", ROOT / "arc_browser_use.py")
acu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acu)

# A minified-style excerpt with every patch anchor, in the shape the real
# background.js uses. Identifiers like Kt and Se change between releases.
BACKGROUND = (
    "var df=class Kt{async ensureAgentTabGroup(t,r,n){await this.ensureInit();await this.createGroup(r)}"
    "async ensureInit(){}async createGroup(t){await chrome.tabs.group({tabIds:[t]})}};"
    "var Se={chrome:{shortDisplayName:\"Chrome\"}};"
    "class Ab{getInfo(){return{family:this.browserFamily,name:Se[this.browserFamily].shortDisplayName,version:1}}}"
)
HAS_OPENSSL = shutil.which("openssl") is not None


def varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def field(number, payload):
    return varint(number << 3 | 2) + varint(len(payload)) + payload


def zip_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data in files.items():
            bundle.writestr(name, data)
    return buffer.getvalue()


def extension_files(background=BACKGROUND):
    manifest = {"manifest_version": 3, "name": "ChatGPT", "version": "9.9.9",
                "background": {"service_worker": "background.js"}}
    return {"manifest.json": json.dumps(manifest), "background.js": background,
            "_metadata/verified_contents.json": "{}"}


class Signer:
    """An RSA key that signs CRX3 files the way the Chrome Web Store does."""

    def __init__(self, directory):
        self.private = Path(directory) / "key.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(self.private), "2048"], check=True, capture_output=True)
        self.public = subprocess.run(["openssl", "rsa", "-in", str(self.private), "-pubout", "-outform", "DER"],
                                     check=True, capture_output=True).stdout
        self.id = acu.extension_id(self.public)

    def sign(self, message):
        return subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(self.private)],
                              input=message, check=True, capture_output=True).stdout

    def crx(self, files, tamper=False):
        archive = zip_bytes(files)
        crx_id = hashlib.sha256(self.public).digest()[:16]
        signed_data = field(1, crx_id)
        message = b"CRX3 SignedData\x00" + struct.pack("<I", len(signed_data)) + signed_data + archive
        proof = field(1, self.public) + field(2, self.sign(message))
        header = field(2, proof) + field(10000, signed_data)
        if tamper:
            archive = archive.replace(b"ChatGPT", b"ChatGPU")
        return b"Cr24" + struct.pack("<II", 3, len(header)) + header + archive


def patch(source):
    warnings = []
    text, applied = acu.patch_background(source, log=warnings.append)
    return text, applied, warnings


class PatchTests(unittest.TestCase):
    def test_applies_every_patch(self):
        patched, applied, warnings = patch(BACKGROUND)
        self.assertIn("async ensureAgentTabGroup(t,r,n){return;await this.ensureInit()", patched)
        self.assertIn('family:this.browserFamily,name:"Arc",version:1', patched)
        self.assertEqual(applied, [p.name for p in acu.PATCHES])
        self.assertEqual(warnings, [])

    def test_refuses_a_missing_required_anchor(self):
        source = BACKGROUND.replace("ensureAgentTabGroup", "ensureGroup")
        with self.assertRaisesRegex(acu.SetupError, "skip-tab-groups.*found 0"):
            patch(source)

    def test_refuses_a_repeated_required_anchor(self):
        with self.assertRaisesRegex(acu.SetupError, "found 2"):
            patch(BACKGROUND + BACKGROUND.replace("shortDisplayName,version", "x,version"))

    def test_skips_an_optional_patch_with_a_warning(self):
        patched, applied, warnings = patch(BACKGROUND.replace("family:this.browserFamily,", ""))
        self.assertEqual(applied, ["skip-tab-groups"])
        self.assertIn("ensureAgentTabGroup(t,r,n){return;", patched)
        self.assertRegex(warnings[0], "arc-display-name.*Skipped")

    def test_anchors_ignore_minified_names(self):
        source = BACKGROUND.replace("Se[", "Q$[").replace("(t,r,n)", "(a,b,c)")
        patched, _, _ = patch(source)
        self.assertIn("ensureAgentTabGroup(a,b,c){return;", patched)
        self.assertIn('name:"Arc"', patched)


@unittest.skipUnless(HAS_OPENSSL, "openssl is required")
class CrxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.signer = Signer(cls.tmp.name)
        other = Path(cls.tmp.name) / "other"
        other.mkdir()
        cls.other = Signer(other)  # an unrelated publisher

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        patcher = mock.patch.object(acu, "EXTENSION_ID", self.signer.id)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_accepts_a_signed_crx(self):
        key, archive = acu.read_crx(self.signer.crx(extension_files()))
        self.assertEqual(key, self.signer.public)
        self.assertIn("background.js", zipfile.ZipFile(io.BytesIO(archive)).namelist())

    def test_rejects_a_modified_archive(self):
        with self.assertRaisesRegex(acu.SetupError, "signature check failed"):
            acu.read_crx(self.signer.crx(extension_files(), tamper=True))

    def test_rejects_another_extension(self):
        with self.assertRaisesRegex(acu.SetupError, "not the ChatGPT extension"):
            acu.read_crx(self.other.crx(extension_files()))

    def test_rejects_non_crx_data(self):
        with self.assertRaisesRegex(acu.SetupError, "Not a CRX"):
            acu.read_crx(zip_bytes(extension_files()))


@unittest.skipUnless(HAS_OPENSSL, "openssl is required")
class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.signer = Signer(cls.keys.name)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        patcher = mock.patch.object(acu, "EXTENSION_ID", self.signer.id)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.crx = self.tmp / "extension.crx"
        self.crx.write_bytes(self.signer.crx(extension_files()))
        self.output = self.tmp / "out" / "extension"

    def build(self, **kwargs):
        kwargs.setdefault("crx", self.crx)
        return acu.build(self.output, log=lambda *_: None, **kwargs)

    def test_writes_a_patched_extension_with_the_official_key(self):
        record = self.build()
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(base64.b64decode(manifest["key"]), self.signer.public)
        self.assertIn('name:"Arc"', (self.output / "background.js").read_text())
        self.assertFalse((self.output / "_metadata").exists())
        self.assertEqual(record["extensionVersion"], "9.9.9")
        self.assertEqual(json.loads((self.output / acu.BUILD_RECORD).read_text())["patches"],
                         [patch.name for patch in acu.PATCHES])

    def test_rebuild_keeps_the_previous_build(self):
        self.build()
        self.build()
        self.assertTrue((self.output / "background.js").exists())
        self.assertTrue(self.output.with_name("extension.previous").is_dir())
        self.assertEqual([p.name for p in self.output.parent.iterdir() if p.name.startswith(".")], [])

    def test_refuses_to_delete_a_previous_folder_it_did_not_create(self):
        self.build()
        foreign = self.output.with_name("extension.previous")
        foreign.mkdir()
        (foreign / "notes.txt").write_text("keep me")
        for action in (self.build, lambda: acu.uninstall(self.output, log=lambda *_: None)):
            with self.assertRaisesRegex(acu.SetupError, "not created by this tool"):
                action()
        self.assertEqual((foreign / "notes.txt").read_text(), "keep me")
        self.assertTrue((self.output / "background.js").exists())

    def test_refuses_to_replace_a_directory_it_did_not_create(self):
        self.output.mkdir(parents=True)
        (self.output / "notes.txt").write_text("keep me")
        with self.assertRaisesRegex(acu.SetupError, "not created by this tool"):
            self.build()
        self.assertEqual((self.output / "notes.txt").read_text(), "keep me")

    def test_failed_patch_leaves_the_existing_build(self):
        self.build()
        self.crx.write_bytes(self.signer.crx(extension_files(BACKGROUND.replace("ensureAgentTabGroup", "x"))))
        with self.assertRaises(acu.SetupError):
            self.build()
        self.assertIn('name:"Arc"', (self.output / "background.js").read_text())

    def test_fetch_writes_the_unpatched_extension(self):
        target = self.tmp / "upstream"
        self.assertEqual(acu.fetch(target, crx=self.crx, log=lambda *_: None), "9.9.9")
        self.assertEqual((target / "background.js").read_text(), BACKGROUND)
        self.assertIn("key", json.loads((target / "manifest.json").read_text()))
        with self.assertRaisesRegex(acu.SetupError, "already exists"):
            acu.fetch(target, crx=self.crx, log=lambda *_: None)

    def test_rejects_archive_paths_outside_the_extension(self):
        with self.assertRaisesRegex(acu.SetupError, "Unsafe path"):
            acu.extract_zip(zip_bytes({"../escape.js": "x"}), self.tmp / "unzipped")

    def test_uninstall_removes_only_its_own_builds(self):
        self.build()
        self.build()
        acu.uninstall(self.output, log=lambda *_: None)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_name("extension.previous").exists())
        self.output.mkdir()
        with self.assertRaises(acu.SetupError):
            acu.uninstall(self.output, log=lambda *_: None)


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)
        self.output = self.home / "build" / "extension"
        self.output.mkdir(parents=True)
        (self.output / acu.BUILD_RECORD).write_text(json.dumps(
            {"tool": "arc-browser-use", "extensionVersion": "9.9.9"}))
        arc_app = self.home / "Applications/Arc.app"
        arc_app.mkdir(parents=True)
        host_binary = self.home / "host"
        host_binary.write_text("#!/bin/sh\n")
        host_binary.chmod(0o755)
        hosts = self.home / "NativeMessagingHosts"
        hosts.mkdir()
        (hosts / f"{acu.NATIVE_HOST}.json").write_text(json.dumps({
            "path": str(host_binary), "allowed_origins": [f"chrome-extension://{acu.EXTENSION_ID}/"]}))
        codex = self.home / "codex"
        codex.mkdir()
        (codex / "chrome-native-hosts-v2.json").write_text(json.dumps(
            {"entries": [{"extensionIds": [acu.EXTENSION_ID]}]}))
        self.profile = self.home / "Arc/Default"
        self.profile.mkdir(parents=True)
        self.set_prefs(str(self.output))
        for name, value in {"ARC_APPS": (arc_app,), "CHROME_HOSTS": hosts, "ARC_DATA": self.home / "Arc",
                            "HOME": self.home}.items():
            patcher = mock.patch.object(acu, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict("os.environ", {"CODEX_HOME": str(codex)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_prefs(self, path, developer_mode=True, location=acu.UNPACKED):
        (self.profile / "Secure Preferences").write_text(json.dumps({"extensions": {
            "settings": {acu.EXTENSION_ID: {"path": path, "location": location}},
            "ui": {"developer_mode": developer_mode}}}))

    def doctor(self):
        lines = []
        ok = acu.doctor(self.output, offline=True, log=lines.append)
        return ok, "\n".join(lines)

    def test_passes_when_everything_is_in_place(self):
        ok, text = self.doctor()
        self.assertTrue(ok, text)
        self.assertIn("Arc loads the build (profile 'Default')", text)

    def test_reports_an_extension_loaded_from_elsewhere(self):
        self.set_prefs("/somewhere/else")
        ok, text = self.doctor()
        self.assertFalse(ok)
        self.assertIn("remove the ChatGPT extension that profile 'Default' loads from /somewhere/else. "
                      "Then turn on Developer mode", text)

    def test_reports_the_store_extension_by_name(self):
        self.set_prefs(f"{acu.EXTENSION_ID}/1.0_0", location=1)
        ok, text = self.doctor()
        self.assertFalse(ok)
        self.assertIn("loads from the Chrome Web Store. Then turn on Developer mode", text)

    def test_reports_developer_mode_off(self):
        self.set_prefs(str(self.output), developer_mode=False)
        ok, text = self.doctor()
        self.assertFalse(ok)
        self.assertIn("FAIL  Developer mode is on in Arc", text)

    def test_reports_a_missing_connector(self):
        (acu.CHROME_HOSTS / f"{acu.NATIVE_HOST}.json").unlink()
        ok, text = self.doctor()
        self.assertFalse(ok)
        self.assertIn("FAIL  The Chrome plugin's native connector is installed", text)


if __name__ == "__main__":
    unittest.main()
