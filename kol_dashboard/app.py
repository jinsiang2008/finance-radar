"""KOL Dashboard + Macro Risk Radar — FastAPI app.

Routes:
  GET  /                    — single-page dashboard
  GET  /health              — health probe
  GET  /api/stats           — headline counters
  GET  /api/kols            — per-KOL summary
  GET  /api/events          — filtered event list
  GET  /api/events/{id}     — event intelligence, evidence and related stories
  GET  /api/macro           — latest macro risk snapshot
  GET  /api/macro/history   — composite risk score over time
  GET  /api/briefings/latest — read-only persisted Daily Briefing
  GET  /api/decisions       — complete public decision snapshot
  GET  /api/decisions/summary — small first-screen decision projection
  GET  /api/decisions/detail — one decision's evidence chain
  GET  /api/relations       — public mechanism relations
  GET  /api/market/reactions — public market validation records
  POST /api/auth/login      — unlock private mode
  GET  /api/auth/status     — private-mode session status
  POST /api/auth/logout     — clear private-mode session
  GET  /api/private/options/overview — authenticated research-only options readiness
  GET  /api/private/*       — authenticated portfolio overlay
  POST /api/prune           — manual prune (admin)

Environment:
  KOL_DASHBOARD_PORT        — default 8088
  KOL_DASHBOARD_HOST        — default 127.0.0.1
  KOL_DASHBOARD_DB          — sqlite path
  KOL_DAILY_REFRESH_SCHEDULE — deploy-owned Daily timer contract (hourly)
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Path as ApiPath, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import auth
import briefing_service
import db
import decision_snapshot
import decision_service
import llm_enrichment
import options_research_service

BASE = Path(__file__).parent
app = FastAPI(title="KOL Dashboard + Macro Risk Radar", version="2.0")

app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

LOGIN_LIMITER = auth.LoginRateLimiter(max_attempts=5, window_seconds=15 * 60)
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}
_PUBLIC_CACHE_HEADERS = {"Cache-Control": "public, max-age=30"}
_BRIEFING_CACHE_HEADERS = {"Cache-Control": "public, max-age=60"}


class LoginBody(BaseModel):
    passcode: str = Field(min_length=1, max_length=256)


_AI_ACTION_HEADER = "X-Finance-Radar-Action"
_AI_ACTION_VALUE = "request-ai-enrichment"
_AI_SUBJECT_TYPES = {"event", "macro_event"}
_EVENT_SUBJECT_ID = re.compile(r"^[1-9][0-9]{0,18}$")
_SQLITE_MAX_INTEGER = (1 << 63) - 1
_KOL_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_KOL_FILTERS = db.MAX_EVENT_KOL_FILTERS
_MAX_KOL_FILTER_QUERY_LENGTH = _MAX_KOL_FILTERS * 64 + _MAX_KOL_FILTERS - 1
_AI_PUBLIC_RESPONSE_FIELDS = frozenset(
    {
        "state",
        "can_request",
        "accepted_at",
        "generated_at",
        "retry_after_seconds",
        "next_attempt_at",
    }
)


def _parse_kol_filters(kol: str | None, kols: str | None) -> list[str]:
    """Return a stable, de-duplicated union of legacy and multi-KOL filters."""
    selected: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if not _KOL_KEY.fullmatch(value):
            raise HTTPException(
                status_code=422,
                detail=(
                    "invalid_kols: keys must use lowercase letters, "
                    "numbers, underscores, or hyphens"
                ),
            )
        if value in seen:
            return
        seen.add(value)
        selected.append(value)

    if kol:
        add(kol)
    if kols:
        for raw_key in kols.split(","):
            key = raw_key.strip()
            if not key:
                continue
            add(key)
    if len(selected) > _MAX_KOL_FILTERS:
        raise HTTPException(
            status_code=422,
            detail=f"too_many_kols: select at most {_MAX_KOL_FILTERS}",
        )
    return selected


def _auth_config() -> auth.AuthConfig:
    try:
        return auth.load_config()
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail="private_mode_misconfigured",
            headers=_NO_STORE_HEADERS,
        ) from exc


def _session_signer(config: auth.AuthConfig) -> auth.SessionSigner:
    if not config.configured or config.session_secret is None:
        raise HTTPException(
            status_code=503,
            detail="private_mode_unavailable",
            headers=_NO_STORE_HEADERS,
        )
    return auth.SessionSigner(
        config.session_secret,
        ttl_seconds=config.session_ttl_seconds,
    )


def _client_key(request: Request) -> str:
    client = request.client
    return client.host if client is not None else "unknown"


def _authenticated(request: Request, config: auth.AuthConfig) -> bool:
    if not config.configured:
        return False
    token = request.cookies.get(config.cookie_name)
    return _session_signer(config).verify(token) is not None


def require_private_session(request: Request) -> dict[str, Any]:
    config = _auth_config()
    if not config.configured:
        raise HTTPException(
            status_code=503,
            detail="private_mode_unavailable",
            headers=_NO_STORE_HEADERS,
        )
    token = request.cookies.get(config.cookie_name)
    claims = _session_signer(config).verify(token)
    if claims is None:
        raise HTTPException(
            status_code=401,
            detail="private_mode_required",
            headers=_NO_STORE_HEADERS,
        )
    return claims


def _private_response(
    payload: Any,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={**_NO_STORE_HEADERS, **dict(headers or {})},
    )


@app.on_event("startup")
def _init_db() -> None:
    db.init()
    db.warm_event_relevance_cache()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/auth/status")
def api_auth_status(request: Request) -> JSONResponse:
    config = _auth_config()
    return _private_response(
        {
            "configured": config.configured,
            "authenticated": _authenticated(request, config),
        }
    )


@app.post("/api/auth/login")
def api_auth_login(request: Request, body: LoginBody) -> JSONResponse:
    config = _auth_config()
    if not config.configured or config.passcode_hash is None:
        raise HTTPException(
            status_code=503,
            detail="private_mode_unavailable",
            headers=_NO_STORE_HEADERS,
        )
    client_key = _client_key(request)
    allowed, retry_after = LOGIN_LIMITER.acquire(client_key)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="too_many_attempts",
            headers={
                **_NO_STORE_HEADERS,
                "Retry-After": str(retry_after),
            },
        )
    if not auth.verify_passcode(body.passcode, config.passcode_hash):
        raise HTTPException(
            status_code=401,
            detail="invalid_credentials",
            headers=_NO_STORE_HEADERS,
        )

    LOGIN_LIMITER.reset(client_key)
    token = _session_signer(config).issue()
    response = _private_response(
        {
            "authenticated": True,
            "expires_in": config.session_ttl_seconds,
        }
    )
    response.set_cookie(
        key=config.cookie_name,
        value=token,
        max_age=config.session_ttl_seconds,
        httponly=True,
        secure=config.cookie_secure,
        samesite="strict",
        path=config.cookie_path,
    )
    return response


@app.post("/api/auth/logout")
def api_auth_logout() -> JSONResponse:
    config = _auth_config()
    response = _private_response({"authenticated": False})
    response.delete_cookie(
        key=config.cookie_name,
        path=config.cookie_path,
        secure=config.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@app.get("/api/stats")
def api_stats(hours: int = Query(24, ge=1, le=720)) -> dict:
    return db.stats(hours=hours)


@app.get("/api/kols")
def api_kols() -> list:
    return db.list_kols()


@app.get("/api/events")
def api_events(
    kol: Optional[str] = Query(None, max_length=64),
    kols: Optional[str] = Query(None, max_length=_MAX_KOL_FILTER_QUERY_LENGTH),
    hours: Optional[int] = Query(24, ge=1, le=720),
    impact: Optional[str] = Query(None),
    q: Optional[str] = Query(None, max_length=120),
    time_status: Literal["verified", "unverified"] = Query("verified"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    selected_kols = _parse_kol_filters(kol, kols)
    items = db.query_events(
        kols=selected_kols,
        hours=hours,
        impact=impact,
        q=q,
        time_status=time_status,
        limit=limit,
        offset=offset,
        use_ai_impact=True,
    )
    items = [
        item
        for item in items
        if llm_enrichment.is_event_enrichment_eligible(item)
    ]
    for it in items:
        it["tickers"] = [t for t in (it.get("tickers") or "").split(",") if t]
    return {"items": items, "count": len(items)}


@app.get("/api/events/{event_id:int}")
def api_event_detail(
    event_id: int = ApiPath(ge=1, le=9_223_372_036_854_775_807),
    kol: Optional[str] = Query(None, max_length=120),
    source_url: Optional[str] = Query(None, max_length=2048),
) -> JSONResponse:
    detail = db.get_event_detail(event_id)
    if detail is None:
        reason = (
            "event_not_available" if db.event_exists(event_id)
            else "event_not_found"
        )
        raise HTTPException(status_code=404, detail=reason)
    event = detail["event"]
    if not llm_enrichment.is_event_enrichment_eligible(event):
        raise HTTPException(status_code=404, detail="event_not_available")
    event["tickers"] = [
        ticker for ticker in (event.get("tickers") or "").split(",") if ticker
    ]
    for sighting in detail["sightings"]:
        sighting_tickers = sighting.get("tickers") or ""
        sighting["tickers"] = (
            [ticker for ticker in sighting_tickers.split(",") if ticker]
            if isinstance(sighting_tickers, str)
            else list(sighting_tickers)
        )
    primary_ai_subject = None
    if kol or source_url:
        selected_sighting = next(
            (
                sighting
                for sighting in detail["sightings"]
                if (
                    not kol
                    or str(sighting.get("kol_key") or "") == kol
                )
                and (
                    not source_url
                    or str(sighting.get("source_url") or "") == source_url
                )
            ),
            None,
        )
        if selected_sighting is None:
            raise HTTPException(status_code=404, detail="event_sighting_not_found")
        primary_event = dict(event)
        same_ai_subject = (
            int(event.get("sighting_id") or 0)
            == int(selected_sighting.get("sighting_id") or -1)
        )
        for field in (
            "title",
            "snippet",
            "tickers",
            "source_url",
            "source",
            "kol_key",
            "kol_name",
            "kol_name_cn",
            "attribution_basis",
            "matched_alias",
            "rule_impact",
            "impact",
            "has_market_kw",
            "published_at",
            "time_status",
            "first_seen_at",
            "last_seen_at",
            "sighting_id",
        ):
            event[field] = selected_sighting.get(field)
        selected_tickers = event.get("tickers") or ""
        event["tickers"] = (
            [ticker for ticker in selected_tickers.split(",") if ticker]
            if isinstance(selected_tickers, str)
            else list(selected_tickers)
        )
        event["ai_request_eligible"] = same_ai_subject
        if not same_ai_subject:
            primary_ai_subject = {
                key: primary_event.get(key)
                for key in (
                    "sighting_id",
                    "title",
                    "snippet",
                    "tickers",
                    "source_url",
                    "source",
                    "kol_key",
                    "kol_name",
                    "kol_name_cn",
                    "attribution_basis",
                    "matched_alias",
                    "published_at",
                    "time_status",
                    "impact",
                    "rule_impact",
                    "ai_status",
                    "ai_enrichment",
                )
            }
            primary_ai_subject["ai_request_eligible"] = True
            event["ai_status"] = "ineligible"
            event["ai_enrichment"] = None
        if not llm_enrichment.is_event_enrichment_eligible(event):
            raise HTTPException(status_code=404, detail="event_not_available")
    relations = decision_service.project_public_relations(
        db.query_relations(
            source_type="event",
            source_id=str(event_id),
            limit=100,
        )
    )
    reactions = decision_service.project_public_reactions(
        db.query_market_reactions(
            source_type="event",
            source_id=str(event_id),
            limit=100,
        )
    )
    if primary_ai_subject is not None:
        # Relations and market reactions are generated once from the event's
        # preferred evidence. Keep them attached to that identity when the
        # caller is inspecting another KOL/source sighting.
        primary_ai_subject["relations"] = relations
        primary_ai_subject["market_reactions"] = reactions
        relations = []
        reactions = []
    return JSONResponse(
        {
            **detail,
            "primary_ai_subject": primary_ai_subject,
            "relations": relations,
            "market_reactions": reactions,
        },
        headers=_PUBLIC_CACHE_HEADERS,
    )

def _public_macro_snapshot(
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the latest evidence-bound public macro projection.

    The helper performs only SQLite reads and pure validation.  Keeping it
    shared prevents the Daily route from bypassing the same public/privacy and
    stale-AI checks used by ``/api/macro``.
    """
    snap = snapshot if snapshot is not None else db.latest_macro()
    if not snap:
        return None
    expected: list[tuple[str, str, str]] = []
    monitored_events = snap.get("monitored_events")
    if isinstance(monitored_events, list):
        for event in monitored_events[:24]:
            if not isinstance(event, Mapping):
                continue
            event_id = str(event.get("id") or "").strip()
            if not event_id:
                continue
            event_key = llm_enrichment.macro_event_key(event)
            _, input_hash = llm_enrichment.build_macro_event_input(event)
            expected.append((event_id, event_key, input_hash))

    cached = db.query_macro_event_enrichments(
        event_key for _, event_key, _ in expected
    )
    matching: dict[str, dict[str, Any]] = {}
    for event_id, event_key, input_hash in expected:
        value = cached.get(event_key)
        if not isinstance(value, Mapping):
            continue
        # A cache entry is evidence-bound: stale output never follows a title
        # or indicator value after the underlying monitored event changes.
        if (
            value.get("input_hash") != input_hash
            or value.get("prompt_version")
            != llm_enrichment.MACRO_PROMPT_VERSION
        ):
            continue
        matching[event_id] = dict(value)

    return decision_service.project_public_macro(
        snap,
        macro_event_enrichments=matching,
        now=datetime.now(timezone.utc),
    )


