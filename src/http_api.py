from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__, "message": str(exc)})

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/items":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"items": service.list_items(role)})
                elif path == "/api/batches":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"batches": service.list_batches(role)})
                elif path.startswith("/api/batches/") and path.endswith("/recomputations"):
                    batch_id = int(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, {
                        "recomputations": service.batch_detail(batch_id, role)["recomputations"]
                    })
                elif path.startswith("/api/batches/") and path.endswith("/measure-reviews"):
                    batch_id = int(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, {
                        "measure_reviews": service.batch_detail(batch_id, role)["measure_reviews"]
                    })
                elif path.startswith("/api/batches/"):
                    batch_id = int(path.rsplit("/", 1)[-1])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.batch_detail(batch_id, role))
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"records": service.list_records(item_id, role)})
                elif path.startswith("/api/items/"):
                    item_id = int(path.rsplit("/", 1)[-1])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.get_item(item_id, role))
                elif path == "/api/audit":
                    actor, role = self._identity()
                    query = parse_qs(urlparse(self.path).query)
                    entity_id = int(query["entity_id"][0]) if "entity_id" in query else None
                    entity_type = query.get("entity_type", [None])[0]
                    del actor
                    self._json(200, {"events": service.audit(role, entity_id, entity_type)})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                elif path == "/api/batches":
                    self._json(201, service.create_batch(body, actor, role))
                elif path.startswith("/api/batches/") and path.endswith("/members"):
                    batch_id = int(path.split("/")[3])
                    result = service.merge_item(batch_id, body, actor, role)
                    # 并入成功(含审计失败可恢复)201；重复/恢复200，携带当前批次版本
                    status = 201 if result.get("accepted") else 200
                    self._json(status, result)
                elif path.startswith("/api/batches/") and path.endswith("/recover"):
                    batch_id = int(path.split("/")[3])
                    self._json(200, service.recover_merge(batch_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    self._json(201, service.add_record(item_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/transition"):
                    item_id = int(path.split("/")[3])
                    target = body.get("target")
                    expected = body.get("expected_version")
                    self._json(200, service.transition(
                        item_id, target, expected, actor, role))
                elif "/records/" in path and path.endswith("/verify"):
                    parts = path.split("/")
                    item_id, record_id = int(parts[3]), int(parts[5])
                    self._json(200, service.verify_measure(
                        item_id, record_id, body, actor, role))
                elif "/records/" in path and path.endswith("/reopen"):
                    parts = path.split("/")
                    item_id, record_id = int(parts[3]), int(parts[5])
                    self._json(200, service.reopen_measure(
                        item_id, record_id, body, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
