import json
import os
import queue
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, Response, abort, jsonify, request, send_from_directory


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ms_since(dt: datetime) -> int:
    return int((_now_utc() - dt).total_seconds() * 1000)


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


@dataclass
class TeamState:
    id: str
    name: str
    created_at: datetime
    route: List[str] = field(default_factory=list)
    current_index: int = 0
    collected: List[str] = field(default_factory=list)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def completed_count(self) -> int:
        return min(self.current_index, len(self.route))

    def current_mission(self) -> Optional[str]:
        if not self.route:
            return None
        if self.current_index >= len(self.route):
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
    teams: Dict[str, TeamState] = field(default_factory=dict)

    def reset(self) -> None:
        self.phase = "config"
        self.selected_games = []
        self.registration_open = False
        self.event_started_at = None
        self.teams = {}


state_lock = threading.RLock()
state = RuntimeState()


class SseBroker:
    def __init__(self) -> None:
        self._clients: List["queue.Queue[str]"] = []
        self._lock = threading.RLock()

    def add_client(self) -> "queue.Queue[str]":
        q: "queue.Queue[str]" = queue.Queue(maxsize=200)
        with self._lock:
            self._clients.append(q)
        return q

    def remove_client(self, q: "queue.Queue[str]") -> None:
        with self._lock:
            self._clients = [c for c in self._clients if c is not q]

    def publish(self, event: str, payload: Any) -> None:
        message = f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
        dead: List["queue.Queue[str]"] = []
        with self._lock:
            for c in self._clients:
                try:
                    c.put_nowait(message)
                except queue.Full:
                    dead.append(c)
            if dead:
                self._clients = [c for c in self._clients if c not in dead]


broker = SseBroker()


def compute_leaderboard() -> List[Dict[str, Any]]:
    with state_lock:
        started = state.event_started_at
        rows: List[Tuple[int, int, TeamState]] = []
        for t in state.teams.values():
            completed = t.completed_count()
            elapsed = t.elapsed_ms(started) or 0
            rows.append((-completed, elapsed, t))
        rows.sort(key=lambda x: (x[0], x[1], x[2].name.lower()))
        out: List[Dict[str, Any]] = []
        for idx, (_, elapsed, t) in enumerate(rows, start=1):
            current = t.current_mission()
            out.append(
                {
                    "rank": idx,
                    "teamId": t.id,
                    "teamName": t.name,
                    "completedMissions": t.completed_count(),
                    "elapsedMs": elapsed if started else None,
                    "currentMission": current,
                    "currentMissionName": GAME_BY_ID[current]["name"] if current else None,
                    "finished": t.finished_at is not None,
                }
            )
        return out


def snapshot_public_state() -> Dict[str, Any]:
    with state_lock:
        return {
            "phase": state.phase,
            "selectedGames": state.selected_games,
            "registrationOpen": state.registration_open,
            "registeredTeams": [
                {"teamId": t.id, "teamName": t.name, "createdAt": _iso(t.created_at)}
                for t in sorted(state.teams.values(), key=lambda x: x.created_at)
            ],
            "eventStartedAt": _iso(state.event_started_at) if state.event_started_at else None,
            "allGames": [
                {"id": g["id"], "name": g["name"], "letter": GAME_LETTER[g["id"]]}
                for g in ALL_GAMES
            ],
            "maxGames": 8,
        }


def broadcast_state() -> None:
    broker.publish("state", snapshot_public_state())
    broker.publish("leaderboard", compute_leaderboard())


def _require_json() -> Dict[str, Any]:
    if not request.is_json:
        abort(400, description="Expected JSON")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(400, description="Invalid JSON body")
    return body


def _parse_game_id_from_scan(text: str) -> Optional[str]:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("IH|GAME|"):
        gid = raw.split("|", 2)[2].strip().lower()
        return gid if gid in GAME_BY_ID else None
    if raw in GAME_BY_ID:
        return raw
    if "g=" in raw:
        idx = raw.find("g=")
        gid = raw[idx + 2 :]
        gid = gid.split("&", 1)[0].split("#", 1)[0].strip().lower()
        return gid if gid in GAME_BY_ID else None
    return None


app = Flask(__name__, static_folder=None)


FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))


@app.get("/api/public")
def api_public() -> Response:
    return jsonify(snapshot_public_state())


@app.get("/api/leaderboard")
def api_leaderboard() -> Response:
    return jsonify({"rows": compute_leaderboard()})


@app.get("/api/team/<team_id>")
def api_team(team_id: str) -> Response:
    with state_lock:
        t = state.teams.get(team_id)
        if not t:
            abort(404, description="Team not found")
        current = t.current_mission()
        return jsonify(
            {
                "teamId": t.id,
                "teamName": t.name,
                "phase": state.phase,
                "route": t.route,
                "currentIndex": t.current_index,
                "currentMission": current,
                "currentMissionName": GAME_BY_ID[current]["name"] if current else None,
                "collected": t.collected,
                "collectedCode": "".join(t.collected),
                "completedMissions": t.completed_count(),
                "eventStartedAt": _iso(state.event_started_at) if state.event_started_at else None,
                "finishedAt": _iso(t.finished_at) if t.finished_at else None,
                "elapsedMs": t.elapsed_ms(state.event_started_at),
            }
        )