@app.get("/api/macro")
def api_macro() -> JSONResponse:
    public_snapshot = _public_macro_snapshot()
    if public_snapshot is None:
        return JSONResponse(
            {"available": False, "reason": "尚未采集到宏观快照，请运行 macro_collect.py"},
            status_code=200,
        )
    public_snapshot["available"] = True
    return JSONResponse(public_snapshot)


@app.get("/api/macro/history")
def api_macro_history(limit: int = Query(60, ge=2, le=500)) -> dict:
    return {"items": db.macro_history(limit=limit)}


@app.get("/api/briefings/latest")
def api_latest_briefing() -> JSONResponse:
    # ``load_public_snapshot`` is intentionally used instead of
    # ``ensure_public_snapshot``: an HTTP read must not rebuild decisions.
    decision_record = decision_snapshot.load_public_snapshot()
    imported_record = db.load_latest_daily_briefing_snapshot(max_age_hours=24)
    imported_snapshot = (
        imported_record.get("payload")
        if isinstance(imported_record, Mapping)
        and isinstance(imported_record.get("payload"), Mapping)
        else None
    )
    payload = briefing_service.build_latest_briefing(
        repository=db,
        public_macro=_public_macro_snapshot(),
        decision_record=decision_record,
        imported_snapshot=imported_snapshot,
        refresh_schedule=os.environ.get("KOL_DAILY_REFRESH_SCHEDULE", ""),
    )
    return JSONResponse(payload, headers=_BRIEFING_CACHE_HEADERS)


