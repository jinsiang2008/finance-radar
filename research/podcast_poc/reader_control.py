"""Start the local reader and request one due check; no independent scheduler."""
import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

try:
    from . import poc
except ImportError:
    import poc

BASE = "http://127.0.0.1:8767"
OPENER = build_opener(ProxyHandler({}))


def status():
    with OPENER.open(BASE + "/api/wechat", timeout=5) as response:
        value = json.load(response)
    if value.get("reader_instance") != "podcast-poc-v2":
        raise RuntimeError("Port 8767 is occupied by a different reader version; restart it explicitly.")
    if value.get("reader_workspace") != poc.reader_workspace():
        raise RuntimeError("Port 8767 belongs to a different workspace; refusing to submit jobs.")
    return value


def start():
    runtime = poc.DATA / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "reader-start.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return status()
        except HTTPError:
            raise RuntimeError("The existing reader returned an HTTP error; inspect it before restarting.") from None
        except URLError as exc:
            if not isinstance(exc.reason, OSError) or exc.reason.errno != errno.ECONNREFUSED:
                raise RuntimeError("The local reader is unreachable; refusing to start a possible duplicate.") from None
        with (runtime / "reader.log").open("ab", buffering=0) as log:
            child = subprocess.Popen([sys.executable, str(Path(poc.__file__).resolve()), "serve", "--port", "8767"],
                cwd=poc.ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                env=os.environ.copy())
        (runtime / "reader.pid").write_text(str(child.pid))
        for _ in range(40):
            if child.poll() is not None:
                raise RuntimeError("Reader exited during startup; inspect its local log.")
            try:
                return status()
            except URLError:
                time.sleep(0.25)
        raise RuntimeError("Reader did not become ready within 10 seconds.")


def request_check(value, force=False):
    request = Request(BASE + "/api/wechat/check", data=json.dumps({"force": force}).encode(),
        headers={"Content-Type": "application/json", "X-Reader-Token": value["csrf_token"], "Origin": BASE}, method="POST")
    with OPENER.open(request, timeout=10) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "status", "tick", "check-now"))
    args = parser.parse_args()
    value = status() if args.command == "status" else start()
    if args.command in {"tick", "check-now"}:
        result = request_check(value, force=args.command == "check-now")
    else:
        result = {"url": BASE + "/wechat", "automation": value["automation"]}
    # The process-specific browser token never leaves this helper.
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
