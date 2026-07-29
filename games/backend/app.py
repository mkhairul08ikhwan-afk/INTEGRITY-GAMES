import json
import logging
import os
import math
import queue
import random
import re
import threading
import time
import uuid
import urllib.request
from io import BytesIO
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

import cv2
import numpy as np
import pyqrcode
from flask import Flask, Response, g, jsonify, request, send_from_directory, stream_with_context
from werkzeug.exceptions import HTTPException, NotFound


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _slugify(name: str) -> str:
    out = []
    for ch in name.lower().strip():
        if ch.isalnum():
            out.append(ch)
        elif ch in {" ", "-", "_"}:
            out.append("_")
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug


def _build_qr_png(text: str, target_size: Optional[int] = None) -> bytes:
    quiet_zone = 4
    qr = pyqrcode.create(text, error="H")
    module_span = (17 + (qr.version * 4)) + (quiet_zone * 2)
    scale = 12
    if target_size:
        scale = max(1, math.ceil(target_size / module_span))
    buff = BytesIO()
    qr.png(buff, scale=scale, quiet_zone=quiet_zone)
    return buff.getvalue()


def _decode_qr_image(image_bytes: bytes) -> Optional[str]:
    img_array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if image is None:
        return None
    detector = cv2.QRCodeDetector()
    decoded_text, _, _ = detector.detectAndDecode(image)
    decoded_text = (decoded_text or "").strip()
    return decoded_text or None


def _configure_logging() -> logging.Logger:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logger = logging.getLogger("integrity_hunting")
    logger.setLevel(level)
    return logger


LOGGER = _configure_logging()
SERVER_INSTANCE_ID = uuid.uuid4().hex
SERVER_BOOTED_AT = _now_utc()
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
TEAM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")
MAX_SELECTED_GAMES = 8
ASSET_MAX_AGE_SECONDS = 3600


# #region debug-point A:debug-report
def _debug_report(hypothesis_id: str, msg: str, data: Optional[Dict[str, Any]] = None, run_id: str = "pre-fix") -> None:
    payload = json.dumps(
        {
            "sessionId": "live-qr-rank",
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": "backend/app.py",
            "msg": f"[DEBUG] {msg}",
            "data": data or {},
            "ts": int(time.time() * 1000),
        }
    ).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                "http://127.0.0.1:7777/event",
                data=payload,
                headers={"Content-Type": "application/json"},
            ),
            timeout=0.3,
        ).read()
    except Exception:
        pass


# #endregion

ALL_GAMES: List[Dict[str, str]] = [
    {"id": "integrity_mat", "name": "Integrity Mat"},
    {"id": "integrity_rope", "name": "Integrity Rope"},
    {"id": "integrity_balloon", "name": "Integrity Balloon"},
    {"id": "integrity_maze", "name": "Integrity Maze"},
    {"id": "integrity_dice", "name": "Integrity Dice"},
    {"id": "integrity_pipe", "name": "Integrity Pipe"},
    {"id": "integrity_cup", "name": "Integrity Cup"},
    {"id": "integrity_echo", "name": "Integrity Echo"},
    {"id": "height_quest", "name": "Height Quest"},
    {"id": "snake_rush", "name": "Snake Rush"},
    {"id": "bearded_fox", "name": "Bearded Fox"},
    {"id": "code_breaker", "name": "Code Breaker"},
]
GAME_BY_ID: Dict[str, Dict[str, str]] = {g["id"]: g for g in ALL_GAMES}
GAME_LETTER: Dict[str, str] = {
    "integrity_mat": "A",
    "integrity_rope": "B",
    "integrity_balloon": "C",
    "integrity_maze": "D",
    "integrity_dice": "E",
    "integrity_pipe": "F",
    "integrity_cup": "G",
    "integrity_echo": "H",
    "height_quest": "I",
    "snake_rush": "J",
    "bearded_fox": "K",
    "code_breaker": "L",
}


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}


