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
MAX_BODY_BYTES = 10_000_000


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
    db.execute(
        """CREATE TABLE IF NOT EXISTS spans (
            trace_id TEXT NOT NULL,
            span_id TEXT NOT NULL,
            parent_span_id TEXT,
            name TEXT NOT NULL,
            service_name TEXT,
            start_ns INTEGER NOT NULL,
            end_ns INTEGER NOT NULL,
            kind INTEGER NOT NULL DEFAULT 0,
            status_code INTEGER NOT NULL DEFAULT 0,
            status_message TEXT,
            attributes TEXT NOT NULL,
            events TEXT NOT NULL,
            PRIMARY KEY (trace_id, span_id)
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id, start_ns)")
    return db


def otlp_value(value: object) -> object:
    """Decode an OTLP JSON AnyValue into ordinary JSON-compatible values."""
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "boolValue", "intValue", "doubleValue", "bytesValue"):
        if key in value:
            return value[key]
    if "arrayValue" in value:
        return [otlp_value(item) for item in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return otlp_attributes(value["kvlistValue"].get("values", []))
    return value


def otlp_attributes(values: object) -> dict:
    if isinstance(values, dict):
        return {str(key): otlp_value(value) for key, value in values.items()}
    if not isinstance(values, list):
        raise ValueError("OTLP attributes must be an array or object")
    result = {}
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            raise ValueError("Each OTLP attribute must have a string key")
        result[item["key"]] = otlp_value(item.get("value"))
    return result


def flatten_otlp_spans(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("resourceSpans"), list):
        raise ValueError("Expected an OTLP ExportTraceServiceRequest with resourceSpans")
    result = []
    for resource_group in payload["resourceSpans"]:
        if not isinstance(resource_group, dict):
            raise ValueError("Each resourceSpans item must be an object")
        resource_data = resource_group.get("resource") or {}
        if not isinstance(resource_data, dict):
            raise ValueError("resource must be an object")
        resource = otlp_attributes(resource_data.get("attributes", []))
        service_name = resource.get("service.name")
        scope_groups = resource_group.get("scopeSpans", [])
        if not isinstance(scope_groups, list):
            raise ValueError("scopeSpans must be an array")
        for scope_group in scope_groups:
            if not isinstance(scope_group, dict):
                raise ValueError("Each scopeSpans item must be an object")
            for span in scope_group.get("spans", []):
                if not isinstance(span, dict):
                    raise ValueError("Each span must be an object")
                trace_id, span_id = span.get("traceId"), span.get("spanId")
                if not isinstance(trace_id, str) or not trace_id or not isinstance(span_id, str) or not span_id:
                    raise ValueError("Every span must include traceId and spanId")
                try:
                    start_ns = int(span["startTimeUnixNano"])
                    end_ns = int(span["endTimeUnixNano"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("Every span must include numeric startTimeUnixNano and endTimeUnixNano") from exc
                attributes = otlp_attributes(span.get("attributes", []))
                status = span.get("status") or {}
                if not isinstance(status, dict):
                    raise ValueError("span status must be an object")
                span_events = span.get("events", [])
                if not isinstance(span_events, list):
                    raise ValueError("span events must be an array")
                result.append({
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "parent_span_id": span.get("parentSpanId") or None,
                    "name": str(span.get("name") or "unnamed span"),
                    "service_name": service_name,
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "kind": int(span.get("kind", 0) or 0),
                    "status_code": int(status.get("code", 0) or 0),
                    "status_message": status.get("message"),
                    "attributes": attributes,
                    "events": span_events,
                })
    if not result:
        raise ValueError("The OTLP request contains no spans")
    return result


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
        if parsed.path == "/v1/traces":
            params = parse_qs(parsed.query)
            trace_id = params.get("trace_id", [None])[0]
            try:
                limit = min(max(int(params.get("limit", ["100"])[0]), 1), 1000)
            except ValueError:
                self.send_json(400, {"error": "limit must be an integer"})
                return
            with connect() as db:
                if trace_id:
                    rows = db.execute(
                        "SELECT * FROM spans WHERE trace_id = ? ORDER BY start_ns, span_id", (trace_id,)
                    ).fetchall()
                    spans = [self.span_json(row) for row in rows]
                    self.send_json(200, {"trace_id": trace_id, "spans": spans, "count": len(spans)})
                    return
                rows = db.execute(
                    """SELECT trace_id, MIN(start_ns) AS start_ns,
                              MAX(end_ns) AS end_ns, COUNT(*) AS span_count,
                              SUM(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) AS error_count,
                              MAX(CASE WHEN parent_span_id IS NULL THEN name END) AS root_name,
                              MAX(service_name) AS service_name
                       FROM spans GROUP BY trace_id ORDER BY start_ns DESC LIMIT ?""", (limit,)
                ).fetchall()
            traces = []
            for row in rows:
                start_ns, end_ns = row["start_ns"], row["end_ns"]
                traces.append({
                    "trace_id": row["trace_id"], "start_ns": start_ns, "end_ns": end_ns,
                    "start_time": datetime.fromtimestamp(start_ns / 1e9, timezone.utc).isoformat(),
                    "duration_ms": max(0, end_ns - start_ns) / 1e6,
                    "span_count": row["span_count"], "error_count": row["error_count"],
                    "root_name": row["root_name"] or "trace", "service_name": row["service_name"],
                })
            self.send_json(200, {"traces": traces, "count": len(traces)})
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

    @staticmethod
    def span_json(row: sqlite3.Row) -> dict:
        span = dict(row)
        span["attributes"] = json.loads(span["attributes"])
        span["events"] = json.loads(span["events"])
        span["duration_ms"] = max(0, span["end_ns"] - span["start_ns"]) / 1e6
        span["start_time"] = datetime.fromtimestamp(span["start_ns"] / 1e9, timezone.utc).isoformat()
        return span

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/v1/traces":
            try:
                spans = flatten_otlp_spans(self.read_json())
                with connect() as db:
                    db.executemany(
                        """INSERT INTO spans
                           (trace_id, span_id, parent_span_id, name, service_name, start_ns, end_ns,
                            kind, status_code, status_message, attributes, events)
                           VALUES (:trace_id, :span_id, :parent_span_id, :name, :service_name,
                                   :start_ns, :end_ns, :kind, :status_code, :status_message,
                                   :attributes, :events)
                           ON CONFLICT(trace_id, span_id) DO UPDATE SET
                             parent_span_id=excluded.parent_span_id, name=excluded.name,
                             service_name=excluded.service_name, start_ns=excluded.start_ns,
                             end_ns=excluded.end_ns, kind=excluded.kind, status_code=excluded.status_code,
                             status_message=excluded.status_message, attributes=excluded.attributes,
                             events=excluded.events""",
                        [{**span, "attributes": json.dumps(span["attributes"], ensure_ascii=False),
                          "events": json.dumps(span["events"], ensure_ascii=False)} for span in spans],
                    )
                self.send_json(200, {"partialSuccess": {}})
            except (ValueError, TypeError, KeyError) as exc:
                self.send_json(400, {"error": "invalid_otlp_trace_request", "message": str(exc)})
            return
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
