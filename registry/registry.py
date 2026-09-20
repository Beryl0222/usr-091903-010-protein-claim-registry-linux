"""Registry：命令处理与读模型投影。"""

import functools
import threading

from .store import ConflictError, EventStore, NotFoundError, ValidationError
from .vocab import (
    CLAIM_TYPES,
    EXTRAPOLATED,
    NEEDS_REVIEW,
    PAPER_DRAFT,
    PAPER_SUBMITTED,
    REFUTED,
    RELEASE_APPROVED,
    RELEASE_STATES,
    RELATION_CONTRADICTS,
    RELATION_DEPENDS_ON,
    RELATION_EXTRAPOLATION,
    RELATION_SUPPORTS,
    SUPPORTED,
    UNVALIDATED,
    WITHDRAWN_ANALYSIS,
    WITHDRAWN_SAMPLE,
)

SENSITIVE_CLAIM_TYPES = frozenset({"disease_target"})


def _require(value, message):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(message)
    return value


class Registry:
    def __init__(self, store: EventStore):
        self.store = store
        self._write_lock = threading.RLock()
        self._rebuild()
        # 所有写命令串行化：校验与追加在同一把锁内完成
        for name in (
            "register_protein", "link_ortholog", "register_computation_batch",
            "record_structure_prediction", "create_experiment", "register_sample",
            "register_control", "record_observation", "record_statistical_analysis",
            "raise_claim", "add_evidence", "add_claim_dependency",
            "withdraw_sample", "withdraw_analysis", "resolve_review",
            "create_paper", "add_paper_citation", "submit_paper", "decide_release",
        ):
            setattr(self, name, self._with_lock(getattr(self, name)))

    def _with_lock(self, method):
        @functools.wraps(method)
        def wrapper(*args, **kwargs):
            with self._write_lock:
                return method(*args, **kwargs)
        return wrapper

    def _append(self, event_type, payload, actor=None):
        """追加事件并立即更新当前投影（事件本身仍是唯一持久状态）。"""
        event = self.store.append(event_type, payload, actor)
        self._apply(event)
        return event

    # ====================================================================== #
    # 投影
    # ====================================================================== #

    def _rebuild(self):
        self.proteins = {}
        self.batches = {}
        self.predictions = {}
        self.experiments = {}
        self.claims = {}
        self.papers = {}           # 当前投影（含冻结快照指针）
        self.frozen_snapshots = {} # paper_id -> 提交时不可变快照
        self.release_decisions = {}
        self.withdrawn_samples = set()
        self.withdrawn_analyses = set()
        self._index = {}           # 全局实体 id -> 类型，防重复
        for event in self.store.events:
            self._apply(event)

    def _apply(self, event):
        etype = event["type"]
        p = event["payload"]
        seq = event["seq"]

        if etype == "protein_registered":
            protein = {
                "id": p["id"],
                "canonical_name": p["canonical_name"],
                "organism": p["organism"],
                "tax_id": p.get("tax_id"),
                "aliases": list(p.get("aliases", [])),
                "orthologs": [],
                "target_public": bool(p.get("target_public", False)),
                "updated_seq": seq,
            }
            self.proteins[p["id"]] = protein
            self._index[p["id"]] = "protein"

        elif etype == "ortholog_linked":
            self.proteins[p["protein_id"]]["orthologs"].append(
                {"protein_id": p["ortholog_id"], "note": p.get("note", "")}
            )
            self.proteins[p["protein_id"]]["updated_seq"] = seq

        elif etype == "computation_batch_registered":
            self.batches[p["id"]] = {**p, "updated_seq": seq}
            self._index[p["id"]] = "computation_batch"

        elif etype == "structure_prediction_recorded":
            prediction = {**p, "updated_seq": seq}
            self.predictions[p["id"]] = prediction
            self._index[p["id"]] = "structure_prediction"

        elif etype == "experiment_created":
            experiment = {
                "id": p["id"],
                "title": p["title"],
                "species": p["species"],
                "assay": p.get("assay", ""),
                "materials": list(p.get("materials", [])),
                "samples": {},
                "controls": {},
                "observations": {},
                "analyses": {},
                "updated_seq": seq,
            }
            self.experiments[p["id"]] = experiment
            self._index[p["id"]] = "experiment"

        elif etype == "sample_registered":
            exp = self.experiments[p["experiment_id"]]
            sample = {**p, "updated_seq": seq, "withdrawn": False}
            exp["samples"][p["id"]] = sample
            self._index[p["id"]] = "sample"

        elif etype == "control_registered":
            exp = self.experiments[p["experiment_id"]]
            exp["controls"][p["id"]] = {**p, "updated_seq": seq}
            self._index[p["id"]] = "control"

        elif etype == "observation_recorded":
            exp = self.experiments[p["experiment_id"]]
            observation = {**p, "updated_seq": seq}
            exp["observations"][p["id"]] = observation
            self._index[p["id"]] = "observation"

        elif etype == "statistical_analysis_recorded":
            exp = self.experiments[p["experiment_id"]]
            analysis = {**p, "updated_seq": seq, "withdrawn": False}
            exp["analyses"][p["id"]] = analysis
            self._index[p["id"]] = "analysis"

        elif etype == "claim_raised":
            claim = {
                "id": p["id"],
                "protein_id": p["protein_id"],
                "claim_type": p["claim_type"],
                "subject_species": p["subject_species"],
                "assertion": p["assertion"],
                "computation_batch_id": p.get("computation_batch_id"),
                "evidence": [],
                "dependencies": [],
                "review_flags": [],
                "review_notes": [],
                "raised_seq": seq,
                "updated_seq": seq,
            }
            self.claims[p["id"]] = claim
            self._index[p["id"]] = "claim"

        elif etype == "evidence_added":
            link = {**p, "seq": seq}
            self.claims[p["claim_id"]]["evidence"].append(link)
            self.claims[p["claim_id"]]["updated_seq"] = seq

        elif etype == "claim_dependency_added":
            self.claims[p["claim_id"]]["dependencies"].append(
                {
                    "depends_on": p["depends_on"],
                    "relation": p.get("relation", RELATION_DEPENDS_ON),
                    "note": p.get("note", ""),
                    "seq": seq,
                }
            )
            self.claims[p["claim_id"]]["updated_seq"] = seq

        elif etype in ("sample_withdrawn", "analysis_withdrawn"):
            kind = WITHDRAWN_SAMPLE if etype == "sample_withdrawn" else WITHDRAWN_ANALYSIS
            target_id = p["target_id"]
            if kind == WITHDRAWN_SAMPLE:
                self.withdrawn_samples.add(target_id)
                for exp in self.experiments.values():
                    if target_id in exp["samples"]:
                        exp["samples"][target_id]["withdrawn"] = True
            else:
                self.withdrawn_analyses.add(target_id)
                for exp in self.experiments.values():
                    if target_id in exp["analyses"]:
                        exp["analyses"][target_id]["withdrawn"] = True

        elif etype == "claim_flagged_review":
            claim = self.claims[p["claim_id"]]
            claim["review_flags"].append(
                {"reason": p["reason"], "since_seq": seq, "detail": p.get("detail", "")}
            )
            claim["updated_seq"] = seq

        elif etype == "review_resolved":
            claim = self.claims[p["claim_id"]]
            claim["review_flags"] = []
            claim["review_notes"].append(
                {"note": p.get("note", ""), "seq": seq, "actor": event.get("actor")}
            )
            claim["updated_seq"] = seq

        elif etype == "paper_created":
            self.papers[p["id"]] = {
                "id": p["id"],
                "title": p["title"],
                "state": PAPER_DRAFT,
                "citations": [],
                "updated_seq": seq,
            }
            self._index[p["id"]] = "paper"

        elif etype == "paper_citation_added":
            self.papers[p["paper_id"]]["citations"].append({**p, "seq": seq})
            self.papers[p["paper_id"]]["updated_seq"] = seq

        elif etype == "paper_submitted":
            paper = self.papers[p["paper_id"]]
            paper["state"] = PAPER_SUBMITTED
            paper["submitted_seq"] = seq
            paper["snapshot_seq"] = p["snapshot"]["snapshot_seq"]
            self.frozen_snapshots[p["paper_id"]] = p["snapshot"]

        elif etype == "release_decided":
            self.release_decisions[p["paper_id"]] = {
                "decision": p["decision"],
                "note": p.get("note", ""),
                "seq": seq,
                "actor": event.get("actor"),
            }

    # ====================================================================== #
    # 命令：蛋白质 / 计算批次
    # ====================================================================== #

    def register_protein(
        self, protein_id, canonical_name, organism,
        tax_id=None, aliases=None, target_public=False, actor=None,
    ):
        self._ensure_new(protein_id, "protein")
        _require(canonical_name, "canonical_name 必填")
        _require(organism, "organism 必填")
        return self._append("protein_registered", {
            "id": protein_id,
            "canonical_name": canonical_name,
            "organism": organism,
            "tax_id": tax_id,
            "aliases": list(aliases or []),
            "target_public": bool(target_public),
        }, actor)

    def link_ortholog(self, protein_id, ortholog_id, note="", actor=None):
        self._require_entity(protein_id, "protein")
        self._require_entity(ortholog_id, "protein")
        if protein_id == ortholog_id:
            raise ValidationError("蛋白不能与自身互为同源")
        existing = {o["protein_id"] for o in self.proteins[protein_id]["orthologs"]}
        if ortholog_id in existing:
            raise ConflictError(f"同源关系已存在：{ortholog_id}")
        return self._append("ortholog_linked", {
            "protein_id": protein_id, "ortholog_id": ortholog_id, "note": note,
        }, actor)

    def register_computation_batch(
        self, batch_id, source, version, search_space=None, note="", actor=None,
    ):
        """登记一次计算筛选批次，例如 AlphaFold 版本与两亿结构的搜索空间。"""
        self._ensure_new(batch_id, "computation_batch")
        _require(version, "结构预测版本 version 必填（如 afdb_v4）")
        return self._append("computation_batch_registered", {
            "id": batch_id,
            "source": _require(source, "source 必填（如 AlphaFold DB）"),
            "version": version,
            "search_space": search_space or "",
            "note": note,
        }, actor)

    def record_structure_prediction(
        self, prediction_id, protein_id, batch_id,
        target_pdb=None, tm_score=None, rmsd=None, metrics=None, actor=None,
    ):
        self._ensure_new(prediction_id, "structure_prediction")
        self._require_entity(protein_id, "protein")
        self._require_entity(batch_id, "computation_batch")
        if tm_score is not None and not (0 <= float(tm_score) <= 1):
            raise ValidationError("tm_score 必须在 [0,1]")
        return self._append("structure_prediction_recorded", {
            "id": prediction_id,
            "protein_id": protein_id,
            "batch_id": batch_id,
            "target_pdb": target_pdb,
            "tm_score": tm_score,
            "rmsd": rmsd,
            "metrics": metrics or {},
        }, actor)

    # ====================================================================== #
    # 命令：实验材料 / 样本 / 对照 / 观察 / 分析
    # ====================================================================== #

    def create_experiment(
        self, experiment_id, title, species, assay="", materials=None, actor=None,
    ):
        self._ensure_new(experiment_id, "experiment")
        return self._append("experiment_created", {
            "id": experiment_id,
            "title": _require(title, "title 必填"),
            "species": _require(species, "species 必填（实验体系物种）"),
            "assay": assay,
            "materials": list(materials or []),
        }, actor)

    def register_sample(
        self, experiment_id, sample_id, sample_type, description="",
        genotype=None, metadata=None, actor=None,
    ):
        self._require_entity(experiment_id, "experiment")
        self._ensure_new(sample_id, "sample")
        return self._append("sample_registered", {
            "id": sample_id,
            "experiment_id": experiment_id,
            "sample_type": _require(sample_type, "sample_type 必填（cell_line/strain/tissue…）"),
            "description": description,
            "genotype": genotype,
            "metadata": metadata or {},
        }, actor)

    def register_control(
        self, experiment_id, control_id, control_type,
        paired_sample_id=None, description="", actor=None,
    ):
        self._require_entity(experiment_id, "experiment")
        self._ensure_new(control_id, "control")
        if paired_sample_id is not None:
            self._require_entity(paired_sample_id, "sample")
        return self._append("control_registered", {
            "id": control_id,
            "experiment_id": experiment_id,
            "control_type": _require(control_type, "control_type 必填"),
            "paired_sample_id": paired_sample_id,
            "description": description,
        }, actor)

    def record_observation(
        self, observation_id, experiment_id, sample_id,
        endpoint, value, unit="", controls=None, conditions=None,
        species=None, actor=None,
    ):
        self._require_entity(experiment_id, "experiment")
        self._require_entity(sample_id, "sample")
        self._ensure_new(observation_id, "observation")
        exp = self.experiments[experiment_id]
        if sample_id not in exp["samples"]:
            raise ValidationError(f"样本 {sample_id} 不属于实验 {experiment_id}")
        for control_id in controls or []:
            if control_id not in exp["controls"]:
                raise ValidationError(f"对照 {control_id} 不属于实验 {experiment_id}")
        return self._append("observation_recorded", {
            "id": observation_id,
            "experiment_id": experiment_id,
            "sample_id": sample_id,
            "endpoint": _require(endpoint, "endpoint 必填（测量指标）"),
            "value": value,
            "unit": unit,
            "control_ids": list(controls or []),
            "conditions": conditions or {},
            "species": species or exp["species"],
        }, actor)

    def record_statistical_analysis(
        self, analysis_id, experiment_id, method,
        observation_ids=None, sample_ids=None,
        p_value=None, effect_size=None, ci=None, model_spec=None,
        kind="experimental", actor=None,
    ):
        self._require_entity(experiment_id, "experiment")
        self._ensure_new(analysis_id, "analysis")
        exp = self.experiments[experiment_id]
        for oid in observation_ids or []:
            if oid not in exp["observations"]:
                raise ValidationError(f"观察 {oid} 不属于实验 {experiment_id}")
        for sid in sample_ids or []:
            if sid not in exp["samples"]:
                raise ValidationError(f"样本 {sid} 不属于实验 {experiment_id}")
        if p_value is not None and not (0 <= float(p_value) <= 1):
            raise ValidationError("p_value 必须在 [0,1]")
        return self._append("statistical_analysis_recorded", {
            "id": analysis_id,
            "experiment_id": experiment_id,
            "method": _require(method, "method 必填（统计方法）"),
            "observation_ids": list(observation_ids or []),
            "sample_ids": list(sample_ids or []),
            "p_value": p_value,
            "effect_size": effect_size,
            "ci": ci,
            "model_spec": model_spec or {},
            "kind": kind,  # experimental | clinical
        }, actor)

    # ====================================================================== #
    # 命令：主张与证据连接
    # ====================================================================== #

    def raise_claim(
        self, claim_id, protein_id, claim_type, subject_species, assertion,
        computation_batch_id=None, actor=None,
    ):
        self._ensure_new(claim_id, "claim")
        self._require_entity(protein_id, "protein")
        if claim_type not in CLAIM_TYPES:
            raise ValidationError(f"未知主张类型：{claim_type}，允许 {CLAIM_TYPES}")
        _require(subject_species, "subject_species 必填（主张所断言的物种）")
        payload = {
            "id": claim_id,
            "protein_id": protein_id,
            "claim_type": claim_type,
            "subject_species": subject_species,
            "assertion": _require(assertion, "assertion 必填（粒度明确的一句话主张）"),
        }
        if computation_batch_id is not None:
            self._require_entity(computation_batch_id, "computation_batch")
            payload["computation_batch_id"] = computation_batch_id
        return self._append("claim_raised", payload, actor)

    def add_evidence(
        self, claim_id, evidence_type, evidence_id, relation,
        source_species=None, note="", actor=None,
    ):
        """把一条证据连到主张。relation 区分支持/反驳/跨物种外推。"""
        claim = self._require_entity(claim_id, "claim")
        if evidence_type not in ("structure_prediction", "observation", "analysis"):
            raise ValidationError("evidence_type 必须是 structure_prediction/observation/analysis")
        if relation not in (
            RELATION_SUPPORTS, RELATION_CONTRADICTS, RELATION_EXTRAPOLATION,
        ):
            raise ValidationError(f"未知证据关系：{relation}")
        entity = self._require_entity(evidence_id, evidence_type)
        if evidence_type == "observation":
            source_species = source_species or entity["species"]
        elif evidence_type == "analysis":
            source_species = source_species or self.experiments[entity["experiment_id"]]["species"]
        elif evidence_type == "structure_prediction":
            source_species = source_species or self.proteins[entity["protein_id"]]["organism"]
            if relation != RELATION_EXTRAPOLATION:
                # 纯计算证据在语义上只能“外推”，防止把结构相似写成实验支持
                relation = RELATION_EXTRAPOLATION
        _require(source_species, "source_species 无法推断，必须显式给出")

        direct = source_species == claim["subject_species"]
        link = {
            "claim_id": claim_id,
            "evidence_type": evidence_type,
            "evidence_id": evidence_id,
            "relation": relation,
            "source_species": source_species,
            "direct": direct,
            "note": note,
        }
        event = self._append("evidence_added", link, actor)
        return event

    def add_claim_dependency(
        self, claim_id, depends_on, relation=RELATION_DEPENDS_ON, note="", actor=None,
    ):
        """主张间推断链，例如“疾病靶点”依赖“受体身份”，后者依赖“结构相似”。"""
        self._require_entity(claim_id, "claim")
        self._require_entity(depends_on, "claim")
        if claim_id == depends_on:
            raise ValidationError("主张不能依赖自身")
        if relation not in (RELATION_DEPENDS_ON, RELATION_EXTRAPOLATION):
            raise ValidationError("主张间关系只能是 depends_on / extrapolation")
        deps = self.claims[claim_id].setdefault("dependencies", [])
        if any(d["depends_on"] == depends_on for d in deps):
            raise ConflictError(f"依赖已存在：{depends_on}")
        if self._creates_cycle(claim_id, depends_on):
            raise ValidationError("该依赖会形成循环")
        return self._append("claim_dependency_added", {
            "claim_id": claim_id,
            "depends_on": depends_on,
            "relation": relation,
            "note": note,
        }, actor)

    def _creates_cycle(self, claim_id, new_dependency):
        """加边 claim_id -> new_dependency 是否成环：
        即 new_dependency 是否（传递）依赖回 claim_id。"""
        stack = [new_dependency]
        reachable = set()
        while stack:
            current = stack.pop()
            if current in reachable:
                continue
            reachable.add(current)
            for dep in self.claims.get(current, {}).get("dependencies", []):
                stack.append(dep["depends_on"])
        return claim_id in reachable

    # ====================================================================== #
    # 命令：撤回 -> 级联待复核
    # ====================================================================== #

    def withdraw_sample(self, sample_id, reason, actor=None):
        self._require_entity(sample_id, "sample")
        if sample_id in self.withdrawn_samples:
            raise ConflictError(f"样本 {sample_id} 已撤回")
        self._append("sample_withdrawn", {
            "target_id": sample_id, "kind": WITHDRAWN_SAMPLE, "reason": reason,
        }, actor)
        invalid = self._evidence_invalidated_by_sample(sample_id)
        return self._cascade_review(invalid, f"样本 {sample_id} 撤回：{reason}", actor)

    def withdraw_analysis(self, analysis_id, reason, actor=None):
        self._require_entity(analysis_id, "analysis")
        if analysis_id in self.withdrawn_analyses:
            raise ConflictError(f"分析 {analysis_id} 已撤回")
        self._append("analysis_withdrawn", {
            "target_id": analysis_id, "kind": WITHDRAWN_ANALYSIS, "reason": reason,
        }, actor)
        return self._cascade_review({analysis_id}, f"分析 {analysis_id} 撤回：{reason}", actor)

    def _evidence_invalidated_by_sample(self, sample_id):
        invalid = set()
        for exp in self.experiments.values():
            for oid, obs in exp["observations"].items():
                if obs["sample_id"] == sample_id:
                    invalid.add(oid)
            for aid, analysis in exp["analyses"].items():
                touches = set(analysis.get("sample_ids", []))
                for oid in analysis.get("observation_ids", []):
                    obs = exp["observations"].get(oid)
                    if obs:
                        touches.add(obs["sample_id"])
                if sample_id in touches:
                    invalid.add(aid)
        return invalid

    def _cascade_review(self, invalid_evidence, reason, actor):
        """找到直接使用失效证据的主张，再沿依赖链向上传播，全部置待复核。"""
        affected = set()
        for claim_id, claim in self.claims.items():
            if any(link["evidence_id"] in invalid_evidence for link in claim["evidence"]):
                affected.add(claim_id)
        # 沿 depends_on / extrapolation 反向传播：A 依赖 B，B 受影响 -> A 受影响
        frontier = list(affected)
        while frontier:
            broken = frontier.pop()
            for claim_id, claim in self.claims.items():
                if claim_id in affected:
                    continue
                if any(d["depends_on"] in affected or d["depends_on"] == broken
                       for d in claim["dependencies"]):
                    affected.add(claim_id)
                    frontier.append(claim_id)
        events = []
        for claim_id in sorted(affected):
            claim = self.claims[claim_id]
            if claim["review_flags"]:
                continue  # 已在待复核中，不重复打标
            events.append(self._append("claim_flagged_review", {
                "claim_id": claim_id,
                "reason": reason,
                "detail": "依赖链级联",
            }, actor))
        return {"affected_claims": sorted(affected), "flag_events": events}

    def resolve_review(self, claim_id, note="", expected_status=None, actor=None):
        """复核完成：清除待复核标记，状态按剩余有效证据重新派生。"""
        claim = self._require_entity(claim_id, "claim")
        if not claim["review_flags"]:
            raise ConflictError(f"主张 {claim_id} 当前不在待复核状态")
        derived = self.derive_status(claim_id)
        if expected_status is not None and expected_status != derived:
            raise ConflictError(
                f"复核人预期状态 {expected_status}，但剩余证据派生为 {derived}"
            )
        return self._append("review_resolved", {
            "claim_id": claim_id,
            "note": note,
            "derived_status": derived,
        }, actor)

    # ====================================================================== #
    # 命令：论文快照（冻结）与公开发布审批
    # ====================================================================== #

    def create_paper(self, paper_id, title, authors=None, doi=None, actor=None):
        self._ensure_new(paper_id, "paper")
        return self._append("paper_created", {
            "id": paper_id,
            "title": _require(title, "title 必填"),
            "authors": list(authors or []),
            "doi": doi,
        }, actor)

    def add_paper_citation(
        self, paper_id, ref_key, claim_id, quoted_assertion=None, actor=None,
    ):
        """草稿中登记“此处引用了哪条主张”。提交后不可再改。"""
        paper = self._require_entity(paper_id, "paper")
        self._require_entity(claim_id, "claim")
        if paper["state"] != PAPER_DRAFT:
            raise ConflictError(f"论文 {paper_id} 已提交，快照冻结，不能新增引用")
        if any(c["ref_key"] == ref_key for c in paper["citations"]):
            raise ConflictError(f"引用标号已存在：{ref_key}")
        claim = self.claims[claim_id]
        return self._append("paper_citation_added", {
            "paper_id": paper_id,
            "ref_key": _require(ref_key, "ref_key 必填（文中引用编号）"),
            "claim_id": claim_id,
            "protein_id": claim["protein_id"],
            "claim_type": claim["claim_type"],
            "quoted_assertion": quoted_assertion or claim["assertion"],
            "claim_status_at_citation": self.derive_status(claim_id),
        }, actor)

    def submit_paper(self, paper_id, actor=None):
        """提交论文：把每条引用连同当时的主张状态冻结为不可变快照。"""
        paper = self._require_entity(paper_id, "paper")
        if paper["state"] != PAPER_DRAFT:
            raise ConflictError(f"论文 {paper_id} 已提交，不得重复提交")
        if not paper["citations"]:
            raise ValidationError("论文至少包含一条主张引用才能提交")
        frozen_citations = []
        for citation in paper["citations"]:
            claim = self.claims[citation["claim_id"]]
            frozen_citations.append({
                "ref_key": citation["ref_key"],
                "claim_id": claim["id"],
                "claim_type": claim["claim_type"],
                "protein_id": claim["protein_id"],
                "subject_species": claim["subject_species"],
                "quoted_assertion": citation["quoted_assertion"],
                "status_at_snapshot": self.derive_status(claim["id"]),
                "claim_version": claim["updated_seq"],
                "evidence_summary": self._evidence_summary(claim),
            })
        snapshot = {
            "paper_id": paper_id,
            "title": paper["title"],
            "authors": list(paper.get("authors", [])),
            "doi": paper.get("doi"),
            "snapshot_seq": self.store.seq() + 1,
            "citations": frozen_citations,
        }
        return self._append("paper_submitted", {
            "paper_id": paper_id, "snapshot": snapshot,
        }, actor)

    def decide_release(self, paper_id, decision, note="", actor=None):
        """机构审批某篇已提交论文是否可进入公开接口。"""
        paper = self._require_entity(paper_id, "paper")
        if paper["state"] != PAPER_SUBMITTED:
            raise ConflictError("只有已提交论文可审批")
        if decision not in RELEASE_STATES:
            raise ValidationError(f"decision 必须是 {RELEASE_STATES}")
        return self._append("release_decided", {
            "paper_id": paper_id, "decision": decision, "note": note,
        }, actor)

    # ====================================================================== #
    # 状态派生
    # ====================================================================== #

    def _evidence_summary(self, claim):
        """快照与追溯共用：每条证据是否仍有效、直接还是跨物种。"""
        items = []
        for link in claim["evidence"]:
            items.append({
                "evidence_type": link["evidence_type"],
                "evidence_id": link["evidence_id"],
                "relation": link["relation"],
                "source_species": link["source_species"],
                "direct": link["direct"],
                "active": self._link_active(link),
                "domain": self._link_domain(link),
            })
        return items

    def _link_active(self, link):
        etype = link["evidence_type"]
        eid = link["evidence_id"]
        if etype == "structure_prediction":
            return eid in self.predictions
        if etype == "observation":
            if any(eid in exp["observations"] for exp in self.experiments.values()):
                for exp in self.experiments.values():
                    obs = exp["observations"].get(eid)
                    if obs:
                        return obs["sample_id"] not in self.withdrawn_samples
        if etype == "analysis":
            if eid in self.withdrawn_analyses:
                return False
            for exp in self.experiments.values():
                analysis = exp["analyses"].get(eid)
                if analysis:
                    if any(sid in self.withdrawn_samples for sid in analysis.get("sample_ids", [])):
                        return False
                    for oid in analysis.get("observation_ids", []):
                        obs = exp["observations"].get(oid)
                        if obs and obs["sample_id"] in self.withdrawn_samples:
                            return False
                    return True
        return False

    def _link_domain(self, link):
        etype = link["evidence_type"]
        eid = link["evidence_id"]
        if etype == "structure_prediction":
            return "computational"
        if etype == "analysis":
            for exp in self.experiments.values():
                analysis = exp["analyses"].get(eid)
                if analysis:
                    return "translational" if analysis.get("kind") == "clinical" else "experimental"
        return "experimental"

    def derive_status(self, claim_id):
        claim = self.claims[claim_id]
        if claim["review_flags"]:
            return NEEDS_REVIEW

        active = [link for link in claim["evidence"] if self._link_active(link)]
        direct_contradictions = [
            l for l in active
            if l["direct"] and l["relation"] == RELATION_CONTRADICTS
            and self._link_domain(l) in ("experimental", "translational")
        ]
        if direct_contradictions:
            return REFUTED

        direct_support = [
            l for l in active
            if l["direct"] and l["relation"] == RELATION_SUPPORTS
            and self._link_domain(l) in ("experimental", "translational")
        ]
        if direct_support:
            return SUPPORTED

        indirect = [
            l for l in active
            if not l["direct"]
            or l["relation"] == RELATION_EXTRAPOLATION
            or self._link_domain(l) == "computational"
        ]
        if indirect:
            return EXTRAPOLATED
        return UNVALIDATED

    # ====================================================================== #
    # 读模型
    # ====================================================================== #

    def list_claims(self, protein_id=None, status=None, claim_type=None):
        rows = []
        for claim in self.claims.values():
            if protein_id and claim["protein_id"] != protein_id:
                continue
            if claim_type and claim["claim_type"] != claim_type:
                continue
            current = self.derive_status(claim["id"])
            if status and current != status:
                continue
            rows.append(self._claim_view(claim, current))
        return sorted(rows, key=lambda r: r["id"])

    def get_claim(self, claim_id):
        claim = self._require_entity(claim_id, "claim")
        return self._claim_view(claim, self.derive_status(claim_id))

    def _claim_view(self, claim, status):
        return {
            "id": claim["id"],
            "protein_id": claim["protein_id"],
            "claim_type": claim["claim_type"],
            "subject_species": claim["subject_species"],
            "assertion": claim["assertion"],
            "computation_batch_id": claim.get("computation_batch_id"),
            "status": status,
            "evidence": self._evidence_summary(claim),
            "dependencies": list(claim["dependencies"]),
            "review_flags": list(claim["review_flags"]),
            "review_notes": list(claim["review_notes"]),
            "version": claim["updated_seq"],
        }

    def claim_trace(self, claim_id):
        """同行评议视图：直接证据、跨物种推断边界、完整推断链与状态时间线。"""
        claim = self._require_entity(claim_id, "claim")
        evidence = self._evidence_summary(claim)
        direct = [e for e in evidence if e["direct"]]
        cross_species = [e for e in evidence if not e["direct"]]
        chain = self._inference_chain(claim_id)
        timeline = self._claim_timeline(claim_id)
        return {
            "claim": self.get_claim(claim_id),
            "direct_evidence": direct,
            "cross_species_evidence": cross_species,
            "species_boundary": {
                "subject_species": claim["subject_species"],
                "direct_species": sorted({e["source_species"] for e in direct}),
                "extrapolated_from_species": sorted({
                    e["source_species"] for e in cross_species
                }),
                "note": "跨物种证据只支撑 extrapolated，不构成同物种直接证明或反驳",
            },
            "inference_chain": chain,
            "timeline": timeline,
        }

    def _inference_chain(self, claim_id):
        """返回该主张依赖的上游主张（传递闭包）及每一跳的关系。"""
        result = []
        seen = set()
        stack = [(claim_id, 0)]
        while stack:
            current, depth = stack.pop()
            for dep in self.claims.get(current, {}).get("dependencies", []):
                key = (current, dep["depends_on"])
                if key in seen:
                    continue
                seen.add(key)
                upstream = self.claims[dep["depends_on"]]
                result.append({
                    "from": current,
                    "to": dep["depends_on"],
                    "relation": dep["relation"],
                    "depth": depth + 1,
                    "upstream_status": self.derive_status(dep["depends_on"]),
                    "upstream_claim_type": upstream["claim_type"],
                })
                stack.append((dep["depends_on"], depth + 1))
        return sorted(result, key=lambda r: (r["depth"], r["from"], r["to"]))

    def _claim_timeline(self, claim_id):
        timeline = []
        for event in self.store.events:
            p = event["payload"]
            relevant = event["type"] in (
                "claim_raised", "evidence_added", "claim_dependency_added",
                "claim_flagged_review", "review_resolved",
            ) and p.get("claim_id") == claim_id
            if relevant:
                timeline.append({
                    "seq": event["seq"], "ts": event["ts"],
                    "type": event["type"],
                    "status_after": self._status_at(claim_id, event["seq"]),
                })
        return timeline

    def _status_at(self, claim_id, upto_seq):
        """按事件切片重放该主张在 upto_seq 时刻的置信状态。"""
        claim = self.claims[claim_id]
        if upto_seq < claim["raised_seq"]:
            return None
        links, flags = [], 0
        withdrawn_samples, withdrawn_analyses = set(), set()
        for event in self.store.events:
            if event["seq"] > upto_seq:
                break
            etype, p = event["type"], event["payload"]
            if etype == "evidence_added" and p.get("claim_id") == claim_id:
                links.append(p)
            elif etype == "claim_flagged_review" and p.get("claim_id") == claim_id:
                flags += 1
            elif etype == "review_resolved" and p.get("claim_id") == claim_id:
                flags = 0
            elif etype == "sample_withdrawn":
                withdrawn_samples.add(p["target_id"])
            elif etype == "analysis_withdrawn":
                withdrawn_analyses.add(p["target_id"])
        if flags:
            return NEEDS_REVIEW

        def active_at(link):
            etype, eid = link["evidence_type"], link["evidence_id"]
            if etype == "structure_prediction":
                return True
            if etype == "analysis":
                if eid in withdrawn_analyses:
                    return False
                for exp in self.experiments.values():
                    analysis = exp["analyses"].get(eid)
                    if analysis:
                        if any(s in withdrawn_samples for s in analysis.get("sample_ids", [])):
                            return False
                        for oid in analysis.get("observation_ids", []):
                            obs = exp["observations"].get(oid)
                            if obs and obs["sample_id"] in withdrawn_samples:
                                return False
                return True
            if etype == "observation":
                for exp in self.experiments.values():
                    obs = exp["observations"].get(eid)
                    if obs:
                        return obs["sample_id"] not in withdrawn_samples
            return False

        active = [l for l in links if active_at(l)]
        if any(l["direct"] and l["relation"] == RELATION_CONTRADICTS
               and self._link_domain(l) in ("experimental", "translational")
               for l in active):
            return REFUTED
        if any(l["direct"] and l["relation"] == RELATION_SUPPORTS
               and self._link_domain(l) in ("experimental", "translational")
               for l in active):
            return SUPPORTED
        if active:
            return EXTRAPOLATED
        return UNVALIDATED

    def get_paper(self, paper_id):
        paper = self._require_entity(paper_id, "paper")
        view = {
            "id": paper["id"],
            "title": paper["title"],
            "state": paper["state"],
            "authors": paper.get("authors", []),
            "doi": paper.get("doi"),
            "citations": [
                {
                    **{k: v for k, v in c.items() if k != "seq"},
                    "current_status": self.derive_status(c["claim_id"]),
                    "status_changed_since_citation":
                        self.derive_status(c["claim_id"]) != c["claim_status_at_citation"],
                }
                for c in paper["citations"]
            ],
            "release": self.release_decisions.get(paper_id),
        }
        if paper["state"] == PAPER_SUBMITTED:
            view["frozen_snapshot_seq"] = self.frozen_snapshots[paper_id]["snapshot_seq"]
        return view

    def list_papers(self):
        return [self.get_paper(pid) for pid in sorted(self.papers)]

    def public_papers(self):
        """公开接口：仅释放已批准论文的冻结快照，并脱敏未公开靶点细节。"""
        released = []
        for paper_id, decision in self.release_decisions.items():
            if decision["decision"] != RELEASE_APPROVED:
                continue
            snapshot = self.frozen_snapshots.get(paper_id)
            if not snapshot:
                continue
            released.append(self._public_snapshot(snapshot))
        return sorted(released, key=lambda s: s["paper_id"])

    def public_paper(self, paper_id):
        decision = self.release_decisions.get(paper_id)
        if not decision or decision["decision"] != RELEASE_APPROVED:
            raise NotFoundError(f"论文 {paper_id} 未批准公开发布")
        snapshot = self.frozen_snapshots.get(paper_id)
        if not snapshot:
            raise NotFoundError(f"论文 {paper_id} 无冻结快照")
        return self._public_snapshot(snapshot)

    def _public_snapshot(self, snapshot):
        citations = []
        for c in snapshot["citations"]:
            protein = self.proteins[c["protein_id"]]
            sensitive = (
                c["claim_type"] in SENSITIVE_CLAIM_TYPES and not protein["target_public"]
            )
            if sensitive:
                citations.append({
                    "ref_key": c["ref_key"],
                    "redacted": True,
                    "reason": "unpublished_target_detail",
                })
            else:
                citations.append({
                    "ref_key": c["ref_key"],
                    "claim_type": c["claim_type"],
                    "subject_species": c["subject_species"],
                    "assertion": c["quoted_assertion"],
                    "status_at_snapshot": c["status_at_snapshot"],
                    "evidence_domains": sorted({
                        e["domain"] for e in c["evidence_summary"] if e["active"]
                    }),
                    # “直接证据”特指同物种实验/转化证据；
                    # 纯计算与跨物种证据即使物种字段相同也不计入
                    "direct_evidence": any(
                        e["direct"] and e["active"]
                        and e["domain"] in ("experimental", "translational")
                        for e in c["evidence_summary"]
                    ),
                })
        return {
            "paper_id": snapshot["paper_id"],
            "title": snapshot["title"],
            "doi": snapshot["doi"],
            "snapshot_seq": snapshot["snapshot_seq"],
            "citations": citations,
        }

    def batch_impact(self, batch_id):
        """项目负责人视角：一个计算批次影响了哪些预测与下游主张。"""
        self._require_entity(batch_id, "computation_batch")
        predictions = [p for p in self.predictions.values() if p["batch_id"] == batch_id]
        claims = []
        for claim in self.claims.values():
            touched = claim.get("computation_batch_id") == batch_id or any(
                l["evidence_type"] == "structure_prediction"
                and self.predictions.get(l["evidence_id"], {}).get("batch_id") == batch_id
                for l in claim["evidence"]
            )
            if touched:
                claims.append(self.get_claim(claim["id"]))
        return {
            "batch": self.batches[batch_id],
            "predictions": [dict(p) for p in predictions],
            "claims": claims,
            "claim_status_counts": self._count_by(claims, "status"),
            "claim_type_counts": self._count_by(claims, "claim_type"),
        }

    def compare_batches(self, batch_a, batch_b):
        """比较两批计算筛选对主张置信状态的影响。"""
        impact_a = self.batch_impact(batch_a)
        impact_b = self.batch_impact(batch_b)
        return {
            "a": {
                "batch_id": batch_a,
                "version": impact_a["batch"]["version"],
                "prediction_count": len(impact_a["predictions"]),
                "claim_status_counts": impact_a["claim_status_counts"],
                "claim_type_counts": impact_a["claim_type_counts"],
            },
            "b": {
                "batch_id": batch_b,
                "version": impact_b["batch"]["version"],
                "prediction_count": len(impact_b["predictions"]),
                "claim_status_counts": impact_b["claim_status_counts"],
                "claim_type_counts": impact_b["claim_type_counts"],
            },
        }

    @staticmethod
    def _count_by(rows, key):
        counts = {}
        for row in rows:
            counts[row[key]] = counts.get(row[key], 0) + 1
        return counts

    # ====================================================================== #
    # 辅助
    # ====================================================================== #

    def _ensure_new(self, entity_id, kind):
        _require(entity_id, f"{kind} id 必填")
        if entity_id in self._index:
            raise ConflictError(f"{kind} 标识已存在：{entity_id}")

    def _require_entity(self, entity_id, kind):
        if kind == "protein" and entity_id in self.proteins:
            return self.proteins[entity_id]
        if kind == "computation_batch" and entity_id in self.batches:
            return self.batches[entity_id]
        if kind == "structure_prediction" and entity_id in self.predictions:
            return self.predictions[entity_id]
        if kind == "experiment" and entity_id in self.experiments:
            return self.experiments[entity_id]
        if kind == "claim" and entity_id in self.claims:
            return self.claims[entity_id]
        if kind == "paper" and entity_id in self.papers:
            return self.papers[entity_id]
        if kind in ("sample", "control", "observation", "analysis"):
            buckets = {
                "sample": "samples",
                "control": "controls",
                "observation": "observations",
                "analysis": "analyses",
            }
            for exp in self.experiments.values():
                bucket = exp[buckets[kind]]
                if entity_id in bucket:
                    return bucket[entity_id]
        raise NotFoundError(f"{kind} 不存在：{entity_id}")
