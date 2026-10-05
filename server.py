"""Tiny, local-first telemetry collector for agentic systems."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


DB_PATH = Path(os.environ.get("TELEMETRY_DB", "telemetry.db"))
MAX_BODY_BYTES = 1_000_000


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            run_id TEXT NOT NULL,
            agent_id TEXT,
            parent_agent_id TEXT,
            task_id TEXT,
            status TEXT,
            attributes TEXT NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, timestamp)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id, timestamp)")
    return db


def normalize_event(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Each event must be a JSON object")
    event_type = value.get("event_type")
    run_id = value.get("run_id")
    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError("event_type is required")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id is required")
    attributes = value.get("attributes", {})
    if not isinstance(attributes, dict):
        raise ValueError("attributes must be a JSON object")
    timestamp = value.get("timestamp")
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    if not isinstance(timestamp, str):
        raise ValueError("timestamp must be an ISO 8601 string")
    return {
        "id": str(value.get("id") or uuid.uuid4()),
        "timestamp": timestamp,
        "event_type": event_type.strip(),
        "run_id": run_id.strip(),
        "agent_id": value.get("agent_id"),
        "parent_agent_id": value.get("parent_agent_id"),
        "task_id": value.get("task_id"),
        "status": value.get("status"),
        "attributes": attributes,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentTelemetry/0.1"

    def send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> object:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError(f"Request body must be between 1 and {MAX_BODY_BYTES} bytes")
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Request body must be valid JSON") from exc

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            page = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/health":
            self.send_json(200, {"ok": True, "service": "agent-telemetry", "version": "0.1.0"})
            return
        if parsed.path != "/v1/events":
            self.send_json(404, {"error": "not_found"})
            return
        params = parse_qs(parsed.query)
        clauses, values = [], []
        for key in ("run_id", "agent_id", "event_type", "task_id", "status"):
            if params.get(key):
                clauses.append(f"{key} = ?")
                values.append(params[key][0])
        try:
            limit = min(max(int(params.get("limit", ["100"])[0]), 1), 1000)
        except ValueError:
            self.send_json(400, {"error": "limit must be an integer"})
            return
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with connect() as db:
            rows = db.execute(
                f"SELECT * FROM events{where} ORDER BY timestamp DESC LIMIT ?", (*values, limit)
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["attributes"] = json.loads(event["attributes"])
            events.append(event)
        self.send_json(200, {"events": events, "count": len(events)})

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/v1/events":
            self.send_json(404, {"error": "not_found"})
            return
        try:
            payload = self.read_json()
            batch = payload if isinstance(payload, list) else [payload]
            if not batch or len(batch) > 100:
                raise ValueError("Send between 1 and 100 events per request")
            events = [normalize_event(item) for item in batch]
            with connect() as db:
                db.executemany(
                    """INSERT INTO events
                    (id, timestamp, event_type, run_id, agent_id, parent_agent_id, task_id, status, attributes)
                    VALUES (:id, :timestamp, :event_type, :run_id, :agent_id, :parent_agent_id,
                            :task_id, :status, :attributes)""",
                    [
                        {**event, "attributes": json.dumps(event["attributes"], ensure_ascii=False)}
                        for event in events
                    ],
                )
            self.send_json(202, {"accepted": len(events), "event_ids": [event["id"] for event in events]})
        except ValueError as exc:
            self.send_json(400, {"error": "invalid_event", "message": str(exc)})
        except sqlite3.IntegrityError:
            self.send_json(409, {"error": "duplicate_event_id"})

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.log_date_time_string()} {self.address_string()} {fmt % args}")


if __name__ == "__main__":
    host = os.environ.get("TELEMETRY_HOST", "127.0.0.1")
    port = int(os.environ.get("TELEMETRY_PORT", "8000"))
    print(f"Agent telemetry alpha listening on http://{host}:{port} (database: {DB_PATH})")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
