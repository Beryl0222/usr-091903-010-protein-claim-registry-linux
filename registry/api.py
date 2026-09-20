"""HTTP 适配层。

路由分两类：
- 内部接口（/commands、/claims、/papers、/batches、/events…）：面向登记员、
  同行评议者与项目负责人，返回当前投影与完整追溯信息；
- 公开接口（/public/…）：只读，只释放“已批准”论文在提交时冻结的快照，
  并对未公开靶点主张脱敏。

所有写操作统一走 POST /commands：
    {"command": "raise_claim", "actor": "zhang", "args": {...}}
命令名即 Registry 方法名，args 为其关键字参数，不另造一套 DTO。
"""

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .registry import Registry
from .store import ConflictError, NotFoundError, ValidationError

COMMANDS = frozenset({
    "register_protein", "link_ortholog", "register_computation_batch",
    "record_structure_prediction", "create_experiment", "register_sample",
    "register_control", "record_observation", "record_statistical_analysis",
    "raise_claim", "add_evidence", "add_claim_dependency",
    "withdraw_sample", "withdraw_analysis", "resolve_review",
    "create_paper", "add_paper_citation", "submit_paper", "decide_release",
})


def make_handler(registry: Registry, health_payload):
    class RegistryHandler(BaseHTTPRequestHandler):
        server_version = "ProteinClaimRegistry/1.0"

        # --- 基础设施 ---------------------------------------------------------

        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValidationError(f"请求体不是合法 JSON：{exc}")
            if not isinstance(data, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return data

        def log_message(self, *_args):
            return

        # --- 路由 -------------------------------------------------------------

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            try:
                if path == "/health":
                    self._send(200, health_payload())
                elif path == "/claims":
                    self._send(200, registry.list_claims(
                        protein_id=_one(query, "protein_id"),
                        status=_one(query, "status"),
                        claim_type=_one(query, "claim_type"),
                    ))
                elif path.startswith("/claims/") and path.endswith("/trace"):
                    claim_id = path[len("/claims/"):-len("/trace")]
                    self._send(200, registry.claim_trace(claim_id))
                elif path.startswith("/claims/"):
                    self._send(200, registry.get_claim(path[len("/claims/"):]))
                elif path == "/proteins":
                    self._send(200, sorted(registry.proteins))
                elif path.startswith("/proteins/"):
                    protein_id = path[len("/proteins/"):]
                    protein = dict(registry._require_entity(protein_id, "protein"))
                    protein["claims"] = registry.list_claims(protein_id=protein_id)
                    self._send(200, protein)
                elif path == "/experiments":
                    self._send(200, [self._experiment_view(eid) for eid in sorted(registry.experiments)])
                elif path.startswith("/experiments/"):
                    eid = path[len("/experiments/"):]
                    registry._require_entity(eid, "experiment")
                    self._send(200, self._experiment_view(eid))
                elif path == "/batches":
                    self._send(200, [dict(b) for b in sorted(registry.batches.values(),
                                                              key=lambda b: b["id"])])
                elif path.startswith("/batches/") and path.endswith("/impact"):
                    batch_id = path[len("/batches/"):-len("/impact")]
                    self._send(200, registry.batch_impact(batch_id))
                elif path == "/batches/compare":
                    self._send(200, registry.compare_batches(
                        _require_param(query, "a"), _require_param(query, "b")))
                elif path == "/papers":
                    self._send(200, registry.list_papers())
                elif path.startswith("/papers/"):
                    self._send(200, registry.get_paper(path[len("/papers/"):]))
                elif path == "/events":
                    # 内部审计：只追加日志，任何人都不能改写
                    self._send(200, [
                        {"seq": e["seq"], "ts": e["ts"], "type": e["type"],
                         "actor": e.get("actor"), "payload": e["payload"]}
                        for e in registry.store.events
                    ])
                elif path == "/public/papers":
                    self._send(200, registry.public_papers())
                elif path.startswith("/public/papers/"):
                    self._send(200, registry.public_paper(path[len("/public/papers/"):]))
                else:
                    self._send(404, {"error": "not_found", "path": path})
            except ValidationError as exc:
                self._send(400, {"error": "validation_error", "detail": str(exc)})
            except NotFoundError as exc:
                self._send(404, {"error": "not_found", "detail": str(exc)})
            except ConflictError as exc:
                self._send(409, {"error": "conflict", "detail": str(exc)})

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            try:
                if path != "/commands":
                    self._send(404, {"error": "not_found", "path": path})
                    return
                data = self._read_json()
                command = data.get("command")
                if command not in COMMANDS:
                    raise ValidationError(f"未知命令：{command}，允许：{sorted(COMMANDS)}")
                args = data.get("args") or {}
                if not isinstance(args, dict):
                    raise ValidationError("args 必须是 JSON 对象")
                actor = data.get("actor") or self.headers.get("X-Actor")
                result = getattr(registry, command)(**args, actor=actor)
                if isinstance(result, dict) and "flag_events" in result:
                    # 撤回命令：返回级联结果（标志事件同样已落盘）
                    body = {
                        "accepted": True,
                        "affected_claims": result["affected_claims"],
                        "flagged": len(result["flag_events"]),
                        "seq": registry.store.seq(),
                    }
                else:
                    body = {"accepted": True, "event": result,
                            "seq": registry.store.seq()}
                self._send(201, body)
            except TypeError as exc:
                # 命令参数名错误 / 缺少必填参数
                self._send(400, {"error": "validation_error", "detail": str(exc)})
            except ValidationError as exc:
                self._send(400, {"error": "validation_error", "detail": str(exc)})
            except NotFoundError as exc:
                self._send(404, {"error": "not_found", "detail": str(exc)})
            except ConflictError as exc:
                self._send(409, {"error": "conflict", "detail": str(exc)})

        # --- 视图辅助 ---------------------------------------------------------

        def _experiment_view(self, experiment_id):
            exp = registry.experiments[experiment_id]
            return {
                "id": exp["id"],
                "title": exp["title"],
                "species": exp["species"],
                "assay": exp["assay"],
                "materials": exp["materials"],
                "samples": list(exp["samples"].values()),
                "controls": list(exp["controls"].values()),
                "observations": list(exp["observations"].values()),
                "analyses": list(exp["analyses"].values()),
            }

    return RegistryHandler


def _one(query, key):
    values = query.get(key)
    return values[0] if values else None


def _require_param(query, key):
    value = _one(query, key)
    if not value:
        raise ValidationError(f"缺少查询参数：{key}")
    return value