@app.post("/api/coach/select-games")
def api_select_games() -> Response:
    body = _require_json()
    selected = body.get("selectedGames")
    if not isinstance(selected, list) or not all(isinstance(x, str) for x in selected):
        abort(400, description="selectedGames must be a list of strings")
    selected_ids = [_slugify(x) if x not in GAME_BY_ID else x for x in selected]
    selected_ids = [x for x in selected_ids if x in GAME_BY_ID]
    selected_ids = list(dict.fromkeys(selected_ids))
    if len(selected_ids) != 8:
        abort(400, description="Coach must select exactly 8 games")
    with state_lock:
        state.selected_games = selected_ids
        state.registration_open = True
        state.phase = "registration"
        state.event_started_at = None
        for t in state.teams.values():
            t.route = []
            t.current_index = 0
            t.collected = []
            t.started_at = None
            t.finished_at = None
    broadcast_state()
    return jsonify({"ok": True})


@app.post("/api/register")
def api_register() -> Response:
    body = _require_json()
    name = body.get("teamName")
    if not isinstance(name, str):
        abort(400, description="teamName required")
    name = name.strip()
    if len(name) < 1 or len(name) > 40:
        abort(400, description="teamName must be 1-40 characters")
    with state_lock:
        if not state.registration_open or state.phase != "registration":
            abort(403, description="Registration is closed")
        normalized = name.lower()
        if any(t.name.lower() == normalized for t in state.teams.values()):
            abort(409, description="Team name already registered")
        team_id = uuid.uuid4().hex
        state.teams[team_id] = TeamState(id=team_id, name=name, created_at=_now_utc())
    broadcast_state()
    return jsonify({"teamId": team_id, "teamName": name})


@app.post("/api/coach/start")
def api_start() -> Response:
    with state_lock:
        if state.phase != "registration":
            abort(409, description="Game cannot be started right now")
        if not state.selected_games or len(state.selected_games) != 8:
            abort(409, description="Games are not configured")
        state.registration_open = False
        state.phase = "live"
        state.event_started_at = _now_utc()
        started_iso = _iso(state.event_started_at)
        for t in state.teams.values():
            rng = random.Random(f"{t.id}:{started_iso}")
            route = list(state.selected_games)
            rng.shuffle(route)
            t.route = route
            t.current_index = 0
            t.collected = []
            t.started_at = state.event_started_at
            t.finished_at = None
    broadcast_state()
    return jsonify({"ok": True})


@app.post("/api/team/scan")
def api_scan() -> Response:
    body = _require_json()
    team_id = body.get("teamId")
    scanned = body.get("scannedText")
    if not isinstance(team_id, str) or not isinstance(scanned, str):
        abort(400, description="teamId and scannedText required")
    game_id = _parse_game_id_from_scan(scanned)
    if not game_id:
        abort(400, description="Invalid QR content")
    with state_lock:
        if state.phase != "live":
            abort(409, description="Game is not live")
        t = state.teams.get(team_id)
        if not t:
            abort(404, description="Team not found")
        current = t.current_mission()
        if not current:
            abort(409, description="No active mission")
        if game_id != current:
            return jsonify(
                {
                    "ok": False,
                    "result": "wrong_mission",
                    "expected": current,
                    "expectedName": GAME_BY_ID[current]["name"],
                    "scanned": game_id,
                    "scannedName": GAME_BY_ID[game_id]["name"],
                }
            )
        letter = GAME_LETTER.get(game_id, "?")
        if t.current_index < len(t.route):
            t.current_index += 1
            t.collected.append(letter)
        if t.current_index >= len(t.route) and t.finished_at is None:
            t.finished_at = _now_utc()
    broadcast_state()
    return jsonify(
        {
            "ok": True,
            "result": "verified",
            "gameId": game_id,
            "gameName": GAME_BY_ID[game_id]["name"],
            "letter": GAME_LETTER.get(game_id, "?"),
        }
    )


@app.post("/api/coach/shutdown")
def api_shutdown() -> Response:
    with state_lock:
        state.reset()
    broadcast_state()
    return jsonify({"ok": True})


@app.get("/api/stream")
def api_stream() -> Response:
    q = broker.add_client()
    initial_state = snapshot_public_state()
    initial_leaderboard = compute_leaderboard()

    def gen() -> Any:
        try:
            yield f"event: state\ndata: {json.dumps(initial_state, separators=(',', ':'))}\n\n"
            yield f"event: leaderboard\ndata: {json.dumps(initial_leaderboard, separators=(',', ':'))}\n\n"
            last_ping = time.time()
            while True:
                try:
                    msg = q.get(timeout=10)
                    yield msg
                except queue.Empty:
                    pass
                if time.time() - last_ping >= 20:
                    yield ": ping\n\n"
                    last_ping = time.time()
        finally:
            broker.remove_client(q)

    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/assets/<path:filename>")
def assets(filename: str) -> Response:
    return send_from_directory(FRONTEND_DIR, filename)


@app.get("/")
@app.get("/<path:pathname>")
def spa(pathname: str = "") -> Response:
    if pathname.startswith("api/"):
        abort(404)
    if pathname.startswith("assets/"):
        return send_from_directory(FRONTEND_DIR, pathname[len("assets/") :])
    return send_from_directory(FRONTEND_DIR, "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