def _macro_coverage() -> float:
    """Compatibility wrapper for collector coverage accounting."""
    return decision_snapshot.macro_coverage(db)


def _build_public_decisions() -> dict[str, Any]:
    try:
        record = decision_snapshot.ensure_public_snapshot()
        return decision_snapshot.response_payload(record, kind="full")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="decision_snapshot_unavailable",
            headers={"Retry-After": "30"},
        ) from exc


def _decision_record() -> dict[str, Any]:
    try:
        return decision_snapshot.ensure_public_snapshot()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="decision_snapshot_unavailable",
            headers={"Retry-After": "30"},
        ) from exc


def _etag_matches(request: Request, etag: str) -> bool:
    values = request.headers.get("if-none-match", "")
    expected = etag.strip()
    if expected[:2].lower() == "w/":
        expected = expected[2:].lstrip()
    for value in values.split(","):
        candidate = value.strip()
        if candidate == "*":
            return True
        if candidate[:2].lower() == "w/":
            candidate = candidate[2:].lstrip()
        if candidate == expected:
            return True
    return False


def _decision_response_now() -> datetime:
    return datetime.now(timezone.utc)


def _decision_cache_headers(
    record: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, str]:
    max_age = decision_snapshot.cache_max_age(record, now=now)
    return {
        "Cache-Control": (
            f"public, max-age={max_age}, stale-while-revalidate=120"
        ),
        "Vary": "Accept-Encoding",
    }


