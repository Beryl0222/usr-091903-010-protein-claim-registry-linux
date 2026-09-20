"""领域层。

核心约束：
- 主张（claim）带显式类型与粒度；置信状态四值 + 待复核，状态迁移留痕。
- 证据链区分 experimental / computational / inferential；跨物种推断必须
  记录 source claim 与 species_from -> species_to，评审视图由此划出
  「直接证据」与「跨物种推断」边界。
- 样本/实验/观察/分析被撤回时不删除任何数据；沿证据链与推断链传播，
  所有受影响结论进入 needs_review。
- 论文快照内容在提交时冻结（含摘要），后续状态变化不改写快照与引文。
- 公开发布在批准时生成脱敏投影（敏感靶点、当时证据不足的引文被遮蔽），
  投影同样冻结。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .store import Store, canonical_json, new_id

# ---- 枚举 ----------------------------------------------------------------

CLAIM_TYPES = {
    "structural_similarity",   # 结构相似
    "regulatory_involvement",  # 参与调节
    "disease_target",          # 疾病靶点
    "functional",
    "localization",
    "identity",
}

GRANULARITIES = {
    "molecular_structure",
    "molecular_function",
    "cellular",
    "organism",
    "cross_species",
}

# 支持 / 反驳 / 尚未验证 / 仅可外推 / 待复核
STATUS_SUPPORTED = "supported"
STATUS_CONTRADICTED = "contradicted"
STATUS_UNVERIFIED = "unverified"
STATUS_EXTRAPOLABLE = "extrapolable"
STATUS_NEEDS_REVIEW = "needs_review"
STATUSES = {
    STATUS_SUPPORTED,
    STATUS_CONTRADICTED,
    STATUS_UNVERIFIED,
    STATUS_EXTRAPOLABLE,
    STATUS_NEEDS_REVIEW,
}

DIRECT_EVIDENCE_KINDS = {"experimental", "computational"}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _loads(value: str | None, default):
    if not value:
        return default
    return json.loads(value)


@dataclass
class Registry:
    store: Store

    # ---- 蛋白与别名 ----------------------------------------------------

    def register_protein(self, canonical_name, species, aliases=None, actor=None):
        aliases = aliases or []
        pid = new_id("prot")

        def tx(c, seq):
            c.execute(
                "INSERT INTO proteins (id, canonical_name, species, created_at)"
                " VALUES (?,?,?,?)",
                (pid, canonical_name, species, _now()),
            )
            for alias in aliases:
                c.execute(
                    "INSERT INTO protein_aliases (id, protein_id, alias, species_scope, note, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (new_id("als"), pid, alias, species, None, _now()),
                )
            return pid

        return self.store.write(
            "register_protein",
            {"id": pid, "canonical_name": canonical_name, "species": species, "aliases": aliases},
            actor,
            tx,
        )

    def add_alias(self, alias, protein_id=None, canonical_name=None, species_scope=None,
                  note=None, actor=None):
        protein_id = protein_id or self._resolve_protein_id(canonical_name)
        aid = new_id("als")

        def tx(c, seq):
            c.execute(
                "INSERT INTO protein_aliases (id, protein_id, alias, species_scope, note, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (aid, protein_id, alias, species_scope, note, _now()),
            )
            return aid

        return self.store.write(
            "add_alias",
            {"id": aid, "protein_id": protein_id, "alias": alias},
            actor,
            tx,
        )

    def _resolve_protein_id(self, name):
        if not name:
            raise DomainError("缺少蛋白标识（id 或名称/别名）", 400)
        row = self.store.query_one("SELECT id FROM proteins WHERE id = ?", (name,))
        if row:
            return row["id"]
        row = self.store.query_one("SELECT protein_id FROM protein_aliases WHERE alias = ?", (name,))
        if row:
            return row["protein_id"]
        row = self.store.query_one("SELECT id FROM proteins WHERE canonical_name = ?", (name,))
        if row:
            return row["id"]
        raise DomainError(f"未识别的蛋白: {name}", 404)

    def get_protein(self, ref):
        pid = self._resolve_protein_id(ref)
        row = self.store.query_one("SELECT * FROM proteins WHERE id = ?", (pid,))
        aliases = [
            r["alias"]
            for r in self.store.query(
                "SELECT alias FROM protein_aliases WHERE protein_id = ? ORDER BY created_at",
                (pid,),
            )
        ]
        return {
            "id": row["id"],
            "canonical_name": row["canonical_name"],
            "species": row["species"],
            "aliases": aliases,
        }

    # ---- 计算批次与结构预测 --------------------------------------------

    def create_prediction_batch(self, model, model_version, structure_db,
                                structure_db_version, run_note=None, actor=None):
        bid = new_id("batch")

        def tx(c, seq):
            c.execute(
                "INSERT INTO prediction_batches"
                " (id, model, model_version, structure_db, structure_db_version, run_note, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (bid, model, model_version, structure_db, structure_db_version, run_note, _now()),
            )
            return bid

        return self.store.write(
            "create_prediction_batch",
            {"id": bid, "model": model, "model_version": model_version,
             "structure_db": structure_db, "structure_db_version": structure_db_version},
            actor,
            tx,
        )

    def add_structure_prediction(self, protein_ref, batch_id, confidence=None,
                                 metadata=None, actor=None):
        pid = self._resolve_protein_id(protein_ref)
        self._require_batch(batch_id)
        prid = new_id("pred")

        def tx(c, seq):
            c.execute(
                "INSERT INTO structure_predictions"
                " (id, protein_id, batch_id, confidence, metadata_json, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (prid, pid, batch_id, confidence, canonical_json(metadata or {}), _now()),
            )
            return prid

        return self.store.write(
            "add_structure_prediction",
            {"id": prid, "protein_id": pid, "batch_id": batch_id, "confidence": confidence},
            actor,
            tx,
        )

    def _require_batch(self, batch_id):
        if not self.store.query_one("SELECT id FROM prediction_batches WHERE id = ?", (batch_id,)):
            raise DomainError(f"计算批次不存在: {batch_id}", 404)

    # ---- 实验材料 / 实验 / 观察 / 统计分析 ------------------------------

    def register_material(self, kind, name, species=None, source=None, metadata=None, actor=None):
        mid = new_id("mat")

        def tx(c, seq):
            c.execute(
                "INSERT INTO materials (id, kind, name, species, source, metadata_json, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (mid, kind, name, species, source, canonical_json(metadata or {}), _now()),
            )
            return mid

        return self.store.write(
            "register_material",
            {"id": mid, "kind": kind, "name": name, "species": species},
            actor,
            tx,
        )

    def create_experiment(self, label, title, actor=None):
        eid = new_id("exp")

        def tx(c, seq):
            c.execute(
                "INSERT INTO experiments (id, label, title, created_at) VALUES (?,?,?,?)",
                (eid, label, title, _now()),
            )
            return eid

        return self.store.write(
            "create_experiment", {"id": eid, "label": label, "title": title}, actor, tx
        )

    def attach_material(self, experiment_id, material_id, role, actor=None):
        if role not in {"sample", "control", "reagent"}:
            raise DomainError(f"材料角色非法: {role}")
        if not self.store.query_one("SELECT id FROM experiments WHERE id = ?", (experiment_id,)):
            raise DomainError(f"实验不存在: {experiment_id}", 404)
        if not self.store.query_one("SELECT id FROM materials WHERE id = ?", (material_id,)):
            raise DomainError(f"材料不存在: {material_id}", 404)

        def tx(c, seq):
            c.execute(
                "INSERT INTO experiment_materials (experiment_id, material_id, role)"
                " VALUES (?,?,?)",
                (experiment_id, material_id, role),
            )

        self.store.write(
            "attach_material",
            {"experiment_id": experiment_id, "material_id": material_id, "role": role},
            actor,
            tx,
        )

    def record_observation(self, experiment_id, label, description, observed_at=None, actor=None):
        if not self.store.query_one("SELECT id FROM experiments WHERE id = ?", (experiment_id,)):
            raise DomainError(f"实验不存在: {experiment_id}", 404)
        oid = new_id("obs")

        def tx(c, seq):
            c.execute(
                "INSERT INTO observations (id, experiment_id, label, description, observed_at)"
                " VALUES (?,?,?,?,?)",
                (oid, experiment_id, label, description, observed_at or _now()),
            )
            return oid

        return self.store.write(
            "record_observation",
            {"id": oid, "experiment_id": experiment_id, "label": label},
            actor,
            tx,
        )

    def record_analysis(self, experiment_id, method, summary, stats=None, actor=None):
        if not self.store.query_one("SELECT id FROM experiments WHERE id = ?", (experiment_id,)):
            raise DomainError(f"实验不存在: {experiment_id}", 404)
        aid = new_id("anl")

        def tx(c, seq):
            c.execute(
                "INSERT INTO analyses (id, experiment_id, method, summary, stats_json, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (aid, experiment_id, method, summary, canonical_json(stats or {}), _now()),
            )
            return aid

        return self.store.write(
            "record_analysis",
            {"id": aid, "experiment_id": experiment_id, "method": method},
            actor,
            tx,
        )

    # ---- 主张 ----------------------------------------------------------

    def create_claim(self, claim_type, subject_ref, predicate, statement, species_scope,
                     granularity, object_ref=None, sensitive=False, supersedes=None, actor=None):
        if claim_type not in CLAIM_TYPES:
            raise DomainError(f"主张类型非法: {claim_type}；可选 {sorted(CLAIM_TYPES)}")
        if granularity not in GRANULARITIES:
            raise DomainError(f"粒度非法: {granularity}；可选 {sorted(GRANULARITIES)}")
        subject = self._resolve_protein_id(subject_ref)
        obj = self._resolve_protein_id(object_ref) if object_ref else None
        if supersedes and not self.store.query_one(
            "SELECT id FROM claims WHERE id = ?", (supersedes,)
        ):
            raise DomainError(f"被取代主张不存在: {supersedes}", 404)
        cid = new_id("clm")
        ts = _now()

        def tx(c, seq):
            c.execute(
                "INSERT INTO claims (id, claim_type, subject_protein_id, object_protein_id,"
                " predicate, statement, species_scope, granularity, sensitive, supersedes_claim_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cid, claim_type, subject, obj, predicate, statement, species_scope,
                 granularity, 1 if sensitive else 0, supersedes, ts),
            )
            c.execute(
                "INSERT INTO claim_status (claim_id, status, reason, changed_at)"
                " VALUES (?,?,?,?)",
                (cid, STATUS_UNVERIFIED, "created", ts),
            )
            c.execute(
                "INSERT INTO status_history (claim_id, status, reason, actor, event_seq, changed_at)"
                " VALUES (?,?,?,?,?,?)",
                (cid, STATUS_UNVERIFIED, "created", actor, seq, ts),
            )
            return cid

        return self.store.write(
            "create_claim",
            {"id": cid, "claim_type": claim_type, "statement": statement,
             "species_scope": species_scope, "granularity": granularity, "sensitive": sensitive},
            actor,
            tx,
        )

    def _claim_row(self, claim_id):
        row = self.store.query_one("SELECT * FROM claims WHERE id = ?", (claim_id,))
        if not row:
            raise DomainError(f"主张不存在: {claim_id}", 404)
        return row

    # ---- 证据链 --------------------------------------------------------

    def attach_evidence(self, claim_id, kind, weight, actor=None, prediction_id=None,
                        experiment_id=None, observation_id=None, analysis_id=None,
                        source_claim_id=None, inference_basis=None, species_from=None,
                        species_to=None, note=None, assess_status=None):
        """挂载证据链。

        assess_status 非空时，在同一事件（同一全序序号）内即时更新主张置信
        状态，使「证据到达」与「结论状态改变」对批次时点比较原子可见。
        """
        self._claim_row(claim_id)
        if kind not in {"experimental", "computational", "inferential"}:
            raise DomainError(f"证据类别非法: {kind}")
        if weight not in {"supporting", "contradicting"}:
            raise DomainError(f"证据方向非法: {weight}")
        if kind == "computational" and not prediction_id:
            raise DomainError("计算证据必须引用 structure_prediction")
        if kind == "experimental" and not experiment_id:
            raise DomainError("实验证据必须引用 experiment")
        if kind == "inferential":
            if not source_claim_id:
                raise DomainError("推断证据必须引用来源主张 source_claim_id")
            self._claim_row(source_claim_id)
            if not species_from or not species_to:
                raise DomainError("跨物种推断必须给出 species_from 与 species_to")
            if species_from == species_to:
                raise DomainError("species_from 与 species_to 相同，不构成跨物种推断")
        for ref, table in ((prediction_id, "structure_predictions"),
                           (experiment_id, "experiments"),
                           (observation_id, "observations"),
                           (analysis_id, "analyses")):
            if ref and not self.store.query_one(
                f"SELECT id FROM {table} WHERE id = ?", (ref,)
            ):
                raise DomainError(f"引用对象不存在于 {table}: {ref}", 404)

        if assess_status is not None:
            live = [l for l in self._untainted_links(claim_id) if not l["taint"]]
            live.append({"kind": kind, "weight": weight, "taint": None})
            self._validate_transition(claim_id, assess_status, live)

        eid = new_id("evd")
        reason = f"evidence:{eid}:{kind}:{weight}"

        def tx(c, seq):
            c.execute(
                "INSERT INTO evidence_links (id, claim_id, kind, weight, prediction_id,"
                " experiment_id, observation_id, analysis_id, source_claim_id, inference_basis,"
                " species_from, species_to, note, event_seq, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, claim_id, kind, weight, prediction_id, experiment_id, observation_id,
                 analysis_id, source_claim_id, inference_basis, species_from, species_to,
                 note, seq, _now()),
            )
            if assess_status is not None:
                c.execute(
                    "UPDATE claim_status SET status = ?, reason = ?, changed_at = ? WHERE claim_id = ?",
                    (assess_status, reason, _now(), claim_id),
                )
                c.execute(
                    "INSERT INTO status_history (claim_id, status, reason, actor, event_seq, changed_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (claim_id, assess_status, reason, actor, seq, _now()),
                )
            return eid

        return self.store.write(
            "attach_evidence",
            {"id": eid, "claim_id": claim_id, "kind": kind, "weight": weight,
             "prediction_id": prediction_id, "experiment_id": experiment_id,
             "observation_id": observation_id, "analysis_id": analysis_id,
             "source_claim_id": source_claim_id, "species_from": species_from,
             "species_to": species_to, "assess_status": assess_status},
            actor,
            tx,
        )

    # ---- 证据污染判定（撤回标记沿实验链向上查） --------------------------

    def _link_taint(self, link):
        """返回使该证据失效的撤回路径；None 表示证据仍有效。"""
        if link["kind"] == "experimental":
            exp = self.store.query_one(
                "SELECT id, retracted FROM experiments WHERE id = ?",
                (link["experiment_id"],),
            )
            if exp and exp["retracted"]:
                return [f"experiment:{exp['id']}"]
            for mat in self.store.query(
                "SELECT m.id, m.retracted, em.role FROM materials m"
                " JOIN experiment_materials em ON em.material_id = m.id"
                " WHERE em.experiment_id = ? AND m.retracted = 1",
                (link["experiment_id"],),
            ):
                return [f"experiment:{link['experiment_id']}", f"material({mat['role']}):{mat['id']}"]
        if link["observation_id"]:
            obs = self.store.query_one(
                "SELECT id, experiment_id, retracted FROM observations WHERE id = ?",
                (link["observation_id"],),
            )
            if obs and obs["retracted"]:
                return [f"observation:{obs['id']}"]
            if obs:
                exp = self.store.query_one(
                    "SELECT retracted FROM experiments WHERE id = ?", (obs["experiment_id"],)
                )
                if exp and exp["retracted"]:
                    return [f"observation:{obs['id']}", f"experiment:{obs['experiment_id']}"]
        if link["analysis_id"]:
            anl = self.store.query_one(
                "SELECT id, experiment_id, retracted FROM analyses WHERE id = ?",
                (link["analysis_id"],),
            )
            if anl and anl["retracted"]:
                return [f"analysis:{anl['id']}"]
            if anl:
                exp = self.store.query_one(
                    "SELECT retracted FROM experiments WHERE id = ?", (anl["experiment_id"],)
                )
                if exp and exp["retracted"]:
                    return [f"analysis:{anl['id']}", f"experiment:{anl['experiment_id']}"]
        return None

    def _untainted_links(self, claim_id):
        out = []
        for link in self.store.query(
            "SELECT * FROM evidence_links WHERE claim_id = ? ORDER BY created_at", (claim_id,)
        ):
            d = dict(link)
            d["taint"] = self._link_taint(link)
            out.append(d)
        return out

    # ---- 置信状态评估 --------------------------------------------------

    def _validate_transition(self, claim_id, status, live_links):
        """依据「事件生效后」的未撤回证据集合校验目标状态是否合法。"""
        if status not in STATUSES:
            raise DomainError(f"状态非法: {status}")
        direct_support = [l for l in live_links if l["weight"] == "supporting"
                          and l["kind"] in DIRECT_EVIDENCE_KINDS]
        contradict = [l for l in live_links if l["weight"] == "contradicting"]
        infer_support = [l for l in live_links if l["weight"] == "supporting"
                         and l["kind"] == "inferential"]

        if status == STATUS_SUPPORTED and not direct_support:
            raise DomainError("标记 supported 至少需要一条未被撤回的直接支持证据")
        if status == STATUS_CONTRADICTED and not contradict:
            raise DomainError("标记 contradicted 至少需要一条未被撤回的反驳证据")
        if status == STATUS_EXTRAPOLABLE:
            if not infer_support:
                raise DomainError("标记 extrapolable 至少需要一条跨物种推断支持")
            if direct_support:
                raise DomainError("存在未被撤回的直接支持证据时应标记 supported，而非 extrapolable")
        if status == STATUS_UNVERIFIED:
            current = self.store.query_one(
                "SELECT status FROM claim_status WHERE claim_id = ?", (claim_id,)
            )
            if current and current["status"] != STATUS_NEEDS_REVIEW:
                raise DomainError("仅可在待复核结束后将主张重置为 unverified")

    def assess_claim(self, claim_id, status, reason, actor=None):
        self._claim_row(claim_id)
        live = [l for l in self._untainted_links(claim_id) if not l["taint"]]
        self._validate_transition(claim_id, status, live)

        ts = _now()

        def tx(c, seq):
            c.execute(
                "UPDATE claim_status SET status = ?, reason = ?, changed_at = ? WHERE claim_id = ?",
                (status, reason, ts, claim_id),
            )
            c.execute(
                "INSERT INTO status_history (claim_id, status, reason, actor, event_seq, changed_at)"
                " VALUES (?,?,?,?,?,?)",
                (claim_id, status, reason, actor, seq, ts),
            )

        self.store.write(
            "assess_claim",
            {"claim_id": claim_id, "status": status, "reason": reason},
            actor,
            tx,
        )

    def claim_status(self, claim_id):
        self._claim_row(claim_id)
        row = self.store.query_one("SELECT * FROM claim_status WHERE claim_id = ?", (claim_id,))
        return dict(row)

    def list_claims(self, status=None, claim_type=None, protein_ref=None):
        sql = (
            "SELECT c.*, cs.status, cs.reason AS status_reason, cs.changed_at AS status_changed_at"
            " FROM claims c JOIN claim_status cs ON cs.claim_id = c.id WHERE 1=1"
        )
        params = []
        if status:
            if status not in STATUSES:
                raise DomainError(f"状态非法: {status}")
            sql += " AND cs.status = ?"
            params.append(status)
        if claim_type:
            sql += " AND c.claim_type = ?"
            params.append(claim_type)
        if protein_ref:
            pid = self._resolve_protein_id(protein_ref)
            sql += " AND (c.subject_protein_id = ? OR c.object_protein_id = ?)"
            params.extend([pid, pid])
        sql += " ORDER BY c.created_at"
        return [dict(r) for r in self.store.query(sql, params)]

    # ---- 撤回与依赖传播 ------------------------------------------------

    def retract(self, reason, actor=None, material_id=None, experiment_id=None,
                observation_id=None, analysis_id=None):
        targets = [k for k in (("material", material_id), ("experiment", experiment_id),
                               ("observation", observation_id), ("analysis", analysis_id))
                   if k[1]]
        if len(targets) != 1:
            raise DomainError("一次撤回必须且只能指定 material/experiment/observation/analysis 之一")
        kind, ref = targets[0]
        if not self.store.query_one(f"SELECT id FROM {kind}s WHERE id = ?", (ref,)):
            raise DomainError(f"撤回对象不存在: {kind}:{ref}", 404)

        rid = new_id("ret")
        ts = _now()

        # 直接受影响的证据链
        link_ids = set()
        if kind == "material":
            exp_rows = self.store.query(
                "SELECT experiment_id FROM experiment_materials WHERE material_id = ?", (ref,)
            )
            exp_ids = [r["experiment_id"] for r in exp_rows]
            if exp_ids:
                placeholders = ",".join("?" * len(exp_ids))
                for r in self.store.query(
                    f"SELECT id FROM evidence_links WHERE experiment_id IN ({placeholders})"
                    f" OR observation_id IN (SELECT id FROM observations WHERE experiment_id IN ({placeholders}))"
                    f" OR analysis_id IN (SELECT id FROM analyses WHERE experiment_id IN ({placeholders}))",
                    exp_ids * 3,
                ):
                    link_ids.add(r["id"])
            direct_route = [f"material:{ref}"]
        elif kind == "experiment":
            for r in self.store.query(
                "SELECT id FROM evidence_links WHERE experiment_id = ?"
                " OR observation_id IN (SELECT id FROM observations WHERE experiment_id = ?)"
                " OR analysis_id IN (SELECT id FROM analyses WHERE experiment_id = ?)",
                (ref, ref, ref),
            ):
                link_ids.add(r["id"])
            direct_route = [f"experiment:{ref}"]
        elif kind == "observation":
            for r in self.store.query(
                "SELECT id FROM evidence_links WHERE observation_id = ?", (ref,)
            ):
                link_ids.add(r["id"])
            direct_route = [f"observation:{ref}"]
        else:
            for r in self.store.query(
                "SELECT id FROM evidence_links WHERE analysis_id = ?", (ref,)
            ):
                link_ids.add(r["id"])
            direct_route = [f"analysis:{ref}"]

        # claim -> 最短依赖路径
        paths: dict[str, list] = {}
        for lid in link_ids:
            link = self.store.query_one("SELECT * FROM evidence_links WHERE id = ?", (lid,))
            paths.setdefault(
                link["claim_id"],
                direct_route + [f"evidence:{lid}", f"claim:{link['claim_id']}"],
            )

        # 沿推断链传播：source_claim 受影响 => 依赖它的结论也受影响
        while True:
            frontier = set(paths)
            added = False
            for link in self.store.query(
                "SELECT * FROM evidence_links WHERE kind = 'inferential'"
            ):
                src = link["source_claim_id"]
                dst = link["claim_id"]
                if src in frontier and dst not in paths:
                    paths[dst] = paths[src] + [
                        f"inference:{link['species_from']}->{link['species_to']}",
                        f"evidence:{link['id']}",
                        f"claim:{dst}",
                    ]
                    added = True
            if not added:
                break

        affected = sorted(paths)

        def tx(c, seq):
            c.execute(
                "INSERT INTO retractions (id, material_id, experiment_id, observation_id,"
                " analysis_id, reason, actor, retracted_at) VALUES (?,?,?,?,?,?,?,?)",
                (rid, material_id, experiment_id, observation_id, analysis_id,
                 reason, actor, ts),
            )
            c.execute(f"UPDATE {kind}s SET retracted = 1 WHERE id = ?", (ref,))
            for claim_id in affected:
                c.execute(
                    "INSERT INTO retraction_impacts (retraction_id, claim_id, dependency_path_json)"
                    " VALUES (?,?,?)",
                    (rid, claim_id, canonical_json(paths[claim_id])),
                )
                # 不删除结论；进入待复核（已在待复核的刷新原因并留痕）
                c.execute(
                    "UPDATE claim_status SET status = ?, reason = ?, changed_at = ? WHERE claim_id = ?",
                    (STATUS_NEEDS_REVIEW, f"retraction:{rid}", ts, claim_id),
                )
                c.execute(
                    "INSERT INTO status_history (claim_id, status, reason, actor, event_seq, changed_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (claim_id, STATUS_NEEDS_REVIEW, f"retraction:{rid}:{kind}:{ref}", actor, seq, ts),
                )
            return rid

        rid = self.store.write(
            "retract",
            {"id": rid, "target": f"{kind}:{ref}", "reason": reason,
             "affected_claims": affected},
            actor,
            tx,
        )
        return {"retraction_id": rid, "target": f"{kind}:{ref}",
                "affected_claims": affected, "paths": paths}

    # ---- 评审者证据追踪 --------------------------------------------------

    def _expand_link(self, link):
        d = dict(link)
        d["taint"] = self._link_taint(link)
        if link["prediction_id"]:
            pred = self.store.query_one(
                "SELECT sp.*, pb.model, pb.model_version, pb.structure_db, pb.structure_db_version,"
                " pb.id AS batch_id FROM structure_predictions sp"
                " JOIN prediction_batches pb ON pb.id = sp.batch_id"
                " WHERE sp.id = ?",
                (link["prediction_id"],),
            )
            d["prediction"] = {
                "id": pred["id"], "confidence": pred["confidence"],
                "metadata": _loads(pred["metadata_json"], {}),
                "batch": {"id": pred["batch_id"], "model": pred["model"],
                          "model_version": pred["model_version"],
                          "structure_db": pred["structure_db"],
                          "structure_db_version": pred["structure_db_version"]},
            }
        if link["experiment_id"]:
            exp = self.store.query_one(
                "SELECT * FROM experiments WHERE id = ?", (link["experiment_id"],)
            )
            materials = []
            for m in self.store.query(
                "SELECT m.id, m.kind, m.name, m.species, m.source, m.retracted, em.role"
                " FROM materials m JOIN experiment_materials em ON em.material_id = m.id"
                " WHERE em.experiment_id = ?",
                (link["experiment_id"],),
            ):
                materials.append(dict(m))
            d["experiment"] = {
                "id": exp["id"], "label": exp["label"], "title": exp["title"],
                "retracted": bool(exp["retracted"]), "materials": materials,
            }
        if link["observation_id"]:
            obs = self.store.query_one(
                "SELECT * FROM observations WHERE id = ?", (link["observation_id"],)
            )
            d["observation"] = dict(obs)
        if link["analysis_id"]:
            anl = self.store.query_one(
                "SELECT * FROM analyses WHERE id = ?", (link["analysis_id"],)
            )
            d["analysis"] = {**dict(anl), "stats": _loads(anl["stats_json"], {})}
        return d

    def claim_trace(self, claim_id, depth=0, seen=None):
        """沿一条结论返回直接证据与跨物种推断边界（推断来源递归展开）。"""
        seen = seen if seen is not None else set()
        claim = self._claim_row(claim_id)
        if claim_id in seen:
            return {"claim_id": claim_id, "statement": claim["statement"], "cycle": True}
        seen = seen | {claim_id}
        status = dict(self.store.query_one(
            "SELECT * FROM claim_status WHERE claim_id = ?", (claim_id,)
        ))
        history = [dict(r) for r in self.store.query(
            "SELECT status, reason, actor, changed_at FROM status_history"
            " WHERE claim_id = ? ORDER BY id", (claim_id,)
        )]
        links = [self._expand_link(r) for r in self.store.query(
            "SELECT * FROM evidence_links WHERE claim_id = ? ORDER BY created_at", (claim_id,)
        )]
        direct = [l for l in links if l["kind"] in DIRECT_EVIDENCE_KINDS]
        inferred = []
        for l in links:
            if l["kind"] != "inferential":
                continue
            src_id = l["source_claim_id"]
            src_row = self._claim_row(src_id)
            src_status = self.store.query_one(
                "SELECT status FROM claim_status WHERE claim_id = ?", (src_id,)
            )
            src_links = self._untainted_links(src_id)
            src_direct_kinds = sorted({
                x["kind"] for x in src_links
                if x["kind"] in DIRECT_EVIDENCE_KINDS and not x["taint"]
            })
            entry = {
                "link_id": l["id"], "weight": l["weight"],
                "inference_basis": l["inference_basis"],
                "species_from": l["species_from"], "species_to": l["species_to"],
                "source_claim": {
                    "id": src_id, "claim_type": src_row["claim_type"],
                    "statement": src_row["statement"],
                    "species_scope": src_row["species_scope"],
                    "current_status": src_status["status"],
                    "direct_evidence_kinds": src_direct_kinds,
                },
                "taint": l["taint"],
            }
            if depth < 3:
                entry["source_claim"]["trace"] = self.claim_trace(
                    src_id, depth + 1, seen
                )
            inferred.append(entry)

        impacts = []
        for r in self.store.query(
            "SELECT ri.retraction_id, ri.dependency_path_json, r.reason, r.retracted_at"
            " FROM retraction_impacts ri JOIN retractions r ON r.id = ri.retraction_id"
            " WHERE ri.claim_id = ? ORDER BY r.retracted_at",
            (claim_id,),
        ):
            impacts.append({"retraction_id": r["retraction_id"], "reason": r["reason"],
                            "retracted_at": r["retracted_at"],
                            "dependency_path": json.loads(r["dependency_path_json"])})

        subject = self.get_protein(claim["subject_protein_id"])
        return {
            "claim": {
                "id": claim["id"], "claim_type": claim["claim_type"],
                "granularity": claim["granularity"], "predicate": claim["predicate"],
                "statement": claim["statement"], "species_scope": claim["species_scope"],
                "sensitive": bool(claim["sensitive"]),
                "subject": {"id": subject["id"], "canonical_name": subject["canonical_name"],
                            "species": subject["species"], "aliases": subject["aliases"]},
            },
            "current_status": status,
            "status_history": history,
            "direct_evidence": direct,
            "cross_species_inferences": inferred,
            "inference_boundary": {
                "has_direct_evidence": len(direct) > 0,
                "direct_evidence_count": len(direct),
                "inference_count": len(inferred),
                "note": ("直接证据仅覆盖来源物种；本物种结论若只依赖推断链，状态至多为 extrapolable"
                         if inferred else "本主张不含跨物种推断"),
            },
            "retraction_impacts": impacts,
        }

    # ---- 论文快照（不可变） ----------------------------------------------

    def create_paper(self, title, authors, actor=None):
        pid = new_id("pap")

        def tx(c, seq):
            c.execute(
                "INSERT INTO papers (id, title, authors, created_at) VALUES (?,?,?,?)",
                (pid, title, authors, _now()),
            )
            return pid

        return self.store.write(
            "create_paper", {"id": pid, "title": title, "authors": authors}, actor, tx
        )

    def submit_snapshot(self, paper_id, content, citations=None, actor=None):
        paper = self.store.query_one("SELECT * FROM papers WHERE id = ?", (paper_id,))
        if not paper:
            raise DomainError(f"论文不存在: {paper_id}", 404)
        citations = citations or []
        for cit in citations:
            self._claim_row(cit["claim_id"])
        frozen_at = _now()
        sid = new_id("snp")
        digest = _digest_content(paper_id, frozen_at, content, citations)
        if self.store.query_one("SELECT id FROM paper_snapshots WHERE digest = ?", (digest,)):
            raise DomainError("完全相同的论文快照已存在（摘要重复）", 409)

        def tx(c, seq):
            c.execute(
                "INSERT INTO paper_snapshots (id, paper_id, frozen_at, content_json, digest)"
                " VALUES (?,?,?,?,?)",
                (sid, paper_id, frozen_at, canonical_json(content), digest),
            )
            for cit in citations:
                c.execute(
                    "INSERT INTO snapshot_citations (snapshot_id, claim_id, exact_quote)"
                    " VALUES (?,?,?)",
                    (sid, cit["claim_id"], cit["exact_quote"]),
                )
            return sid

        return self.store.write(
            "submit_snapshot",
            {"id": sid, "paper_id": paper_id, "digest": digest,
             "citation_claim_ids": [c["claim_id"] for c in citations]},
            actor,
            tx,
        )

    def get_snapshot(self, snapshot_id):
        row = self.store.query_one("SELECT * FROM paper_snapshots WHERE id = ?", (snapshot_id,))
        if not row:
            raise DomainError(f"快照不存在: {snapshot_id}", 404)
        paper = self.store.query_one("SELECT * FROM papers WHERE id = ?", (row["paper_id"],))
        citations = []
        for cit in self.store.query(
            "SELECT claim_id, exact_quote FROM snapshot_citations WHERE snapshot_id = ?",
            (snapshot_id,),
        ):
            claim = self._claim_row(cit["claim_id"])
            status = self.store.query_one(
                "SELECT status FROM claim_status WHERE claim_id = ?", (cit["claim_id"],)
            )
            citations.append({
                "claim_id": cit["claim_id"],
                "exact_quote": cit["exact_quote"],
                "claim_type": claim["claim_type"],
                "statement": claim["statement"],
                "species_scope": claim["species_scope"],
                "current_status": status["status"],  # 仅作并置展示，不回写快照
            })
        approvals = [dict(r) for r in self.store.query(
            "SELECT scope, decision, decision_note, actor, decided_at"
            " FROM snapshot_approvals WHERE snapshot_id = ? ORDER BY decided_at",
            (snapshot_id,),
        )]
        return {
            "id": row["id"], "paper_id": row["paper_id"],
            "paper_title": paper["title"], "authors": paper["authors"],
            "frozen_at": row["frozen_at"], "digest": row["digest"],
            "immutable": True,
            "content": json.loads(row["content_json"]),
            "citations": citations,
            "approvals": approvals,
        }

    # ---- 批准与公开脱敏 --------------------------------------------------

    def decide_snapshot(self, snapshot_id, scope, decision, actor=None, note=None):
        if scope not in {"internal", "public"}:
            raise DomainError("审批范围必须是 internal 或 public")
        if decision not in {"approved", "rejected"}:
            raise DomainError("审批结论必须是 approved 或 rejected")
        row = self.store.query_one("SELECT * FROM paper_snapshots WHERE id = ?", (snapshot_id,))
        if not row:
            raise DomainError(f"快照不存在: {snapshot_id}", 404)
        existing = self.store.query_one(
            "SELECT id FROM snapshot_approvals WHERE snapshot_id = ? AND scope = ?",
            (snapshot_id, scope),
        )
        if existing:
            raise DomainError(f"{scope} 审批结论已存在且不可更改", 409)
        if scope == "public" and decision == "approved":
            internal = self.store.query_one(
                "SELECT id FROM snapshot_approvals WHERE snapshot_id = ?"
                " AND scope = 'internal' AND decision = 'approved'",
                (snapshot_id,),
            )
            if not internal:
                raise DomainError("公开发布前须先通过内部批准")

        redacted = None
        if scope == "public" and decision == "approved":
            redacted = self._build_public_projection(row)

        aid = new_id("apr")
        ts = _now()

        def tx(c, seq):
            c.execute(
                "INSERT INTO snapshot_approvals (id, snapshot_id, scope, decision, decision_note,"
                " actor, redacted_content_json, decided_at) VALUES (?,?,?,?,?,?,?,?)",
                (aid, snapshot_id, scope, decision, note, actor,
                 canonical_json(redacted) if redacted is not None else None, ts),
            )
            return aid

        self.store.write(
            "decide_snapshot",
            {"id": aid, "snapshot_id": snapshot_id, "scope": scope, "decision": decision},
            actor,
            tx,
        )
        return {"approval_id": aid, "snapshot_id": snapshot_id, "scope": scope,
                "decision": decision, "redacted": redacted}

    def _build_public_projection(self, snapshot_row):
        paper = self.store.query_one(
            "SELECT * FROM papers WHERE id = ?", (snapshot_row["paper_id"],)
        )
        citations = []
        for cit in self.store.query(
            "SELECT claim_id, exact_quote FROM snapshot_citations WHERE snapshot_id = ?",
            (snapshot_row["id"],),
        ):
            claim = self._claim_row(cit["claim_id"])
            status = self.store.query_one(
                "SELECT status FROM claim_status WHERE claim_id = ?", (cit["claim_id"],)
            )["status"]
            if claim["sensitive"]:
                citations.append({"claim_id": cit["claim_id"], "redacted": True,
                                  "reason": "sensitive_unpublished_target"})
            elif status in (STATUS_UNVERIFIED, STATUS_NEEDS_REVIEW, STATUS_CONTRADICTED):
                citations.append({"claim_id": cit["claim_id"], "redacted": True,
                                  "reason": f"claim_{status}_at_release"})
            else:
                citations.append({
                    "claim_id": cit["claim_id"],
                    "claim_type": claim["claim_type"],
                    "statement": claim["statement"],
                    "species_scope": claim["species_scope"],
                    "exact_quote": cit["exact_quote"],
                    "status_at_release": status,
                    "inference_only": status == STATUS_EXTRAPOLABLE,
                })
        content = _redact_content(json.loads(snapshot_row["content_json"]))
        return {
            "snapshot_id": snapshot_row["id"],
            "paper_id": snapshot_row["paper_id"],
            "paper_title": paper["title"],
            "authors": paper["authors"],
            "frozen_at": snapshot_row["frozen_at"],
            "digest": snapshot_row["digest"],
            "content": content,
            "citations": citations,
            "note": "本投影在公开批准时冻结；标记为敏感的章节与引文（未公开靶点细节）已遮蔽。",
        }


    def public_snapshot(self, snapshot_id):
        row = self.store.query_one(
            "SELECT redacted_content_json FROM snapshot_approvals"
            " WHERE snapshot_id = ? AND scope = 'public' AND decision = 'approved'",
            (snapshot_id,),
        )
        if not row or not row["redacted_content_json"]:
            # 不向前端区分「不存在」与「未批准」，避免泄露未公开材料
            raise DomainError("公开发布版本不存在", 404)
        return json.loads(row["redacted_content_json"])

    # ---- 批次影响比较（PI） ----------------------------------------------

    def compare_batches(self, batch_a, batch_b):
        """比较两个计算批次对结论的影响。

        口径：批次的「影响时点」取该批次预测所挂证据链的最大 event_seq；
        每个相关主张返回该时点的历史状态（从 status_history 重放）与当前状态，
        从而即使后续置信度已变化，也能比较各批次当时造成的影响。
        """
        self._require_batch(batch_a)
        self._require_batch(batch_b)

        def batch_view(bid):
            meta = self.store.query_one(
                "SELECT * FROM prediction_batches WHERE id = ?", (bid,)
            )
            preds = self.store.query(
                "SELECT * FROM structure_predictions WHERE batch_id = ?", (bid,)
            )
            links = self.store.query(
                "SELECT el.* FROM evidence_links el"
                " JOIN structure_predictions sp ON sp.id = el.prediction_id"
                " WHERE sp.batch_id = ? ORDER BY el.event_seq",
                (bid,),
            )
            cutoff = max((l["event_seq"] for l in links), default=0)
            claim_ids = sorted({l["claim_id"] for l in links})
            proteins = sorted({
                self.get_protein(p["protein_id"])["canonical_name"] for p in preds
            })
            claims = []
            status_counts = {s: 0 for s in STATUSES}
            for cid in claim_ids:
                claim = self._claim_row(cid)
                then = self._status_at(cid, cutoff)
                current = self.store.query_one(
                    "SELECT status FROM claim_status WHERE claim_id = ?", (cid,)
                )["status"]
                links_at_cutoff = [
                    dict(l) for l in self.store.query(
                        "SELECT * FROM evidence_links WHERE claim_id = ? AND event_seq <= ?",
                        (cid, cutoff),
                    )
                ]
                status_counts[then] += 1
                claims.append({
                    "claim_id": cid,
                    "claim_type": claim["claim_type"],
                    "statement": claim["statement"],
                    "status_at_batch_cutoff": then,
                    "current_status": current,
                    "direct_supporting_at_cutoff": sum(
                        1 for l in links_at_cutoff
                        if l["kind"] in DIRECT_EVIDENCE_KINDS and l["weight"] == "supporting"
                    ),
                    "contradicting_at_cutoff": sum(
                        1 for l in links_at_cutoff if l["weight"] == "contradicting"
                    ),
                    "evidence_count_at_cutoff": len(links_at_cutoff),
                    "tainted_evidence_now": sum(
                        1 for l in self._untainted_links(cid) if l["taint"]
                    ),
                })
            return {
                "batch_id": bid,
                "model": meta["model"], "model_version": meta["model_version"],
                "structure_db": meta["structure_db"],
                "structure_db_version": meta["structure_db_version"],
                "cutoff_event_seq": cutoff,
                "prediction_count": len(preds),
                "proteins": proteins,
                "claim_status_counts_at_cutoff": status_counts,
                "claims": claims,
            }

        a, b = batch_view(batch_a), batch_view(batch_b)
        a_map = {c["claim_id"]: c for c in a["claims"]}
        b_map = {c["claim_id"]: c for c in b["claims"]}
        changed = []
        for cid in sorted(set(a_map) & set(b_map)):
            if a_map[cid]["status_at_batch_cutoff"] != b_map[cid]["status_at_batch_cutoff"]:
                changed.append({
                    "claim_id": cid,
                    f"status_in_{batch_a}": a_map[cid]["status_at_batch_cutoff"],
                    f"status_in_{batch_b}": b_map[cid]["status_at_batch_cutoff"],
                })
        return {
            "batch_a": a, "batch_b": b,
            "delta": {
                "claims_only_in_a": sorted(set(a_map) - set(b_map)),
                "claims_only_in_b": sorted(set(b_map) - set(a_map)),
                "status_changed_between_cutoffs": changed,
            },
        }

    def _status_at(self, claim_id, event_seq):
        """重放 status_history 得到给定全序时点的主张状态。"""
        row = self.store.query_one(
            "SELECT status FROM status_history WHERE claim_id = ? AND event_seq <= ?"
            " ORDER BY event_seq DESC, id DESC LIMIT 1",
            (claim_id, event_seq),
        )
        return row["status"] if row else STATUS_UNVERIFIED

    # ---- 审计 ----------------------------------------------------------

    def audit_tail(self, limit=50):
        rows = self.store.query(
            "SELECT seq, ts, actor, action, payload_json, prev_hash, hash"
            " FROM audit_events ORDER BY seq DESC LIMIT ?",
            (limit,),
        )
        return [{**dict(r), "payload": json.loads(r["payload_json"])} for r in rows]


def _digest_content(paper_id, frozen_at, content, citations):
    from hashlib import sha256
    body = canonical_json({
        "paper_id": paper_id, "frozen_at": frozen_at,
        "content": content,
        "citations": sorted(
            ((c["claim_id"], c["exact_quote"]) for c in citations),
            key=lambda x: x[0],
        ),
    })
    return sha256(body.encode("utf-8")).hexdigest()


# 结构化脱敏约定：以下键名的章节，或携带 confidential=true / redact=true
# 标记的对象，在公开投影中整段遮蔽；不依赖字符串匹配蛋白名，避免泄露别名。
_REDACTED_KEYS = {"target_details", "unpublished_targets", "target_mechanisms", "confidential_notes"}
_REDACTION = {"redacted": True, "reason": "sensitive_unpublished_target"}


def _redact_content(value, redact=False):
    if isinstance(value, dict):
        if value.get("confidential") is True or value.get("redact") is True:
            return dict(_REDACTION)
        out = {}
        for k, v in value.items():
            if k in _REDACTED_KEYS:
                out[k] = dict(_REDACTION)
            else:
                out[k] = _redact_content(v, redact)
        return out
    if isinstance(value, list):
        return [_redact_content(v, redact) for v in value]
    return value