@dataclass
class TeamState:
    id: str
    name: str
    created_at: datetime
    route: List[str] = field(default_factory=list)
    current_index: int = 0
    collected: List[str] = field(default_factory=list)
    completed_game_ids: Set[str] = field(default_factory=set)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def reset_progress(self) -> None:
        self.route = []
        self.current_index = 0
        self.collected = []
        self.completed_game_ids = set()
        self.started_at = None
        self.finished_at = None

    def completed_count(self) -> int:
        return min(self.current_index, len(self.route))

    def current_mission(self) -> Optional[str]:
        if not self.route or self.current_index >= len(self.route):
            return None
        return self.route[self.current_index]

    def elapsed_ms(self, event_start: Optional[datetime]) -> Optional[int]:
        if not event_start:
            return None
        end = self.finished_at or _now_utc()
        return int((end - event_start).total_seconds() * 1000)


@dataclass
class RuntimeState:
    phase: str = "config"
    selected_games: List[str] = field(default_factory=list)
    registration_open: bool = False
    event_started_at: Optional[datetime] = None
    current_event_id: Optional[str] = None
    teams: Dict[str, TeamState] = field(default_factory=dict)
    last_reset_reason: str = "server_boot"
    last_reset_at: datetime = field(default_factory=_now_utc)

    def reset(self, reason: str) -> None:
        self.phase = "config"
        self.selected_games = []
        self.registration_open = False
        self.event_started_at = None
        self.current_event_id = None
        self.teams.clear()
        self.last_reset_reason = reason
        self.last_reset_at = _now_utc()


state_lock = threading.RLock()
state = RuntimeState(last_reset_reason="server_boot", last_reset_at=SERVER_BOOTED_AT)


class SseBroker:
    def __init__(self) -> None:
        self._clients: Dict[str, "queue.Queue[str]"] = {}
        self._lock = threading.RLock()

    def add_client(self) -> Tuple[str, "queue.Queue[str]"]:
        client_id = uuid.uuid4().hex
        client_queue: "queue.Queue[str]" = queue.Queue(maxsize=50)
        with self._lock:
            self._clients[client_id] = client_queue
        return client_id, client_queue

    def remove_client(self, client_id: str) -> None:
        with self._lock:
            self._clients.pop(client_id, None)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def publish(self, event: str, payload: Any) -> None:
        message = f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
        dead_clients: List[str] = []
        with self._lock:
            for client_id, client_queue in self._clients.items():
                try:
                    client_queue.put_nowait(message)
                except queue.Full:
                    dead_clients.append(client_id)
            for client_id in dead_clients:
                self._clients.pop(client_id, None)


broker = SseBroker()


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "-"


def _request_id() -> str:
    return getattr(g, "request_id", "unknown")


def _raise_api_error(status_code: int, code: str, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
    raise ApiError(status_code, code, message, extra)


def _json_error_payload(status_code: int, code: str, message: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "status": status_code,
            "requestId": _request_id(),
        },
    }
    if extra:
        payload["error"].update(extra)
    return payload


def _normalize_team_name(name: Any) -> str:
    if not isinstance(name, str):
        _raise_api_error(400, "invalid_team_name", "teamName must be a string.")
    cleaned = " ".join(name.strip().split())
    if not cleaned:
        _raise_api_error(400, "invalid_team_name", "teamName is required.")
    if len(cleaned) > 40:
        _raise_api_error(400, "invalid_team_name", "teamName must be 1-40 characters.")
    if not TEAM_NAME_RE.fullmatch(cleaned):
        _raise_api_error(400, "invalid_team_name", "teamName may only contain letters, numbers, spaces, hyphens, and underscores.")
    return cleaned


def _require_json() -> Dict[str, Any]:
    if request.mimetype != "application/json":
        _raise_api_error(415, "unsupported_media_type", "Expected application/json request body.")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        _raise_api_error(400, "invalid_json", "Invalid JSON body.")
    return body


def _require_event_available() -> None:
    if state.current_event_id is None and state.phase == "config" and not state.teams:
        _raise_api_error(
            410,
            "event_reset",
            "This event is no longer available. The coach may have shut down the game or the server restarted.",
            {
                "serverInstanceId": SERVER_INSTANCE_ID,
                "serverBootedAt": _iso(SERVER_BOOTED_AT),
            },
        )


