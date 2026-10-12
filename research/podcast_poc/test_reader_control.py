import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from research.podcast_poc import poc, reader_control


class ReaderControlTests(unittest.TestCase):
    def test_http_failure_never_spawns_duplicate_service(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(poc, "DATA", Path(directory)):
            with patch.object(reader_control, "status", side_effect=HTTPError("local", 503, "down", {}, None)), \
                    patch.object(reader_control.subprocess, "Popen") as spawn:
                with self.assertRaises(RuntimeError):
                    reader_control.start()
                spawn.assert_not_called()

    def test_timeout_never_spawns_duplicate_service(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(poc, "DATA", Path(directory)):
            with patch.object(reader_control, "status", side_effect=URLError(TimeoutError())), \
                    patch.object(reader_control.subprocess, "Popen") as spawn:
                with self.assertRaises(RuntimeError):
                    reader_control.start()
                spawn.assert_not_called()

    def test_another_worktree_is_not_accepted(self):
        data = {"reader_instance": "podcast-poc-v2", "reader_workspace": "different-worktree"}
        with patch.object(reader_control.OPENER, "open", return_value=io.BytesIO(json.dumps(data).encode())):
            with self.assertRaises(RuntimeError):
                reader_control.status()

    def test_this_workspace_is_accepted(self):
        data = {"reader_instance": "podcast-poc-v2", "reader_workspace": poc.reader_workspace()}
        with patch.object(reader_control.OPENER, "open", return_value=io.BytesIO(json.dumps(data).encode())):
            self.assertEqual(reader_control.status(), data)
