"""Parse the fixed holdings ledger into a validated, privacy-minimized snapshot."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import date
from pathlib import Path
from typing import Any


_DEFAULT_PRIVATE_DIR = Path(__file__).resolve().parent / "private"
DEFAULT_HOLDINGS_PATH = Path(
    os.environ.get(
        "KOL_DASHBOARD_HOLDINGS_FILE",
        str(_DEFAULT_PRIVATE_DIR / "holdings.md"),
    )
)
SNAPSHOT_SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024

POSITION_FIELDS = (
    "account",
    "asset_key",
    "symbol",
    "name",
    "quantity",
    "avg_cost",
    "currency",
    "asset_class",
    "is_leveraged",
    "as_of",
)

_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_US_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
_CN_SYMBOL_RE = re.compile(r"^\d{6}$")
_CRYPTO_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{1,14}$")
_TABLE_DIVIDER_RE = re.compile(r"^:?-{2,}:?$")
_LEVERAGED_TICKERS = {
    "NVDL",
    "TSLL",
    "YINN",
    "VXX",
    "SPXS",
    "SOXL",
    "DRAM",
    "MUU",
    "GLWG",
    "TQQQ",
    "SQQQ",
    "UPRO",
    "SPXU",
}
_ASSET_CLASSES = {"stock", "etf", "convertible_bond", "crypto"}
_ACCOUNTS = {"招商证券", "Robinhood", "Schwab"}
_CURRENCIES = {"CNY", "USD"}


def _clean_markdown_text(value: Any, *, maximum: int = 200) -> str:
    text = str(value or "").strip()
    text = text.replace("~~", "").replace("**", "").replace("`", "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text or len(text) > maximum or "\x00" in text:
        raise ValueError("invalid holdings text field")
    return text


def _number(value: Any, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    text = str(value or "").strip().replace(",", "")
    text = text.replace("$", "").replace("¥", "").replace("￥", "")
    if text in {"", "-", "—", "–", "N/A", "n/a"} and optional:
        return None
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", text):
        raise ValueError("invalid numeric holdings field")
    result = float(text)
    if not math.isfinite(result):
        raise ValueError("invalid numeric holdings field")
    return result


def _valid_date(value: Any, *, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    text = str(value or "").strip()
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("as_of must be an ISO date") from exc
    return text


def _snapshot_hash(as_of: str | None, positions: list[dict[str, Any]]) -> str:
    fingerprint = json.dumps(
        {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "as_of": as_of,
            "positions": positions,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def _heading_text(line: str) -> str | None:
    normalized = line.strip().lstrip("|").strip()
    match = re.fullmatch(r"#{2,6}\s+(.+)", normalized)
    return match.group(1).strip() if match else None


def _section_for_heading(heading: str) -> dict[str, Any] | None:
    normalized = re.sub(
        r"\s+",
        " ",
        heading.replace("—", "-").replace("–", "-").strip(),
    )
    match = re.fullmatch(
        r"(A 股持仓|美股持仓(?:（Schwab 账户）)?)\s*-\s*"
        r"(股票|ETF / 基金|ETF / 杠杆|ETF / 封闭式基金|可转债|加密货币)",
        normalized,
    )
    if not match:
        return None
    account_heading, section_heading = match.groups()
    if account_heading == "A 股持仓":
        account = "招商证券"
        market = "CN"
        currency = "CNY"
    else:
        account = "Schwab" if "Schwab" in account_heading else "Robinhood"
        market = "CRYPTO" if section_heading == "加密货币" else "US"
        currency = "USD"

    if section_heading == "可转债":
        asset_class = "convertible_bond"
    elif section_heading.startswith("ETF"):
        asset_class = "etf"
    elif section_heading == "加密货币":
        asset_class = "crypto"
    elif section_heading == "股票":
        asset_class = "stock"
    else:
        return None
    return {
        "account": account,
        "market": market,
        "currency": currency,
        "asset_class": asset_class,
        "as_of": None,
    }


def _table_cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    if len(cells) < 4:
        return None
    return cells


def _is_header_or_divider(cells: list[str]) -> bool:
    first = cells[0].replace("**", "").strip()
    if first in {"代号", "代码", "市场", "日期", "项目"}:
        return True
    return all(
        not cell or _TABLE_DIVIDER_RE.fullmatch(cell.replace(" ", ""))
        for cell in cells
    )


def _is_closed_row(cells: list[str], quantity: float) -> bool:
    if quantity <= 0:
        return True
    raw = " | ".join(cells)
    if "~~" in cells[0] or "~~" in cells[1]:
        return True
    return bool(
        re.search(r"(?:已|全部|完全)\s*清仓|❌[^|]{0,40}清仓", raw)
    )


def _asset_identity(section: dict[str, Any], symbol: str) -> tuple[str, str]:
    market = section["market"]
    upper = symbol.upper()
    if market == "CN":
        if not _CN_SYMBOL_RE.fullmatch(upper):
            raise ValueError("invalid CN symbol")
        return upper, f"CN:{upper}"
    if market == "CRYPTO":
        if not _CRYPTO_SYMBOL_RE.fullmatch(upper):
            raise ValueError("invalid crypto symbol")
        return upper, f"CRYPTO:{upper}"
    if not _US_SYMBOL_RE.fullmatch(upper):
        raise ValueError("invalid US symbol")
    return upper, f"US:{upper}"


def _is_leveraged(symbol: str, cells: list[str], asset_class: str) -> bool:
    if asset_class != "etf":
        return False
    text = " ".join(cells)
    return symbol in _LEVERAGED_TICKERS or bool(
        re.search(r"(?:[23]\s*[xX倍]|杠杆|inverse|反向)", text, re.IGNORECASE)
    )


def parse_holdings_markdown(markdown: str) -> dict[str, Any]:
    """Parse only recognized security-holding sections from Markdown text."""
    if not isinstance(markdown, str):
        raise TypeError("holdings markdown must be text")
    if len(markdown.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("holdings file is too large")

    current: dict[str, Any] | None = None
    account_dates: dict[str, str] = {}
    positions: list[dict[str, Any]] = []
    for line in markdown.splitlines():
        heading = _heading_text(line)
        if heading is not None:
            current = _section_for_heading(heading)
            continue
        if current is None:
            continue

        date_match = _DATE_RE.search(line)
        if date_match and line.lstrip().startswith(">"):
            as_of = _valid_date(date_match.group(1), optional=False)
            current["as_of"] = as_of
            account_dates[current["account"]] = as_of
            continue

        cells = _table_cells(line)
        if cells is None or _is_header_or_divider(cells):
            continue
        try:
            quantity = _number(cells[2])
            if quantity is None or _is_closed_row(cells, quantity):
                continue
            avg_cost = _number(cells[3], optional=True)
            symbol_text = _clean_markdown_text(cells[0], maximum=32)
            symbol, asset_key = _asset_identity(current, symbol_text)
            name = _clean_markdown_text(cells[1])
        except ValueError:
            continue
        positions.append(
            {
                "account": current["account"],
                "asset_key": asset_key,
                "symbol": symbol,
                "name": name,
                "quantity": quantity,
                "avg_cost": avg_cost,
                "currency": current["currency"],
                "asset_class": current["asset_class"],
                "is_leveraged": _is_leveraged(
                    symbol, cells, current["asset_class"]
                ),
                "as_of": current["as_of"],
            }
        )

    for position in positions:
        if position["as_of"] is None:
            position["as_of"] = account_dates.get(position["account"])
    dates = [item["as_of"] for item in positions if item["as_of"]]
    snapshot_as_of = max(dates) if dates else None
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "source_hash": _snapshot_hash(snapshot_as_of, positions),
        "as_of": snapshot_as_of,
        "positions": positions,
    }
    return validate_snapshot(snapshot)


def _validate_position(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != set(POSITION_FIELDS):
        raise ValueError("snapshot position fields are not sanitized")
    account = _clean_markdown_text(raw["account"], maximum=50)
    if account not in _ACCOUNTS:
        raise ValueError("unsupported account")
    symbol = _clean_markdown_text(raw["symbol"], maximum=32).upper()
    asset_key = _clean_markdown_text(raw["asset_key"], maximum=100).upper()
    expected_prefix = (
        "CN:" if account == "招商证券" else
        "CRYPTO:" if raw["asset_class"] == "crypto" else
        "US:"
    )
    if asset_key != f"{expected_prefix}{symbol}":
        raise ValueError("asset_key and symbol do not match")
    quantity = _number(raw["quantity"])
    if quantity is None or quantity <= 0 or quantity > 1e18:
        raise ValueError("quantity is outside the supported range")
    avg_cost = _number(raw["avg_cost"], optional=True)
    if avg_cost is not None and (avg_cost < 0 or avg_cost > 1e15):
        raise ValueError("avg_cost is outside the supported range")
    currency = _clean_markdown_text(raw["currency"], maximum=16).upper()
    if currency not in _CURRENCIES:
        raise ValueError("unsupported currency")
    asset_class = _clean_markdown_text(raw["asset_class"], maximum=32)
    if asset_class not in _ASSET_CLASSES:
        raise ValueError("unsupported asset class")
    if not isinstance(raw["is_leveraged"], bool):
        raise ValueError("is_leveraged must be boolean")
    as_of = _valid_date(raw["as_of"])
    return {
        "account": account,
        "asset_key": asset_key,
        "symbol": symbol,
        "name": _clean_markdown_text(raw["name"]),
        "quantity": quantity,
        "avg_cost": avg_cost,
        "currency": currency,
        "asset_class": asset_class,
        "is_leveraged": raw["is_leveraged"],
        "as_of": as_of,
    }


def validate_snapshot(raw: Any) -> dict[str, Any]:
    """Return a fresh validated snapshot and reject any extra privacy fields."""
    required = {"schema_version", "source_hash", "as_of", "positions"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("snapshot fields are not sanitized")
    if raw["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("unsupported snapshot schema version")
    source_hash = str(raw["source_hash"] or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValueError("invalid source_hash")
    if not isinstance(raw["positions"], list) or len(raw["positions"]) > 10_000:
        raise ValueError("positions must be a bounded list")
    positions = [_validate_position(item) for item in raw["positions"]]
    as_of = _valid_date(raw["as_of"])
    position_dates = [item["as_of"] for item in positions if item["as_of"]]
    expected_as_of = max(position_dates) if position_dates else None
    if as_of != expected_as_of:
        raise ValueError("snapshot as_of must match the latest position date")
    if source_hash != _snapshot_hash(as_of, positions):
        raise ValueError("source_hash does not match sanitized positions")
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "source_hash": source_hash,
        "as_of": as_of,
        "positions": positions,
    }


def snapshot_to_json(snapshot: Any) -> str:
    clean = validate_snapshot(snapshot)
    return json.dumps(
        clean,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is forbidden: {value}")


def snapshot_from_json(payload: str | bytes | bytearray) -> dict[str, Any]:
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot JSON is too large")
        text = bytes(payload).decode("utf-8")
    elif isinstance(payload, str):
        text = payload
        if len(text.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot JSON is too large")
    else:
        raise TypeError("snapshot JSON must be text or bytes")
    try:
        decoded = json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid snapshot JSON") from exc
    return validate_snapshot(decoded)


def load_holdings(path: str | Path = DEFAULT_HOLDINGS_PATH) -> dict[str, Any]:
    """Load only the configured ledger; arbitrary paths are never accepted."""
    configured = DEFAULT_HOLDINGS_PATH.expanduser().resolve()
    candidate = Path(path).expanduser().resolve()
    if candidate != configured:
        raise PermissionError("only the configured holdings file may be loaded")
    size = candidate.stat().st_size
    if size > MAX_SNAPSHOT_BYTES:
        raise ValueError("holdings file is too large")
    return parse_holdings_markdown(candidate.read_text(encoding="utf-8"))


build_snapshot = parse_holdings_markdown
parse_snapshot_json = snapshot_from_json
parse_holdings_file = load_holdings
