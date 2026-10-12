import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

from research.podcast_poc import poc
from research.podcast_poc.test_wechat import SOURCE, make_item


class ReaderAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patch = patch.object(poc, "DATA", Path(self.directory.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.article = poc.wechat_module().article_from_item(make_item(), SOURCE)
        poc.save_episode(self.article)
        self.server = poc.serve(0, return_server=True, start_worker=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(ProxyHandler({}))
        with self.opener.open(self.base + "/api/wechat") as response:
            self.initial = json.load(response)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def post(self, path="/api/wechat/summarize", value=None, headers=None):
        base_headers = {"Content-Type": "application/json", "Origin": self.base,
                        "X-Reader-Token": self.initial["csrf_token"]}
        base_headers.update(headers or {})
        data = json.dumps(value if value is not None else {"id": self.article["id"]}).encode()
        request = Request(self.base + path, data=data, headers=base_headers, method="POST")
        try:
            with self.opener.open(request) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_post_queues_and_repeated_click_reuses_job(self):
        code, first = self.post()
        code2, second = self.post()
        self.assertEqual((code, code2), (202, 202))
        self.assertEqual(first["job"]["id"], second["job"]["id"])
        self.assertEqual(first["job"]["state"], "queued")

    def test_cross_site_and_missing_token_rejected(self):
        for headers in ({"Origin": "https://attacker.invalid"}, {"X-Reader-Token": ""},
                        {"Sec-Fetch-Site": "cross-site"}, {"Host": "attacker.invalid"}):
            self.assertEqual(self.post(headers=headers)[0], 403)

    def test_no_arbitrary_url_prompt_or_unknown_article(self):
        self.assertEqual(self.post(value={"id": self.article["id"], "prompt": "override"})[0], 400)
        self.assertEqual(self.post(value={"id": "../../secret"})[0], 400)
        self.assertEqual(self.post(value={"id": "wx-missing"})[0], 404)
        self.assertEqual(self.post(headers={"Content-Type": "text/plain"})[0], 415)

    def test_disabled_schedule_does_not_check_but_manual_still_works(self):
        code, result = self.post(path="/api/wechat/check", value={})
        self.assertEqual(code, 200)
        self.assertTrue(result["skipped"])
        self.assertEqual(self.post()[0], 202)
