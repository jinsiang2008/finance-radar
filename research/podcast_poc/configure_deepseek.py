"""Configure the local reader from a hidden terminal prompt; never accept a CLI key.

Validates once with the official read-only account endpoint, without calling a
model or displaying balances: https://api-docs.deepseek.com/api/get-user-balance/
"""
from __future__ import annotations

import getpass
from http.client import HTTPException
import json
import sys
import warnings
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

try:
    from . import inference, poc
except ImportError:
    import inference
    import poc


BALANCE_ENDPOINT = "https://api.deepseek.com/user/balance"
MAX_BALANCE_BYTES = 65_536


def validate_credentials(key: str) -> bool:
    """One bounded GET, with redirects blocked and no account data returned."""
    key = inference._validate_key(key)
    request = Request(BALANCE_ENDPOINT, headers={"Authorization": "Bearer " + key,
                                               "Accept": "application/json"}, method="GET")
    opener = build_opener(inference._NoRedirect())
    try:
        with opener.open(request, timeout=inference._timeout()) as response:
            if int(response.status) != 200:
                raise inference._http_error(int(response.status))
            raw = response.read(MAX_BALANCE_BYTES + 1)
    except HTTPError as exc:
        status = int(exc.code)
        exc.close()
        raise inference._http_error(status) from None
    except TimeoutError:
        raise inference.DeepSeekError("timeout") from None
    except URLError as exc:
        raise inference.DeepSeekError("timeout" if isinstance(exc.reason, TimeoutError) else "network") from None
    except (OSError, HTTPException, ValueError):
        raise inference.DeepSeekError("network") from None
    if len(raw) > MAX_BALANCE_BYTES:
        raise inference.DeepSeekError("response_too_large")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise inference.DeepSeekError("invalid_response") from None
    if (not isinstance(value, dict) or type(value.get("is_available")) is not bool
            or not isinstance(value.get("balance_infos"), list)):
        raise inference.DeepSeekError("invalid_response")
    # False means insufficient funds, not an invalid key. Do not expose balances.
    return value["is_available"]


def save_credentials(key: str) -> None:
    path = poc.DATA / "runtime" / "deepseek-credentials.json"
    # write_json uses a fresh mode-0600 mkstemp and atomic replacement, so the
    # credential is never briefly published with the old destination's mode.
    poc.write_json(path, {"api_key": inference._validate_key(key)})
    path.chmod(0o600)


def main() -> int:
    print("密钥只保存在本机 private/podcast-poc/runtime/deepseek-credentials.json（Git 已忽略，权限 0600）。")
    print("输入不会回显；仅向 DeepSeek 官方只读账户接口验证一次，不调用模型、不显示余额。")
    print("请勿把密钥粘贴到聊天、命令参数或日志中。")
    try:
        if not sys.stdin.isatty():
            print("请在本机交互终端运行此脚本；不接受管道输入。", file=sys.stderr)
            return 1
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = inference._validate_key(getpass.getpass("DeepSeek API Key: "))
        available = validate_credentials(key)
        save_credentials(key)
        settings = poc.reader_settings()
        settings.update({"credentials_status": "verified" if available else "balance_insufficient",
                         "enabled": False})
        poc.write_json(poc.DATA / "runtime" / "automation.json", settings)
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("配置已取消，未保存新密钥。", file=sys.stderr)
        return 1
    except inference.DeepSeekError as exc:
        print(f"验证未通过，未保存新密钥：{exc}", file=sys.stderr)
        return 1
    except Exception:
        # Never print an exception supplied by terminal, filesystem, or network.
        print("配置未完成，请检查本地文件权限后重试。", file=sys.stderr)
        return 1
    print("密钥已验证并保存在本机，尚未开启自动任务，请回到当前聊天完成启用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