def _parse_game_id_from_scan(text: Any) -> str:
    if not isinstance(text, str):
        _raise_api_error(400, "invalid_scan", "scannedText must be a string.")
    raw = text.strip()
    if not raw:
        _raise_api_error(400, "invalid_scan", "scannedText is required.")
    if raw.startswith("IH|GAME|"):
        game_id = raw.split("|", 2)[2].strip().lower()
    elif raw in GAME_BY_ID:
        game_id = raw
    elif "g=" in raw:
        idx = raw.find("g=")
        game_id = raw[idx + 2 :].split("&", 1)[0].split("#", 1)[0].strip().lower()
    else:
        game_id = raw.lower()
    if game_id not in GAME_BY_ID:
        _raise_api_error(400, "invalid_scan", "QR content does not match a known game.")
    return game_id


def _validate_selected_games(selected: Any) -> List[str]:
    if not isinstance(selected, list):
        _raise_api_error(400, "invalid_selected_games", "selectedGames must be a list of strings.")
    if len(selected) != MAX_SELECTED_GAMES:
        _raise_api_error(400, "invalid_selected_games", f"Coach must select exactly {MAX_SELECTED_GAMES} games.")
    normalized: List[str] = []
    for item in selected:
        if not isinstance(item, str):
            _raise_api_error(400, "invalid_selected_games", "selectedGames must contain only strings.")
        game_id = item if item in GAME_BY_ID else _slugify(item)
        if game_id not in GAME_BY_ID:
            _raise_api_error(400, "invalid_selected_games", f"Unknown game: {item}")
        normalized.append(game_id)
    unique_ids = list(dict.fromkeys(normalized))
    if len(unique_ids) != MAX_SELECTED_GAMES:
        _raise_api_error(400, "invalid_selected_games", f"Coach must select exactly {MAX_SELECTED_GAMES} unique games.")
    return unique_ids


def _team_snapshot(team: TeamState) -> Dict[str, Any]:
    current = team.current_mission()
    return {
        "teamId": team.id,
        "teamName": team.name,
        "phase": state.phase,
        "currentEventId": state.current_event_id,
        "route": team.route,
        "currentIndex": team.current_index,
        "currentMission": current,
        "currentMissionName": GAME_BY_ID[current]["name"] if current else None,
        "collected": team.collected,
        "collectedCode": "".join(team.collected),
        "completedMissions": team.completed_count(),
        "eventStartedAt": _iso(state.event_started_at) if state.event_started_at else None,
        "finishedAt": _iso(team.finished_at) if team.finished_at else None,
        "elapsedMs": team.elapsed_ms(state.event_started_at),
        "serverInstanceId": SERVER_INSTANCE_ID,
        "serverBootedAt": _iso(SERVER_BOOTED_AT),
    }


def compute_leaderboard() -> List[Dict[str, Any]]:
    with state_lock:
        started = state.event_started_at
        rows: List[Tuple[int, int, TeamState]] = []
        for team in state.teams.values():
            completed = team.completed_count()
            elapsed = team.elapsed_ms(started) or 0
            rows.append((-completed, elapsed, team))
        rows.sort(key=lambda item: (item[0], item[1], item[2].name.lower()))
        leaderboard_rows = [
            {
                "rank": index,
                "teamId": team.id,
                "teamName": team.name,
                "completedMissions": team.completed_count(),
                "elapsedMs": elapsed if started else None,
                "currentMission": current,
                "currentMissionName": GAME_BY_ID[current]["name"] if current else None,
                "finished": team.finished_at is not None,
            }
            for index, (_, elapsed, team) in enumerate(rows, start=1)
            for current in [team.current_mission()]
        ]
        # #region debug-point E:leaderboard-compute
        _debug_report(
            "E",
            "Computed leaderboard",
            {
                "rowCount": len(leaderboard_rows),
                "rows": [
                    {
                        "rank": row["rank"],
                        "teamName": row["teamName"],
                        "completedMissions": row["completedMissions"],
                        "elapsedMs": row["elapsedMs"],
                    }
                    for row in leaderboard_rows
                ],
            },
        )
        # #endregion
        return leaderboard_rows


