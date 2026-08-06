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
  GET  /api/decisions       — public risk/opportunity decision cards
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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import auth
import db
import decision_service

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
    public_snapshot = decision_service.project_public_macro(snap)
    public_snapshot["available"] = True
    return JSONResponse(public_snapshot)


@app.get("/api/macro/history")
def api_macro_history(limit: int = Query(60, ge=2, le=500)) -> dict:
    return {"items": db.macro_history(limit=limit)}


def _macro_coverage() -> float:
    snapshot = db.latest_macro()
    if not isinstance(snapshot, Mapping):
        return 0.0
    coverage = snapshot.get("data_coverage")
    if not isinstance(coverage, Mapping):
        return 0.0
    available = coverage.get("available")
    total = coverage.get("total")
    if (
        isinstance(available, (int, float))
        and not isinstance(available, bool)
        and isinstance(total, (int, float))
        and not isinstance(total, bool)
        and total > 0
    ):
        return round(max(0.0, min(1.0, float(available) / float(total))), 4)
    return 0.0


def _build_public_decisions() -> dict[str, Any]:
    relations = db.query_decision_relations(
        event_max_age_hours=decision_service.EVENT_RELATION_MAX_AGE_HOURS
    )
    reactions = db.query_market_reactions(limit=5_000)
    return decision_service.build_public_decisions(
        relations,
        reactions,
        _macro_coverage(),
    )


@app.get("/api/decisions")
def api_decisions() -> JSONResponse:
    return JSONResponse(
        _build_public_decisions(),
        headers=_PUBLIC_CACHE_HEADERS,
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
                "available": False,
                "reason": "portfolio_snapshot_unavailable",
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
            "available": True,
            "snapshot": {
                "snapshot_id": snapshot.get("snapshot_id"),
                "as_of": snapshot.get("as_of"),
                "created_at": snapshot.get("created_at"),
                "position_count": len(positions),
                "staleness": snapshot.get("staleness"),
            },
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
