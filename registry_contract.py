"""端到端契约：TM184C 科学主张登记叙事。

覆盖：
1) 蛋白别名/物种/预测版本/实验材料/对照/观察/统计 与显式粒度主张的连接；
2) 四级置信（支持/反驳/尚未验证/仅可外推）+ 待复核，状态迁移有守卫；
3) 新证据改变当前置信，但论文快照与当时引文冻结不改写；
4) 撤回样本/实验不删除数据，直接结论与跨物种推断链上的结论一律进入待复核；
5) 评审者追踪视图划出直接证据与跨物种推断边界；
6) PI 按计算批次的影响时点比较两批次；
7) 公开接口仅释放批准版本，敏感靶点章节/引文结构化遮蔽；
8) 角色边界（HTTP）；
9) 哈希链审计自检。
"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from registry.api import create_handler
from registry.domain import (
    Registry,
    STATUS_CONTRADICTED,
    STATUS_EXTRAPOLABLE,
    STATUS_NEEDS_REVIEW,
    STATUS_SUPPORTED,
    STATUS_UNVERIFIED,
    DomainError,
)
from registry.store import Store
from service import health_payload

HUMAN = "Homo sapiens"
YEAST = "Saccharomyces cerevisiae"


def build_scenario():
    store = Store(":memory:")
    r = Registry(store)

    # ---- 蛋白与别名 ----------------------------------------------------
    tm = r.register_protein("TM184C", HUMAN, aliases=["TM-184C"], actor="lu")
    ytm = r.register_protein("Ytm184c", YEAST, aliases=["YOR-TM184"], actor="lu")

    # ---- 两个计算批次：两亿+预测结构筛选 v1 / 重算 v2 -------------------
    b1 = r.create_prediction_batch(
        "FoldHive", "v1.0", "AlphaFold-like DB", "2023-11",
        run_note="2.1亿结构受体样筛选", actor="ai-pipeline")
    b2 = r.create_prediction_batch(
        "FoldHive", "v2.0", "AlphaFold-like DB", "2024-06",
        run_note="重算批次，更新模板库", actor="ai-pipeline")
    p1 = r.add_structure_prediction("TM184C", b1, confidence=0.91,
                                    metadata={"rank": 14, "candidates": 210_000_000},
                                    actor="ai-pipeline")
    p2 = r.add_structure_prediction("TM184C", b2, confidence=0.42,
                                    metadata={"rank": 8820, "note": "模板更新后相似度下降"},
                                    actor="ai-pipeline")

    # ---- 主张 1：结构相似（计算直接证据，supported） --------------------
    c_struct = r.create_claim(
        "structural_similarity", "TM184C", "structurally_resembles",
        "TM184C 与 GPCR 家族参考结构在跨膜螺旋排布上相似",
        HUMAN, "molecular_structure", actor="lu")
    r.attach_evidence(c_struct, "computational", "supporting",
                      prediction_id=p1, note="两亿结构筛选排名第14",
                      assess_status=STATUS_SUPPORTED, actor="lu")

    # ---- 实验 A：细胞连接 ----------------------------------------------
    cell_line = r.register_material("cell_line", "HT-29 克隆系", species=HUMAN,
                                    source="细胞库-7", actor="wang")
    ctrl_line = r.register_material("cell_line", "HT-29 空载体对照", species=HUMAN,
                                    source="细胞库-7", actor="wang")
    e_junction = r.create_experiment("EXP-JX-01", "TM184C 与紧密连接标志物共定位", actor="wang")
    r.attach_material(e_junction, cell_line, "sample", actor="wang")
    r.attach_material(e_junction, ctrl_line, "control", actor="wang")
    o_jx = r.record_observation(
        e_junction, "ZO-1 共定位", "TM184C 阳性细胞中 ZO-1 共定位信号增强", actor="wang")
    a_jx = r.record_analysis(
        e_junction, "皮尔逊相关 + 双侧 t 检验", "共定位显著高于对照",
        stats={"pearson_r": 0.71, "p_value": 0.0021, "n": 6,
               "multiple_testing": "Benjamini-Hochberg", "fdr": 0.012},
        actor="wang")

    # ---- 主张 2：参与调节细胞连接（实验直接证据，supported） -------------
    c_junction = r.create_claim(
        "regulatory_involvement", "TM184C", "regulates",
        "TM184C 参与上皮细胞紧密连接稳定性的调节",
        HUMAN, "cellular", actor="wang")
    r.attach_evidence(c_junction, "experimental", "supporting",
                      experiment_id=e_junction, observation_id=o_jx, analysis_id=a_jx,
                      assess_status=STATUS_SUPPORTED, actor="wang")

    # ---- 实验 B：自噬标志物（数据未齐，主张保持尚未验证） ----------------
    e_autophagy = r.create_experiment("EXP-AP-02", "LC3-II 通量检测", actor="chen")
    r.attach_material(e_autophagy, cell_line, "sample", actor="chen")
    r.attach_material(e_autophagy, ctrl_line, "control", actor="chen")
    c_autophagy = r.create_claim(
        "regulatory_involvement", "TM184C", "modulates_autophagy",
        "TM184C 可能调节自噬流（LC3-II 标志物实验进行中）",
        HUMAN, "cellular", actor="chen")
    # 刻意不挂证据：保持 unverified

    # ---- 实验 C：酵母同源蛋白救援 --------------------------------------
    yeast_strain = r.register_material("strain", "ytm184cΔ 敲除株", species=YEAST,
                                       source="酵母种质库", actor="chen")
    rescue_plasmid = r.register_material("construct", "pYTM184c 救援质粒", species=YEAST,
                                         actor="chen")
    e_rescue = r.create_experiment("EXP-YST-03", "Ytm184c 敲除株热胁迫救援", actor="chen")
    r.attach_material(e_rescue, yeast_strain, "sample", actor="chen")
    r.attach_material(e_rescue, rescue_plasmid, "reagent", actor="chen")
    o_rescue = r.record_observation(
        e_rescue, "42℃ 存活率", "回补 Ytm184c 后敲除株热胁迫存活率恢复至野生型 92%",
        actor="chen")
    a_rescue = r.record_analysis(
        e_rescue, "单因素方差分析", "救援效应显著",
        stats={"f": 31.4, "p_value": 0.0004, "n": 9, "posthoc": "Tukey HSD"},
        actor="chen")

    # 酵母内的直接功能主张（有直接实验证据）
    c_yeast_func = r.create_claim(
        "functional", "Ytm184c", "confers",
        "Ytm184c 在热胁迫应答中维持细胞存活",
        YEAST, "organism", actor="chen")
    r.attach_evidence(c_yeast_func, "experimental", "supporting",
                      experiment_id=e_rescue, observation_id=o_rescue, analysis_id=a_rescue,
                      assess_status=STATUS_SUPPORTED, actor="chen")

    # ---- 主张 4：人类疾病靶点 —— 敏感、仅跨物种外推（extrapolable） -----
    c_target = r.create_claim(
        "disease_target", "TM184C", "candidate_target_for",
        "TM184C 是某未公开肠道疾病项目的候选药物靶点",
        HUMAN, "cross_species", sensitive=True, actor="pi")
    r.attach_evidence(c_target, "inferential", "supporting",
                      source_claim_id=c_yeast_func,
                      inference_basis="同源蛋白（序列同一性 34%）酵母热胁迫救援",
                      species_from=YEAST, species_to=HUMAN,
                      note="跨物种外推，人类中无直接证据",
                      assess_status=STATUS_EXTRAPOLABLE, actor="pi")

    return dict(store=store, r=r, tm=tm, ytm=ytm, b1=b1, b2=b2, p1=p1, p2=p2,
                cell_line=cell_line, e_junction=e_junction, o_jx=o_jx, a_jx=a_jx,
                e_rescue=e_rescue, c_struct=c_struct, c_junction=c_junction,
                c_autophagy=c_autophagy, c_yeast_func=c_yeast_func, c_target=c_target)


class Tm184cNarrativeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = build_scenario()
        cls.r = cls.s["r"]

    # ---- 1. 基础连接与别名解析 ------------------------------------------

    def test_01_aliases_resolve_to_same_protein_and_species_scope(self):
        via_alias = self.r.get_protein("TM-184C")
        via_canonical = self.r.get_protein("TM184C")
        self.assertEqual(via_alias["id"], via_canonical["id"])
        self.assertEqual(via_alias["species"], HUMAN)
        self.assertIn("TM-184C", via_alias["aliases"])
        yeast = self.r.get_protein("YOR-TM184")
        self.assertEqual(yeast["species"], YEAST)
        self.assertNotEqual(yeast["id"], via_alias["id"])

    # ---- 2. 置信状态守卫：不同确定性不能混写 -----------------------------

    def test_02_status_guards_enforce_evidence_standards(self):
        fresh = self.r.create_claim(
            "localization", "TM184C", "localizes_to",
            "临时主张：无证据不能 supported", HUMAN, "cellular")
        with self.assertRaises(DomainError) as e:
            r_assess = self.r.assess_claim(fresh, STATUS_SUPPORTED, "口头印象")
        self.assertEqual(e.exception.status, 400)
        self.assertIn("直接支持证据", str(e.exception))
        # 仍为尚未验证
        self.assertEqual(self.r.claim_status(fresh)["status"], STATUS_UNVERIFIED)

        # 外推必须有推断证据，且有直接证据时不得降级为外推
        with self.assertRaises(DomainError):
            self.r.assess_claim(fresh, STATUS_EXTRAPOLABLE, "猜的")
        with self.assertRaises(DomainError):
            self.r.assess_claim(self.s["c_struct"], STATUS_EXTRAPOLABLE,
                                "结构主张已有直接证据，不能仅写外推")
        with self.assertRaises(DomainError):
            self.r.assess_claim(fresh, STATUS_CONTRADICTED, "没有反驳数据")

    def test_03_three_claim_types_carry_distinct_certainty(self):
        self.assertEqual(self.r.claim_status(self.s["c_struct"])["status"], STATUS_SUPPORTED)
        self.assertEqual(self.r.claim_status(self.s["c_junction"])["status"], STATUS_SUPPORTED)
        self.assertEqual(self.r.claim_status(self.s["c_autophagy"])["status"], STATUS_UNVERIFIED)
        self.assertEqual(self.r.claim_status(self.s["c_target"])["status"], STATUS_EXTRAPOLABLE)

    # ---- 3. 论文快照在提交时冻结，后续变化不重写 --------------------------

    def test_04_paper_snapshot_freezes_quotes_against_later_changes(self):
        r = self.r
        paper = r.create_paper("TM184C 受体样功能研究（投稿稿）",
                               "课题组全体", actor="lu")
        content = {
            "abstract": "本稿报告 TM184C 的结构线索与细胞连接表型。",
            "target_details": {"binding_pocket": "未公开的结合口袋坐标与先导化合物",
                               "confidential": True},
            "sections": {"summary": "结论引用登记库主张。"},
        }
        citations = [
            {"claim_id": self.s["c_struct"],
             "exact_quote": "两亿结构筛选提示 TM184C 为受体成员（支持）。"},
            {"claim_id": self.s["c_junction"],
             "exact_quote": "TM184C 参与紧密连接调节（支持）。"},
            {"claim_id": self.s["c_target"],
             "exact_quote": "TM184C 是候选靶点（跨物种外推）。"},
        ]
        snap = r.submit_snapshot(paper, content, citations=citations, actor="lu")
        before = r.get_snapshot(snap)
        self.assertTrue(before["immutable"])
        frozen_digest = before["digest"]
        frozen_content = json.loads(json.dumps(before["content"], ensure_ascii=False))

        # 新数据（v2 批次）到达：结构主张被反驳，当前置信改变
        r.attach_evidence(self.s["c_struct"], "computational", "contradicting",
                          prediction_id=self.s["p2"],
                          note="重算后结构相似度回落",
                          assess_status=STATUS_CONTRADICTED, actor="ai-pipeline")
        self.assertEqual(r.claim_status(self.s["c_struct"])["status"], STATUS_CONTRADICTED)

        after = r.get_snapshot(snap)
        self.assertEqual(after["digest"], frozen_digest)
        self.assertEqual(after["frozen_at"], before["frozen_at"])
        self.assertEqual(after["content"], frozen_content)
        # 引文原文不改写；仅并置展示当前状态
        quotes = {c["claim_id"]: c for c in after["citations"]}
        self.assertEqual(
            quotes[self.s["c_struct"]]["exact_quote"],
            "两亿结构筛选提示 TM184C 为受体成员（支持）。")
        self.assertEqual(quotes[self.s["c_struct"]]["current_status"], STATUS_CONTRADICTED)
        self.s["paper1"] = paper
        self.s["snap1"] = snap

    # ---- 4. 撤回：样本污染传播到直接结论，不删除 --------------------------

    def test_05_material_retraction_moves_dependent_claims_to_review(self):
        result = self.r.retract(
            "HT-29 克隆系支原体污染，相关样本全部撤回",
            material_id=self.s["cell_line"], actor="curator")
        self.assertIn(self.s["c_junction"], result["affected_claims"])
        # 结构主张只依赖计算证据，不受细胞样本撤回影响
        self.assertNotIn(self.s["c_struct"], result["affected_claims"])
        self.assertEqual(
            self.r.claim_status(self.s["c_junction"])["status"], STATUS_NEEDS_REVIEW)

        # 数据未被删除：实验与材料仍可查；实验本身未撤回，但样本材料带撤回标记
        exp = next(l for l in self.r.claim_trace(self.s["c_junction"])["direct_evidence"]
                   if l["experiment_id"] == self.s["e_junction"])
        self.assertFalse(exp["experiment"]["retracted"])
        sample = next(m for m in exp["experiment"]["materials"] if m["role"] == "sample")
        self.assertTrue(sample["retracted"])
        self.assertIsNotNone(exp["taint"])
        self.s["retraction_material"] = result["retraction_id"]

    # ---- 5. 撤回沿跨物种推断链继续传播 -----------------------------------

    def test_06_retraction_propagates_across_inference_boundary(self):
        result = self.r.retract(
            "救援实验存活率原始记录无法复核，分析撤回",
            experiment_id=self.s["e_rescue"], actor="curator")
        affected = set(result["affected_claims"])
        self.assertIn(self.s["c_yeast_func"], affected)
        self.assertIn(self.s["c_target"], affected)
        # 人类靶点主张的依赖路径必须显式包含跨物种推断跳
        path = result["paths"][self.s["c_target"]]
        self.assertTrue(any(p.startswith("inference:") and YEAST in p and HUMAN in p
                            for p in path))
        self.assertEqual(self.r.claim_status(self.s["c_target"])["status"],
                         STATUS_NEEDS_REVIEW)

    # ---- 6. 评审者追踪：直接证据 vs 跨物种边界 ---------------------------

    def test_07_reviewer_trace_shows_boundary_and_history(self):
        trace = self.r.claim_trace(self.s["c_target"])
        self.assertEqual(trace["claim"]["granularity"], "cross_species")
        self.assertEqual(trace["current_status"]["status"], STATUS_NEEDS_REVIEW)
        # 人类靶点主张没有任何人类直接实验证据
        self.assertEqual(trace["direct_evidence"], [])
        self.assertEqual(trace["inference_boundary"]["has_direct_evidence"], False)
        self.assertEqual(trace["inference_boundary"]["inference_count"], 1)
        inf = trace["cross_species_inferences"][0]
        self.assertEqual(inf["species_from"], YEAST)
        self.assertEqual(inf["species_to"], HUMAN)
        self.assertEqual(inf["source_claim"]["id"], self.s["c_yeast_func"])
        self.assertEqual(inf["source_claim"]["current_status"], STATUS_NEEDS_REVIEW)
        # 递归展开来源主张：其直接证据是酵母救援实验，且现已污染
        src_direct = inf["source_claim"]["trace"]["direct_evidence"]
        self.assertEqual(len(src_direct), 1)
        self.assertIsNotNone(src_direct[0]["taint"])
        # 状态历史完整：unverified -> extrapolable -> needs_review
        history = [h["status"] for h in trace["status_history"]]
        self.assertEqual(history, [STATUS_UNVERIFIED, STATUS_EXTRAPOLABLE,
                                   STATUS_NEEDS_REVIEW])
        # 撤回影响记录保留依赖路径
        self.assertTrue(any(i["retraction_id"] == self.s["retraction_material"]
                            for i in self.r.claim_trace(self.s["c_junction"])
                            ["retraction_impacts"]))

    def test_08_reviewer_sees_materials_controls_and_stats_on_direct_evidence(self):
        # 细胞连接实验虽撤回，追踪视图仍给出材料/对照/统计的完整出处
        trace = self.r.claim_trace(self.s["c_junction"])
        link = trace["direct_evidence"][0]
        roles = {m["role"]: m["name"] for m in link["experiment"]["materials"]}
        self.assertEqual(roles["control"], "HT-29 空载体对照")
        self.assertIn("HT-29", roles["sample"])
        self.assertEqual(link["analysis"]["stats"]["p_value"], 0.0021)
        self.assertEqual(link["analysis"]["stats"]["fdr"], 0.012)
        self.assertEqual(link["observation"]["label"], "ZO-1 共定位")

    # ---- 7. 待复核处置：复核后才能回到尚未验证 ---------------------------

    def test_09_review_resolution_requires_explicit_reset(self):
        with self.assertRaises(DomainError):
            # 非 needs_review 状态不能随意重置为 unverified
            self.r.assess_claim(self.s["c_autophagy"], STATUS_UNVERIFIED, "noop")
        # 待复核主张在污染证据排除后，可显式重置为尚未验证
        self.r.assess_claim(self.s["c_junction"], STATUS_UNVERIFIED,
                            "污染样本撤回，结论待重做", actor="curator")
        self.assertEqual(self.r.claim_status(self.s["c_junction"])["status"],
                         STATUS_UNVERIFIED)

    # ---- 8. PI 批次影响比较（历史时点） ----------------------------------

    def test_10_pi_compares_batches_by_impact_cutoff(self):
        cmp = self.r.compare_batches(self.s["b1"], self.s["b2"])
        a, b = cmp["batch_a"], cmp["batch_b"]
        self.assertEqual(a["model_version"], "v1.0")
        self.assertEqual(b["model_version"], "v2.0")
        self.assertLess(a["cutoff_event_seq"], b["cutoff_event_seq"])
        ca = next(c for c in a["claims"] if c["claim_id"] == self.s["c_struct"])
        cb = next(c for c in b["claims"] if c["claim_id"] == self.s["c_struct"])
        # v1 影响时点：支持；v2 影响时点：反驳（历史重放，不受后续撤回影响）
        self.assertEqual(ca["status_at_batch_cutoff"], STATUS_SUPPORTED)
        self.assertEqual(cb["status_at_batch_cutoff"], STATUS_CONTRADICTED)
        self.assertEqual(a["claim_status_counts_at_cutoff"][STATUS_SUPPORTED], 1)
        self.assertEqual(b["claim_status_counts_at_cutoff"][STATUS_CONTRADICTED], 1)
        changed = cmp["delta"]["status_changed_between_cutoffs"]
        self.assertTrue(any(c["claim_id"] == self.s["c_struct"] for c in changed))
        self.assertEqual(ca["evidence_count_at_cutoff"], 1)
        self.assertGreaterEqual(cb["evidence_count_at_cutoff"], 2)

    # ---- 9. 审批与公开脱敏 -----------------------------------------------

    def test_11_public_requires_internal_then_public_approval(self):
        with self.assertRaises(DomainError) as e:
            self.r.decide_snapshot(self.s["snap1"], "public", "approved", actor="pi")
        self.assertIn("内部批准", str(e.exception))
        self.r.decide_snapshot(self.s["snap1"], "internal", "approved",
                               note="内部评审通过", actor="pi")
        result = self.r.decide_snapshot(self.s["snap1"], "public", "approved",
                                        actor="pi")
        # 审批不可重复/改写
        with self.assertRaises(DomainError) as e:
            self.r.decide_snapshot(self.s["snap1"], "public", "rejected", actor="pi")
        self.assertEqual(e.exception.status, 409)
        self.s["approval1"] = result

    def test_12_public_projection_redacts_sensitive_and_unsupported(self):
        pub = self.r.public_snapshot(self.s["snap1"])
        self.assertEqual(pub["snapshot_id"], self.s["snap1"])
        # 敏感靶点章节结构化遮蔽
        self.assertEqual(pub["content"]["target_details"],
                         {"redacted": True, "reason": "sensitive_unpublished_target"})
        self.assertIn("abstract", pub["content"])  # 非敏感章节保留
        by_claim = {c["claim_id"]: c for c in pub["citations"]}
        target_cit = by_claim[self.s["c_target"]]
        self.assertTrue(target_cit["redacted"])
        self.assertEqual(target_cit["reason"], "sensitive_unpublished_target")
        self.assertNotIn("exact_quote", target_cit)
        # 结构主张当前 contradicted：公开版按批准时点遮蔽
        struct_cit = by_claim[self.s["c_struct"]]
        self.assertTrue(struct_cit["redacted"])
        self.assertEqual(struct_cit["reason"], "claim_contradicted_at_release")
        # 已重置为 unverified 的连接主张同样遮蔽
        junction_cit = by_claim[self.s["c_junction"]]
        self.assertTrue(junction_cit["redacted"])

        # 另起一稿：自噬主张（unverified）被引用 → 遮蔽原因不同
        paper2 = self.r.create_paper("TM184C 自噬预印本", "chen", actor="chen")
        snap2 = self.r.submit_snapshot(
            paper2,
            {"abstract": "自噬标志物初步工作。", "sections": {}},
            citations=[{"claim_id": self.s["c_autophagy"],
                        "exact_quote": "可能调节自噬流。"}],
            actor="chen")
        self.r.decide_snapshot(snap2, "internal", "approved", actor="pi")
        self.r.decide_snapshot(snap2, "public", "approved", actor="pi")
        pub2 = self.r.public_snapshot(snap2)
        cit = pub2["citations"][0]
        self.assertTrue(cit["redacted"])
        self.assertEqual(cit["reason"], "claim_unverified_at_release")

    # ---- 10. 审计哈希链 --------------------------------------------------

    def test_13_audit_chain_intact(self):
        report = self.s["store"].verify_audit_chain()
        self.assertTrue(report["ok"], report)
        self.assertGreater(report["events"], 30)
        actions = {e["action"] for e in self.r.audit_tail(limit=500)}
        self.assertIn("retract", actions)
        self.assertIn("submit_snapshot", actions)
        self.assertIn("decide_snapshot", actions)


class HttpBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = build_scenario()
        cls.r = cls.s["r"]
        # HTTP 场景需要自己的已批准快照（与叙事测试类相互独立）
        paper = cls.r.create_paper("TM184C 受体样功能研究（投稿稿）",
                                   "课题组全体", actor="lu")
        cls.s["snap1"] = cls.r.submit_snapshot(
            paper,
            {"abstract": "本稿报告 TM184C 的结构线索与细胞连接表型。",
             "target_details": {"binding_pocket": "未公开的结合口袋坐标",
                                "confidential": True}},
            citations=[
                {"claim_id": cls.s["c_struct"], "exact_quote": "结构相似（支持）。"},
                {"claim_id": cls.s["c_junction"], "exact_quote": "参与连接调节（支持）。"},
                {"claim_id": cls.s["c_target"], "exact_quote": "候选靶点（外推）。"},
            ],
            actor="lu")
        cls.r.decide_snapshot(cls.s["snap1"], "internal", "approved", actor="pi")
        cls.r.decide_snapshot(cls.s["snap1"], "public", "approved", actor="pi")
        handler = create_handler(cls.r, health=health_payload)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _req(self, method, path, body=None, role=None, actor=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if role:
            headers["X-Role"] = role
        if actor:
            headers["X-Actor"] = actor
        req = Request(self.base + path, data=data, headers=headers, method=method)
        return urlopen(req, timeout=3)

    def _req_error(self, method, path, **kw):
        with self.assertRaises(HTTPError) as cm:
            self._req(method, path, **kw)
        cm.exception.code  # noqa: B018 - ensure attribute readable
        return cm.exception

    def test_health_unchanged(self):
        with self._req("GET", "/health") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.load(resp), health_payload())

    def test_internal_route_requires_internal_role(self):
        err = self._req_error("GET", "/api/claims")
        self.assertEqual(err.code, 403)
        err.close()
        # reviewer 只读：不能录入
        err = self._req_error("POST", "/api/proteins",
                              body={"canonical_name": "X", "species": HUMAN},
                              role="reviewer", actor="rev")
        self.assertEqual(err.code, 403)
        err.close()

    def test_write_role_and_pi_only_routes(self):
        with self._req("POST", "/api/proteins",
                       body={"canonical_name": "临时蛋白Z", "species": HUMAN,
                             "aliases": ["ZED"]},
                       role="researcher", actor="lu") as resp:
            self.assertEqual(resp.status, 201)

        # researcher 不能撤回
        err = self._req_error(
            "POST", "/api/retractions",
            body={"reason": "x", "material_id": self.s["cell_line"]},
            role="researcher", actor="lu")
        self.assertEqual(err.code, 403)
        err.close()
        # researcher 不能做批次比较
        err = self._req_error("GET",
                              f"/api/batches/compare?a={self.s['b1']}&b={self.s['b2']}",
                              role="researcher")
        self.assertEqual(err.code, 403)
        err.close()
        with self._req("GET",
                       f"/api/batches/compare?a={self.s['b1']}&b={self.s['b2']}",
                       role="pi") as resp:
            self.assertEqual(resp.status, 200)
            data = json.load(resp)
            self.assertIn("delta", data)

    def test_reviewer_can_trace_claim(self):
        with self._req("GET", f"/api/claims/{self.s['c_target']}", role="reviewer") as resp:
            trace = json.load(resp)
        self.assertEqual(trace["cross_species_inferences"][0]["species_to"], HUMAN)

    def test_public_endpoint_release_rules(self):
        # 未批准的快照：公开接口 404，且不带内部角色无法分辨存在性
        snap_other = self.r.submit_snapshot(
            self.r.create_paper("未批准稿", "x", actor="lu"),
            {"abstract": "草稿"}, actor="lu")
        err = self._req_error("GET", f"/api/public/snapshots/{snap_other}")
        self.assertEqual(err.code, 404)
        err.close()
        # 已批准版本无需角色即可读
        with self._req("GET", f"/api/public/snapshots/{self.s['snap1']}") as resp:
            self.assertEqual(resp.status, 200)
            pub = json.load(resp)
        self.assertEqual(pub["content"]["target_details"]["redacted"], True)
        # 公开角色不能借内部接口读取未脱敏快照
        err = self._req_error("GET", f"/api/snapshots/{self.s['snap1']}", role="public")
        self.assertEqual(err.code, 403)
        err.close()


if __name__ == "__main__":
    unittest.main()
