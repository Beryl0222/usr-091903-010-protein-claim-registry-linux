"""HTTP API。

角色由请求头 X-Role 携带（演示级边界；生产部署应替换为认证中间件）：
- researcher：录入蛋白、批次、实验、证据、主张
- curator：撤回、复核处置
- reviewer：只读，沿结论查看证据追踪
- pi：审批、跨批次影响比较
- public：只能访问 /api/public/**

路径约定：/api/ 为内部接口；/api/public/ 只释放已批准且脱敏的冻结版本。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .domain import DomainError, Registry

INTERNAL_ROLES = {"researcher", "curator", "reviewer", "pi"}
WRITE_ROLES = {"researcher", "curator"}
RETRACT_ROLES = {"curator", "pi"}
APPROVE_ROLES = {"pi", "curator"}


def create_handler(registry: Registry, health=None):
    health = health or (lambda: {"status": "ok"})

    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "ClaimRegistry/1.0"

        # ---- 基础收发 ---------------------------------------------------

        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise DomainError(f"请求体不是合法 JSON: {exc}", 400)
            if not isinstance(data, dict):
                raise DomainError("请求体必须是 JSON 对象", 400)
            return data

        def _actor(self):
            return self.headers.get("X-Actor") or "anonymous"

        def _require_role(self, allowed):
            role = self.headers.get("X-Role", "public")
            if role not in allowed:
                raise DomainError(
                    f"当前角色 {role} 无权执行该操作（需要 {sorted(allowed)}）", 403
                )
            return role

        def log_message(self, *_args):
            return

        # ---- 路由 -------------------------------------------------------

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            try:
                self._route(method, path, query)
            except DomainError as exc:
                self._send(exc.status, {"error": str(exc)})
            except Exception as exc:  # 防御：任何未预期错误不外泄堆栈
                self._send(500, {"error": f"内部错误: {type(exc).__name__}"})
                raise

        def _route(self, method, path, query):
            r = registry

            if path == "/health":
                self._send(200, health())
                return

            # ---- 公开接口（无需内部角色）--------------------------------
            m = re.fullmatch(r"/api/public/snapshots/([^/]+)", path)
            if method == "GET" and m:
                self._send(200, r.public_snapshot(m.group(1)))
                return

            # ---- 内部接口：仅对 /api/ 前缀强制内部角色 -------------------
            # 非 /api/ 的未知路径直接 404，不暴露内部路由是否存在。
            if path.startswith("/api/"):
                self._require_role(INTERNAL_ROLES)
            actor = self._actor()
            body = self._read_json() if method == "POST" else {}

            # 蛋白与别名
            if method == "POST" and path == "/api/proteins":
                self._require_role(WRITE_ROLES)
                pid = r.register_protein(
                    body["canonical_name"], body["species"],
                    aliases=body.get("aliases", []), actor=actor)
                return self._send(201, {"id": pid, **r.get_protein(pid)})

            m = re.fullmatch(r"/api/proteins/([^/]+)", path)
            if method == "GET" and m:
                return self._send(200, r.get_protein(m.group(1)))

            if method == "POST" and path == "/api/aliases":
                self._require_role(WRITE_ROLES)
                aid = r.add_alias(
                    body["alias"], protein_id=body.get("protein_id"),
                    canonical_name=body.get("canonical_name"),
                    species_scope=body.get("species_scope"),
                    note=body.get("note"), actor=actor)
                return self._send(201, {"id": aid})

            # 计算批次与预测
            if method == "POST" and path == "/api/batches":
                self._require_role(WRITE_ROLES)
                bid = r.create_prediction_batch(
                    body["model"], body["model_version"],
                    body["structure_db"], body["structure_db_version"],
                    run_note=body.get("run_note"), actor=actor)
                return self._send(201, {"id": bid})

            if method == "POST" and path == "/api/predictions":
                self._require_role(WRITE_ROLES)
                prid = r.add_structure_prediction(
                    body["protein"], body["batch_id"],
                    confidence=body.get("confidence"),
                    metadata=body.get("metadata"), actor=actor)
                return self._send(201, {"id": prid})

            # 实验链条
            if method == "POST" and path == "/api/materials":
                self._require_role(WRITE_ROLES)
                mid = r.register_material(
                    body["kind"], body["name"], species=body.get("species"),
                    source=body.get("source"), metadata=body.get("metadata"), actor=actor)
                return self._send(201, {"id": mid})

            if method == "POST" and path == "/api/experiments":
                self._require_role(WRITE_ROLES)
                eid = r.create_experiment(body["label"], body["title"], actor=actor)
                return self._send(201, {"id": eid})

            m = re.fullmatch(r"/api/experiments/([^/]+)/materials", path)
            if method == "POST" and m:
                self._require_role(WRITE_ROLES)
                r.attach_material(m.group(1), body["material_id"], body["role"], actor=actor)
                return self._send(200, {"ok": True})

            if method == "POST" and path == "/api/observations":
                self._require_role(WRITE_ROLES)
                oid = r.record_observation(
                    body["experiment_id"], body["label"], body["description"],
                    observed_at=body.get("observed_at"), actor=actor)
                return self._send(201, {"id": oid})

            if method == "POST" and path == "/api/analyses":
                self._require_role(WRITE_ROLES)
                aid = r.record_analysis(
                    body["experiment_id"], body["method"], body["summary"],
                    stats=body.get("stats"), actor=actor)
                return self._send(201, {"id": aid})

            # 主张
            if method == "POST" and path == "/api/claims":
                self._require_role(WRITE_ROLES)
                cid = r.create_claim(
                    body["claim_type"], body["subject"], body["predicate"],
                    body["statement"], body["species_scope"], body["granularity"],
                    object_ref=body.get("object"),
                    sensitive=bool(body.get("sensitive", False)),
                    supersedes=body.get("supersedes"), actor=actor)
                return self._send(201, {"id": cid, **r.claim_status(cid)})

            if method == "GET" and path == "/api/claims":
                return self._send(200, r.list_claims(
                    status=_one(query, "status"),
                    claim_type=_one(query, "claim_type"),
                    protein_ref=_one(query, "protein")))

            m = re.fullmatch(r"/api/claims/([^/]+)", path)
            if method == "GET" and m:
                return self._send(200, r.claim_trace(m.group(1)))

            m = re.fullmatch(r"/api/claims/([^/]+)/evidence", path)
            if method == "POST" and m:
                self._require_role(WRITE_ROLES)
                eid = r.attach_evidence(
                    m.group(1), body["kind"], body["weight"], actor=actor,
                    prediction_id=body.get("prediction_id"),
                    experiment_id=body.get("experiment_id"),
                    observation_id=body.get("observation_id"),
                    analysis_id=body.get("analysis_id"),
                    source_claim_id=body.get("source_claim_id"),
                    inference_basis=body.get("inference_basis"),
                    species_from=body.get("species_from"),
                    species_to=body.get("species_to"),
                    note=body.get("note"),
                    assess_status=body.get("assess_status"))
                return self._send(201, {"id": eid, **r.claim_status(m.group(1))})

            m = re.fullmatch(r"/api/claims/([^/]+)/assess", path)
            if method == "POST" and m:
                self._require_role(WRITE_ROLES)
                r.assess_claim(m.group(1), body["status"], body.get("reason", ""),
                               actor=actor)
                return self._send(200, r.claim_status(m.group(1)))

            # 撤回
            if method == "POST" and path == "/api/retractions":
                self._require_role(RETRACT_ROLES)
                result = r.retract(
                    body["reason"], actor=actor,
                    material_id=body.get("material_id"),
                    experiment_id=body.get("experiment_id"),
                    observation_id=body.get("observation_id"),
                    analysis_id=body.get("analysis_id"))
                return self._send(201, result)

            # 论文快照
            if method == "POST" and path == "/api/papers":
                self._require_role(WRITE_ROLES)
                pid = r.create_paper(body["title"], body["authors"], actor=actor)
                return self._send(201, {"id": pid})

            m = re.fullmatch(r"/api/papers/([^/]+)/snapshots", path)
            if method == "POST" and m:
                self._require_role(WRITE_ROLES)
                sid = r.submit_snapshot(
                    m.group(1), body["content"],
                    citations=body.get("citations"), actor=actor)
                return self._send(201, {"id": sid})

            m = re.fullmatch(r"/api/snapshots/([^/]+)", path)
            if method == "GET" and m:
                return self._send(200, r.get_snapshot(m.group(1)))

            m = re.fullmatch(r"/api/snapshots/([^/]+)/decision", path)
            if method == "POST" and m:
                self._require_role(APPROVE_ROLES)
                result = r.decide_snapshot(
                    m.group(1), body["scope"], body["decision"],
                    actor=actor, note=body.get("note"))
                return self._send(201, result)

            # PI：批次影响比较
            if method == "GET" and path == "/api/batches/compare":
                self._require_role({"pi"})
                return self._send(200, r.compare_batches(
                    _need(query, "a"), _need(query, "b")))

            # 审计链巡检
            if method == "GET" and path == "/api/audit":
                self._require_role({"curator", "pi"})
                return self._send(200, {
                    "chain": registry.store.verify_audit_chain(),
                    "events": r.audit_tail(limit=int(_one(query, "limit") or 50)),
                })

            self._send(404, {"error": f"未知路由: {method} {path}"})

    return ApiHandler


def _one(query, key):
    values = query.get(key)
    return values[0] if values else None


def _need(query, key):
    value = _one(query, key)
    if not value:
        raise DomainError(f"缺少查询参数: {key}")
    return value
