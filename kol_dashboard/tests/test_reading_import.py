import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from reading_import import import_archive  # noqa: E402


class ReadingImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "reading"
        self.archive = self.root / "seed.tgz"

    def package(self, entries):
        with tarfile.open(self.archive, "w:gz") as output:
            for name, content in entries:
                member = tarfile.TarInfo(name)
                member.size = len(content)
                output.addfile(member, io.BytesIO(content))

    def test_import_is_private_and_never_overwrites_existing(self):
        article = json.dumps({"id": "wx-test", "content_kind": "wechat_article", "segments": []}).encode()
        self.package([("articles/wx-test.json", article), ("episodes/test.json", b'{"id":"test","segments":[]}')])
        self.assertEqual(import_archive(self.archive, self.data)["imported"], 2)
        path = self.data / "articles/wx-test.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.data.stat().st_mode & 0o777, 0o700)
        path.write_text("existing remote record")
        self.assertEqual(import_archive(self.archive, self.data)["skipped"], 2)
        self.assertEqual(path.read_text(), "existing remote record")

    def test_rejects_credentials_and_traversal_before_any_write(self):
        for forbidden in ("runtime/deepseek-credentials.json", "../outside.json", "/etc/file", "articles/not-article.json", "diagnostics/answer.json"):
            self.package([("episodes/test.json", b'{"id":"test","segments":[]}'), (forbidden, b"secret")])
            with self.assertRaises(ValueError):
                import_archive(self.archive, self.data)
            self.assertFalse(self.data.exists())

    def test_rejects_symlink_and_identity_mismatch(self):
        with tarfile.open(self.archive, "w:gz") as output:
            member = tarfile.TarInfo("episodes/test.json")
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/shadow"
            output.addfile(member)
        with self.assertRaises(ValueError):
            import_archive(self.archive, self.data)
        self.package([("episodes/test.json", b'{"id":"other","segments":[]}')])
        with self.assertRaises(ValueError):
            import_archive(self.archive, self.data)

    def test_rejects_duplicate_members_and_wrong_audio(self):
        entry = ("episodes/test.json", b'{"id":"test","segments":[]}')
        self.package([entry, entry])
        with self.assertRaises(ValueError):
            import_archive(self.archive, self.data)
        self.package([("media/test.wav", b"invalid sample")])
        with self.assertRaises(ValueError):
            import_archive(self.archive, self.data)
