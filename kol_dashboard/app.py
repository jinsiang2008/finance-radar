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
  GET  /api/decisions       — complete public decision snapshot
  GET  /api/decisions/summary — small first-screen decision projection
  GET  /api/decisions/detail — one decision's evidence chain
  GET  /api/relations       — public mechanism relations
  GET  /api/market/reactions — public market validation records
  POST /api/auth/login      — unlock private mode
  GET  /api/auth/status     — private-mode session status
  POST /api/auth/logout     — clear private-mode session
  GET  /api/private/*       — authenticated portfolio overlay
  POST /api/prune           — manual prune (admin)

Environment:
  KOL_DASHBOARD_PORT        — default 8088
  KOL_DASHBOARD_HOST        — default 127.0.0.1
  KOL_DASHBOARD_DB          — sqlite path
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Path as ApiPath, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import auth
import db
import decision_snapshot
import decision_service
import llm_enrichment

BASE = Path(__file__).parent
app = FastAPI(title="KOL Dashboard + Macro Risk Radar", version="2.0")

app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

LOGIN_LIMITER = auth.LoginRateLimiter(max_attempts=5, window_seconds=15 * 60)
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}
_PUBLIC_CACHE_HEADERS = {"Cache-Control": "public, max-age=30"}


class LoginBody(BaseModel):
    passcode: str = Field(min_length=1, max_length=256)


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


def _private_response(payload: Any, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers=_NO_STORE_HEADERS,
    )


@app.on_event("startup")
def _init_db() -> None:
    db.init()


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
    kol: Optional[str] = Query(None),
    hours: Optional[int] = Query(24, ge=1, le=720),
    impact: Optional[str] = Query(None),
    q: Optional[str] = Query(None, max_length=120),
    time_status: Literal["verified", "unverified"] = Query("verified"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    items = db.query_events(
        kol=kol,
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
        raise HTTPException(status_code=404, detail="event_not_found")
    event = detail["event"]
    if not llm_enrichment.is_event_enrichment_eligible(event):
        raise HTTPException(status_code=404, detail="event_not_available")
    event["tickers"] = [
        ticker for ticker in (event.get("tickers") or "").split(",") if ticker
    ]
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
        if selected_sighting:
            for field in (
                "source_url",
                "source",
                "kol_key",
                "kol_name",
                "kol_name_cn",
                "published_at",
                "time_status",
                "first_seen_at",
                "last_seen_at",
                "source_count",
            ):
                event[field] = selected_sighting.get(field)
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
    return JSONResponse(
        {
            **detail,
            "relations": relations,
            "market_reactions": reactions,
        },
        headers=_PUBLIC_CACHE_HEADERS,
    )

@app.get("/api/macro")
def api_macro() -> JSONResponse:
    snap = db.latest_macro()
    if not snap:
        return JSONResponse(
            {"available": False, "reason": "尚未采集到宏观快照，请运行 macro_collect.py"},
            status_code=200,
        )
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

    public_snapshot = decision_service.project_public_macro(
        snap,
        macro_event_enrichments=matching,
    )
    public_snapshot["available"] = True
    return JSONResponse(public_snapshot)


@app.get("/api/macro/history")
def api_macro_history(limit: int = Query(60, ge=2, le=500)) -> dict:
    return {"items": db.macro_history(limit=limit)}


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