def snapshot_public_state() -> Dict[str, Any]:
    with state_lock:
        return {
            "phase": state.phase,
            "selectedGames": state.selected_games,
            "registrationOpen": state.registration_open,
            "registeredTeams": [
                {"teamId": team.id, "teamName": team.name, "createdAt": _iso(team.created_at)}
                for team in sorted(state.teams.values(), key=lambda item: item.created_at)
            ],
            "eventStartedAt": _iso(state.event_started_at) if state.event_started_at else None,
            "currentEventId": state.current_event_id,
            "serverInstanceId": SERVER_INSTANCE_ID,
            "serverBootedAt": _iso(SERVER_BOOTED_AT),
            "lastResetReason": state.last_reset_reason,
            "lastResetAt": _iso(state.last_reset_at),
            "allGames": [
                {"id": game["id"], "name": game["name"], "letter": GAME_LETTER[game["id"]]}
                for game in ALL_GAMES
            ],
            "maxGames": MAX_SELECTED_GAMES,
        }


def broadcast_state() -> None:
    # #region debug-point C:broadcast-state
    _debug_report(
        "C",
        "Broadcasting state",
        {
            "phase": state.phase,
            "teamCount": len(state.teams),
            "registrationOpen": state.registration_open,
            "selectedGames": list(state.selected_games),
        },
    )
    # #endregion
    broker.publish("state", snapshot_public_state())
    broker.publish("leaderboard", compute_leaderboard())


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config.update(
        JSON_SORT_KEYS=False,
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    )

    @app.before_request
    def before_request() -> None:
        g.request_id = uuid.uuid4().hex[:12]
        g.started_at = time.perf_counter()

    @app.after_request
    def after_request(response: Response) -> Response:
        duration_ms = int((time.perf_counter() - getattr(g, "started_at", time.perf_counter())) * 1000)
        response.headers["X-Request-Id"] = _request_id()
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(self)"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "media-src 'self' blob:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'; "
            "form-action 'self'"
        )

        path = request.path or "/"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        elif path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-cache, max-age=0"
        else:
            response.headers["Cache-Control"] = "no-cache"

        if path != "/api/stream":
            LOGGER.info(
                "request_id=%s method=%s path=%s status=%s duration_ms=%s ip=%s",
                _request_id(),
                request.method,
                path,
                response.status_code,
                duration_ms,
                _client_ip(),
            )
        return response

    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError) -> Response:
        payload = _json_error_payload(error.status_code, error.code, error.message, error.extra)
        LOGGER.warning(
            "request_id=%s api_error code=%s status=%s path=%s message=%s",
            _request_id(),
            error.code,
            error.status_code,
            request.path,
            error.message,
        )
        return jsonify(payload), error.status_code

    @app.errorhandler(HTTPException)
    def handle_http_exception(error: HTTPException) -> Response:
        if request.path.startswith("/api/"):
            code = str(error.name).lower().replace(" ", "_")
            payload = _json_error_payload(error.code or 500, code, error.description or error.name)
            return jsonify(payload), error.code or 500
        return error

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception) -> Response:
        LOGGER.exception("request_id=%s unexpected_error path=%s", _request_id(), request.path)
        if request.path.startswith("/api/"):
            payload = _json_error_payload(500, "internal_server_error", "Unexpected server error.")
            return jsonify(payload), 500
        return Response("Internal Server Error", status=500, mimetype="text/plain")

    @app.get("/api/public")
    def api_public() -> Response:
        return jsonify(snapshot_public_state())

    @app.get("/api/leaderboard")
    def api_leaderboard() -> Response:
        return jsonify({"rows": compute_leaderboard()})

    @app.get("/api/team/<team_id>")
    def api_team(team_id: str) -> Response:
        with state_lock:
            team = state.teams.get(team_id)
            if not team:
                _require_event_available()
                _raise_api_error(404, "team_not_found", "Team not found.")
            return jsonify(_team_snapshot(team))

    @app.post("/api/coach/select-games")
    def api_select_games() -> Response:
        body = _require_json()
        selected_ids = _validate_selected_games(body.get("selectedGames"))
        # #region debug-point D:api-select-games
        _debug_report("D", "Coach submitted selected games", {"selectedGames": selected_ids, "count": len(selected_ids)})
        # #endregion
        with state_lock:
            if state.phase == "live":
                _raise_api_error(409, "game_live", "Cannot reconfigure games while the event is live.")
            state.selected_games = selected_ids
            state.registration_open = True
            state.phase = "registration"
            state.event_started_at = None
            state.current_event_id = uuid.uuid4().hex
            for team in state.teams.values():
                team.reset_progress()
        broadcast_state()
        return jsonify({"ok": True, "selectedGames": selected_ids})

    @app.post("/api/register")
    def api_register() -> Response:
        body = _require_json()
        team_name = _normalize_team_name(body.get("teamName"))
        # #region debug-point C:api-register-request
        _debug_report(
            "C",
            "Participant registration request",
            {
                "teamName": team_name,
                "phase": state.phase,
                "registrationOpen": state.registration_open,
                "currentEventId": state.current_event_id,
            },
        )
        # #endregion
        with state_lock:
            if not state.registration_open or state.phase != "registration" or not state.current_event_id:
                _raise_api_error(403, "registration_closed", "Registration is closed.")
            normalized = team_name.casefold()
            if any(team.name.casefold() == normalized for team in state.teams.values()):
                _raise_api_error(409, "duplicate_team_name", "Team name already registered.")
            team_id = uuid.uuid4().hex
            state.teams[team_id] = TeamState(id=team_id, name=team_name, created_at=_now_utc())
            # #region debug-point C:api-register-stored
            _debug_report("C", "Participant stored", {"teamId": team_id, "teamName": team_name, "teamCount": len(state.teams)})
            # #endregion
        broadcast_state()
        return jsonify({"ok": True, "teamId": team_id, "teamName": team_name}), 201

    @app.post("/api/coach/start")
    def api_start() -> Response:
        # #region debug-point E:api-start-request
        _debug_report("E", "Coach requested start", {"phase": state.phase, "teamCount": len(state.teams)})
        # #endregion
        with state_lock:
            if state.phase != "registration":
                _raise_api_error(409, "invalid_phase", "Game cannot be started right now.")
            if not state.selected_games:
                _raise_api_error(409, "games_not_configured", "At least 1 game must be configured before starting.")
            if not state.current_event_id:
                state.current_event_id = uuid.uuid4().hex
            state.registration_open = False
            state.phase = "live"
            state.event_started_at = _now_utc()
            started_iso = _iso(state.event_started_at)
            for team in state.teams.values():
                rng = random.Random(f"{state.current_event_id}:{team.id}:{started_iso}")
                route = list(state.selected_games)
                rng.shuffle(route)
                team.reset_progress()
                team.route = route
                team.started_at = state.event_started_at
        broadcast_state()
        return jsonify({"ok": True, "eventStartedAt": _iso(state.event_started_at)})

    @app.post("/api/team/scan")
    def api_scan() -> Response:
        body = _require_json()
        team_id = body.get("teamId")
        if not isinstance(team_id, str) or not team_id.strip():
            _raise_api_error(400, "invalid_team_id", "teamId is required.")
        game_id = _parse_game_id_from_scan(body.get("scannedText"))
        with state_lock:
            if state.phase != "live":
                _require_event_available()
                _raise_api_error(409, "game_not_live", "Game is not live.")
            team = state.teams.get(team_id)
            if not team:
                _require_event_available()
                _raise_api_error(404, "team_not_found", "Team not found.")
            if team.finished_at:
                _raise_api_error(409, "team_finished", "All missions are already completed.")
            if game_id in team.completed_game_ids:
                _raise_api_error(
                    409,
                    "duplicate_scan",
                    "This QR has already been scanned by this team.",
                    {"gameId": game_id, "gameName": GAME_BY_ID[game_id]["name"]},
                )
            current = team.current_mission()
            if not current:
                _raise_api_error(409, "no_active_mission", "No active mission.")
            if game_id != current:
                _raise_api_error(
                    409,
                    "wrong_mission",
                    "Please complete your current mission first.",
                    {
                        "expected": current,
                        "expectedName": GAME_BY_ID[current]["name"],
                        "scanned": game_id,
                        "scannedName": GAME_BY_ID[game_id]["name"],
                    },
                )
            letter = GAME_LETTER[game_id]
            team.current_index += 1
            team.collected.append(letter)
            team.completed_game_ids.add(game_id)
            if team.current_index >= len(team.route):
                team.finished_at = _now_utc()
        broadcast_state()
        return jsonify(
            {
                "ok": True,
                "result": "verified",
                "gameId": game_id,
                "gameName": GAME_BY_ID[game_id]["name"],
                "letter": GAME_LETTER[game_id],
            }
        )

    @app.post("/api/coach/shutdown")
    def api_shutdown() -> Response:
        with state_lock:
            state.reset("coach_shutdown")
        broadcast_state()
        return jsonify({"ok": True, "cleared": True, "phase": state.phase})

    @app.get("/api/stream")
    def api_stream() -> Response:
        client_id, client_queue = broker.add_client()
        initial_state = snapshot_public_state()
        initial_leaderboard = compute_leaderboard()
        LOGGER.info("sse_connected client_id=%s total_clients=%s", client_id, broker.client_count())

        @stream_with_context
        def event_stream() -> Generator[str, None, None]:
            try:
                yield "retry: 2000\n\n"
                yield f"event: state\ndata: {json.dumps(initial_state, separators=(',', ':'))}\n\n"
                yield f"event: leaderboard\ndata: {json.dumps(initial_leaderboard, separators=(',', ':'))}\n\n"
                last_ping = time.time()
                while True:
                    try:
                        message = client_queue.get(timeout=12)
                        yield message
                    except queue.Empty:
                        pass
                    if time.time() - last_ping >= 15:
                        yield ": ping\n\n"
                        last_ping = time.time()
            finally:
                broker.remove_client(client_id)
                LOGGER.info("sse_disconnected client_id=%s total_clients=%s", client_id, broker.client_count())

        return Response(
            event_stream(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/qr")
    def api_qr() -> Response:
        text = str(request.args.get("text", "")).strip()
        size_raw = str(request.args.get("size", "")).strip()
        if not text:
            _raise_api_error(400, "validation_error", "Query parameter 'text' is required.")
        if len(text) > 1024:
            _raise_api_error(400, "validation_error", "QR content is too long.")
        target_size: Optional[int] = None
        if size_raw:
            try:
                target_size = int(size_raw)
            except ValueError:
                _raise_api_error(400, "validation_error", "Query parameter 'size' must be an integer.")
            if target_size < 128 or target_size > 2048:
                _raise_api_error(400, "validation_error", "Query parameter 'size' must be between 128 and 2048.")
        try:
            png_bytes = _build_qr_png(text, target_size=target_size)
        except Exception:
            LOGGER.exception("qr_render_failed")
            _raise_api_error(500, "qr_render_failed", "Failed to generate QR code.")
        return Response(png_bytes, mimetype="image/png")

    @app.post("/api/qr/decode")
    def api_qr_decode() -> Response:
        image_file = request.files.get("image")
        if image_file is None:
            _raise_api_error(400, "missing_image", "Image upload is required.")
        image_bytes = image_file.read()
        if not image_bytes:
            _raise_api_error(400, "missing_image", "Uploaded image is empty.")
        decoded = _decode_qr_image(image_bytes)
        if not decoded:
            _raise_api_error(422, "qr_not_found", "No QR code could be decoded from the captured image.")
        return jsonify({"ok": True, "scannedText": decoded})

    @app.get("/assets/<path:filename>")
    def assets(filename: str) -> Response:
        return send_from_directory(FRONTEND_DIR, filename, max_age=ASSET_MAX_AGE_SECONDS)

    @app.get("/")
    @app.get("/<path:pathname>")
    def spa(pathname: str = "") -> Response:
        if pathname.startswith("api/"):
            raise NotFound()
        if pathname.startswith("assets/"):
            return send_from_directory(FRONTEND_DIR, pathname[len("assets/") :], max_age=ASSET_MAX_AGE_SECONDS)
        if pathname and "." in Path(pathname).name:
            raise NotFound()
        return send_from_directory(FRONTEND_DIR, "index.html", max_age=0)

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