def _decision_json_response(
    request: Request,
    record: Mapping[str, Any],
    *,
    kind: str,
) -> Response:
    current = _decision_response_now()
    etag = decision_snapshot.etag(record, kind, now=current)
    headers = {
        **_decision_cache_headers(record, now=current),
        "ETag": etag,
    }
    if _etag_matches(request, etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(
        decision_snapshot.response_payload(record, kind=kind, now=current),
        headers=headers,
    )


@app.get("/api/decisions")
def api_decisions(request: Request) -> Response:
    return _decision_json_response(request, _decision_record(), kind="full")


@app.get("/api/decisions/summary")
def api_decision_summary(request: Request) -> Response:
    return _decision_json_response(request, _decision_record(), kind="summary")


@app.get("/api/decisions/detail")
def api_decision_detail(
    topic_key: str = Query(..., min_length=1, max_length=120),
    asset_key: str = Query(..., min_length=1, max_length=80),
    snapshot_id: Optional[int] = Query(None, ge=1),
) -> JSONResponse:
    current = _decision_record()
    record = current
    if snapshot_id is not None and snapshot_id != current["snapshot_id"]:
        record = decision_snapshot.load_public_snapshot(snapshot_id=snapshot_id)
        if record is None:
            return JSONResponse(
                {
                    "detail": "decision_snapshot_changed",
                    "current_snapshot_id": current["snapshot_id"],
                },
                status_code=409,
                headers=_PUBLIC_CACHE_HEADERS,
            )
    card = decision_service.find_decision(record["full"], topic_key, asset_key)
    if card is None:
        raise HTTPException(status_code=404, detail="decision_not_found")
    current = _decision_response_now()
    metadata = decision_snapshot.response_payload(
        record,
        kind="summary",
        now=current,
    )
    return JSONResponse(
        {
            "snapshot_id": record["snapshot_id"],
            "generated_at": record.get("generated_at"),
            "age_seconds": metadata["age_seconds"],
            "stale": metadata["stale"],
            "decision": card,
            "evidence_policy": record["full"].get(
                "evidence_policy", decision_service.EVIDENCE_POLICY
            ),
            "business_health": record["full"].get("business_health", {}),
        },
        headers=_decision_cache_headers(record, now=current),
    )


@app.get("/api/relations")
def api_relations(
    source_type: Optional[str] = Query(None, max_length=40),
    source_id: Optional[str] = Query(None, max_length=160),
    topic_key: Optional[str] = Query(None, max_length=120),
    asset_key: Optional[str] = Query(None, max_length=80),
    relation_type: Optional[str] = Query(None, max_length=40),
    limit: int = Query(200, ge=1, le=1_000),
) -> JSONResponse:
    items = decision_service.project_public_relations(
        db.query_relations(
            source_type=source_type,
            source_id=source_id,
            topic_key=topic_key,
            asset_key=asset_key,
            relation_type=relation_type,
            limit=limit,
            eligible_events_only=True,
        )
    )
    return JSONResponse(
        {"items": items, "count": len(items)},
        headers=_PUBLIC_CACHE_HEADERS,
    )


@app.get("/api/market/reactions")
def api_market_reactions(
    source_type: Optional[str] = Query(None, max_length=40),
    source_id: Optional[str] = Query(None, max_length=160),
    asset_key: Optional[str] = Query(None, max_length=80),
    window: Optional[str] = Query(None, max_length=8),
    limit: int = Query(500, ge=1, le=5_000),
) -> JSONResponse:
    items = decision_service.project_public_reactions(
        db.query_market_reactions(
            source_type=source_type,
            source_id=source_id,
            asset_key=asset_key,
            window=window,
            limit=limit,
            eligible_events_only=True,
        )
    )
    return JSONResponse(
        {"items": items, "count": len(items)},
        headers=_PUBLIC_CACHE_HEADERS,
    )


def _latest_quotes(positions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    quotes: dict[str, dict[str, Any]] = {}
    asset_keys = {
        str(position.get("asset_key") or "").strip()
        for position in positions
        if str(position.get("asset_key") or "").strip()
    }
    for asset_key in sorted(asset_keys):
        bars = db.query_market_prices(asset_key=asset_key, limit=10_000)
        if not bars:
            continue
        latest = max(
            bars,
            key=lambda item: (
                int(item.get("timestamp") or 0),
                str(item.get("observed_at") or ""),
            ),
        )
        timestamp = int(latest.get("timestamp") or 0)
        observed_at = (
            datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
            if timestamp > 0
            else latest.get("observed_at")
        )
        quotes[asset_key] = {
            "status": "available",
            "price": latest.get("close"),
            "currency": latest.get("currency"),
            "observed_at": observed_at,
        }
    return quotes


def _build_private_decisions() -> tuple[dict[str, Any], dict[str, Any] | None]:
    public = _build_public_decisions()
    snapshot = db.latest_portfolio_snapshot()
    positions = (
        snapshot.get("positions")
        if isinstance(snapshot, Mapping)
        and isinstance(snapshot.get("positions"), list)
        else []
    )
    overlay = decision_service.build_private_overlay(
        public,
        positions,
        _latest_quotes(positions),
    )
    overlay["portfolio_available"] = snapshot is not None
    if snapshot is not None:
        overlay["portfolio_snapshot"] = {
            "snapshot_id": snapshot.get("snapshot_id"),
            "as_of": snapshot.get("as_of"),
            "created_at": snapshot.get("created_at"),
            "position_count": len(positions),
            "staleness": snapshot.get("staleness"),
        }
    return overlay, snapshot


def _ai_request_error(status_code: int, detail: str) -> None:
    raise HTTPException(
        status_code=status_code,
        detail=detail,
        headers=_NO_STORE_HEADERS,
    )


def _require_ai_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        return
    try:
        parsed = urlsplit(origin)
    except ValueError:
        _ai_request_error(403, "ai_same_origin_required")
    request_host = request.headers.get("host", "").strip().casefold()
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.netloc.casefold() != request_host
    ):
        _ai_request_error(403, "ai_same_origin_required")


def _resolve_ai_request_subject(
    subject_type: Any,
    subject_id: Any,
) -> dict[str, Any]:
    if not isinstance(subject_type, str) or subject_type not in _AI_SUBJECT_TYPES:
        _ai_request_error(422, "invalid_ai_subject")
    if not isinstance(subject_id, str):
        _ai_request_error(422, "invalid_ai_subject")
    public_id = subject_id.strip()
    if not public_id or len(public_id) > 160 or public_id != subject_id:
        _ai_request_error(422, "invalid_ai_subject")

    model = llm_enrichment.configured_model()
    if subject_type == "event":
        if not _EVENT_SUBJECT_ID.fullmatch(public_id):
            _ai_request_error(422, "invalid_ai_subject")
        event_id = int(public_id)
        if event_id > _SQLITE_MAX_INTEGER:
            _ai_request_error(422, "invalid_ai_subject")
        event = db.get_event_enrichment_subject(event_id)
        if event is None or not llm_enrichment.is_event_enrichment_eligible(event):
            _ai_request_error(404, "ai_subject_not_found")
        event_input, input_hash = llm_enrichment.build_event_input(event)
        return {
            "subject_type": "event",
            "subject_id": public_id,
            "subject_key": public_id,
            "input_hash": input_hash,
            "prompt_version": llm_enrichment.PROMPT_VERSION,
            "model": model,
            "event_input": event_input,
        }

    snapshot = db.latest_macro()
    monitored = (
        snapshot.get("monitored_events")
        if isinstance(snapshot, Mapping)
        else None
    )
    if not isinstance(monitored, list):
        _ai_request_error(404, "ai_subject_not_found")
    macro_event: Mapping[str, Any] | None = None
    for candidate in monitored[:24]:
        if not isinstance(candidate, Mapping):
            continue
        if str(candidate.get("id") or "").strip() == public_id:
            macro_event = candidate
            break
    if macro_event is None:
        _ai_request_error(404, "ai_subject_not_found")
    event_input, input_hash = llm_enrichment.build_macro_event_input(macro_event)
    return {
        "subject_type": "macro_event",
        "subject_id": public_id,
        "subject_key": llm_enrichment.macro_event_key(macro_event),
        "input_hash": input_hash,
        "prompt_version": llm_enrichment.MACRO_PROMPT_VERSION,
        "model": model,
        "event_input": event_input,
    }


def _touch_enrichment_pending() -> bool:
    """Wake the oneshot worker without allowing an env path to escape data/."""
    db_path = Path(db.DB_PATH).resolve()
    configured = os.environ.get("KOL_ENRICHMENT_PENDING_PATH", "").strip()
    marker = (
        Path(configured).expanduser()
        if configured
        else db_path.parent / "enrichment.pending"
    )
    if (
        marker.name != "enrichment.pending"
        or marker.parent.resolve() != db_path.parent
    ):
        return False
    flags = os.O_WRONLY | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(marker, flags, 0o600)
        try:
            os.utime(fd, None)
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


def _ai_status_response(
    subject: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    status_code: int = 200,
    wake_up: str | None = None,
) -> JSONResponse:
    payload = {
        key: value
        for key, value in result.items()
        if key in _AI_PUBLIC_RESPONSE_FIELDS and value is not None
    }
    payload["subject_type"] = subject["subject_type"]
    payload["subject_id"] = subject["subject_id"]
    if wake_up is not None:
        payload["wake_up"] = wake_up
    retry_after = payload.get("retry_after_seconds")
    headers = (
        {"Retry-After": str(int(retry_after))}
        if isinstance(retry_after, int) and retry_after > 0
        else None
    )
    return _private_response(
        payload,
        status_code=status_code,
        headers=headers,
    )


@app.post("/api/private/ai-requests")
async def api_request_ai_enrichment(
    request: Request,
    _: dict[str, Any] = Depends(require_private_session),
) -> JSONResponse:
    _require_ai_same_origin(request)
    if request.headers.get(_AI_ACTION_HEADER) != _AI_ACTION_VALUE:
        _ai_request_error(403, "ai_action_header_required")
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type.strip().lower() != "application/json":
        _ai_request_error(415, "json_body_required")
    declared_length = request.headers.get("content-length")
    if declared_length:
        try:
            if int(declared_length) > 2048:
                _ai_request_error(413, "ai_request_too_large")
        except ValueError:
            _ai_request_error(400, "invalid_content_length")
    raw = await request.body()
    if len(raw) > 2048:
        _ai_request_error(413, "ai_request_too_large")
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _ai_request_error(422, "invalid_ai_request")
    if not isinstance(body, Mapping) or set(body) != {"subject_type", "subject_id"}:
        _ai_request_error(422, "invalid_ai_request")
    subject = _resolve_ai_request_subject(
        body.get("subject_type"),
        body.get("subject_id"),
    )
    result = db.request_ai_enrichment(
        subject_type=subject["subject_type"],
        subject_key=subject["subject_key"],
        input_hash=subject["input_hash"],
        prompt_version=subject["prompt_version"],
        model=subject["model"],
    )
    if result["state"] == "rate_limited":
        return _ai_status_response(subject, result, status_code=429)
    wake_up = None
    if result["state"] == "queued":
        wake_up = "immediate" if _touch_enrichment_pending() else "timer"
    return _ai_status_response(subject, result, wake_up=wake_up)


@app.get("/api/private/ai-requests/status")
def api_ai_enrichment_request_status(
    request: Request,
    _: dict[str, Any] = Depends(require_private_session),
) -> JSONResponse:
    subject = _resolve_ai_request_subject(
        request.query_params.get("subject_type"),
        request.query_params.get("subject_id"),
    )
    result = db.get_ai_enrichment_request_status(
        subject_type=subject["subject_type"],
        subject_key=subject["subject_key"],
        input_hash=subject["input_hash"],
        prompt_version=subject["prompt_version"],
        model=subject["model"],
    )
    return _ai_status_response(subject, result)


@app.get("/api/private/decisions")
def api_private_decisions(
    _: dict[str, Any] = Depends(require_private_session),
) -> JSONResponse:
    overlay, _snapshot = _build_private_decisions()
    return _private_response(overlay)


@app.get("/api/private/portfolio-impact")
def api_private_portfolio_impact(
    _: dict[str, Any] = Depends(require_private_session),
) -> JSONResponse:
    overlay, snapshot = _build_private_decisions()
    if snapshot is None:
        return _private_response(
            {
                "schema_version": 1,
                "available": False,
                "reason": "portfolio_snapshot_unavailable",
                "decision_snapshot_id": overlay.get("snapshot_id"),
                "summary": overlay.get("portfolio_overview", {}),
                "matching_policy": "exact_asset_key_v1",
                "indirect_exposure_calculated": False,
                "trade_execution_available": False,
                "impacts": [],
                "unmatched_positions": [],
                "human_review_required": True,
            }
        )
    positions = snapshot.get("positions") or []
    impacts = [
        card
        for card in overlay.get("decisions", [])
        if isinstance(card, Mapping) and card.get("matched_positions")
    ]
    matched_assets = {
        str(card.get("asset_key"))
        for card in impacts
        if card.get("asset_key")
    }
    return _private_response(
        {
            "schema_version": 1,
            "available": True,
            "decision_snapshot_id": overlay.get("snapshot_id"),
            "snapshot": {
                "snapshot_id": snapshot.get("snapshot_id"),
                "as_of": snapshot.get("as_of"),
                "created_at": snapshot.get("created_at"),
                "position_count": len(positions),
                "staleness": snapshot.get("staleness"),
            },
            "summary": overlay.get("portfolio_overview", {}),
            "matching_policy": "exact_asset_key_v1",
            "indirect_exposure_calculated": False,
            "trade_execution_available": False,
            "impacts": impacts,
            "unmatched_positions": [
                position
                for position in positions
                if str(position.get("asset_key")) not in matched_assets
            ],
            "human_review_required": True,
        }
    )


@app.get("/api/private/options/overview")
def api_private_options_overview(
    _: dict[str, Any] = Depends(require_private_session),
) -> JSONResponse:
    """Return a fail-closed Options Lab research view without fetching markets."""

    return _private_response(
        options_research_service.build_options_overview(
            portfolio_snapshot=db.latest_portfolio_snapshot(),
            public_macro=_public_macro_snapshot(),
        )
    )


@app.post("/api/prune")
def api_prune(
    _: dict[str, Any] = Depends(require_private_session),
) -> JSONResponse:
    return _private_response({"pruned": db.prune_old()})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        str(BASE / "templates" / "index.html"),
        headers={"Cache-Control": "no-cache"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.environ.get("KOL_DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.environ.get("KOL_DASHBOARD_PORT", "8088")),
        log_level="info",
        access_log=False,
    )
