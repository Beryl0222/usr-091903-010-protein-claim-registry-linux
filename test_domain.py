"""领域规则测试：状态派生、撤回级联、快照冻结、公开脱敏。"""

import unittest

from registry import EventStore, Registry
from registry.scenario import build_tm184c_scenario
from registry.store import ConflictError, NotFoundError, ValidationError
from registry.vocab import (
    CLAIM_DISEASE_TARGET,
    CLAIM_FUNCTIONAL_INVOLVEMENT,
    CLAIM_RECEPTOR_IDENTITY,
    CLAIM_STRUCTURAL_SIMILARITY,
    EXTRAPOLATED,
    NEEDS_REVIEW,
    REFUTED,
    RELATION_CONTRADICTS,
    RELATION_SUPPORTS,
    SUPPORTED,
    UNVALIDATED,
)


def new_registry():
    return Registry(EventStore())


class StatusDerivationTest(unittest.TestCase):
    def setUp(self):
        self.r = new_registry()
        build_tm184c_scenario(self.r)

    def test_three_claim_types_get_distinct_certainty(self):
        # 结构相似：纯计算 -> 仅可外推
        self.assertEqual(self.r.get_claim("C_STRUCT")["status"], EXTRAPOLATED)
        # 参与调节：同物种实验支持 -> 支持
        self.assertEqual(self.r.get_claim("C_FUNC")["status"], SUPPORTED)
        # 酵母救援：跨物种 -> 仅可外推（不能写成“已证实”）
        self.assertEqual(self.r.get_claim("C_RESCUE")["status"], EXTRAPOLATED)
        # 受体身份：有同物种直接实验支持 -> 支持
        self.assertEqual(self.r.get_claim("C_REC")["status"], SUPPORTED)
        # 疾病靶点：只有依赖链，没有任何直接证据 -> 尚未验证
        self.assertEqual(self.r.get_claim("C_TARGET")["status"], UNVALIDATED)

    def test_structural_evidence_can_not_directly_support(self):
        # 即便调用方误传 supports，纯计算证据也被归为外推
        self.r.register_protein("P2", "X2", "human")
        self.r.register_computation_batch("B2", "AFDB", "v1")
        self.r.record_structure_prediction("S2", "P2", "B2", tm_score=0.7)
        self.r.raise_claim(
            "C2", "P2", CLAIM_RECEPTOR_IDENTITY, "human", "X2 是受体",
            computation_batch_id="B2",
        )
        self.r.add_evidence("C2", "structure_prediction", "S2", RELATION_SUPPORTS)
        link = self.r.get_claim("C2")["evidence"][0]
        self.assertEqual(link["relation"], "extrapolation")
        self.assertEqual(self.r.get_claim("C2")["status"], EXTRAPOLATED)

    def test_direct_contradiction_refutes(self):
        # 新增一条同物种反驳分析（独立样本）
        self.r.register_sample("E_HUMAN", "SAM_KO2", "cell_line",
                               description="独立重复敲除", genotype="TM184C-/-")
        self.r.record_observation("O_NOJX", "E_HUMAN", "SAM_KO2",
                                  "junction_integrity", 0.01, unit="z",
                                  controls=["CTL_WT"])
        self.r.record_statistical_analysis(
            "A_NOJX", "E_HUMAN", "welch_t_test",
            observation_ids=["O_NOJX"], sample_ids=["SAM_KO2"],
            p_value=0.9, effect_size=0.02,
        )
        self.r.add_evidence("C_FUNC", "analysis", "A_NOJX", RELATION_CONTRADICTS)
        self.assertEqual(self.r.get_claim("C_FUNC")["status"], REFUTED)

    def test_cross_species_contradiction_does_not_refute_human_claim(self):
        # 酵母里的阴性结果不能直接反驳人源主张，只算外推域
        self.r.register_sample("E_YEAST", "SAM_YNEG", "strain", genotype="yor1Δ-neg")
        self.r.record_observation("O_YNEG", "E_YEAST", "SAM_YNEG",
                                  "fraction_surviving", 0.05, controls=["CTL_YVEC"])
        self.r.record_statistical_analysis(
            "A_YNEG", "E_YEAST", "fishers_exact_test",
            observation_ids=["O_YNEG"], sample_ids=["SAM_YNEG"], p_value=0.8,
        )
        self.r.add_evidence("C_FUNC", "analysis", "A_YNEG",
                            RELATION_CONTRADICTS, source_species="S. cerevisiae")
        # 人源直接支持仍在 -> supported，跨物种反证不降级
        self.assertEqual(self.r.get_claim("C_FUNC")["status"], SUPPORTED)


class WithdrawalCascadeTest(unittest.TestCase):
    def setUp(self):
        self.r = new_registry()
        build_tm184c_scenario(self.r)

    def test_sample_withdrawal_flags_dependent_claims_only(self):
        result = self.r.withdraw_sample("SAM_KO", "细胞系 STR 鉴定不符")
        affected = set(result["affected_claims"])
        # C_FUNC（直接依赖 A_JX/A_LC3）、C_REC（依赖链+A_JX）、
        # C_TARGET（依赖 C_REC）全部进入待复核
        self.assertEqual(affected, {"C_FUNC", "C_REC", "C_TARGET"})
        for claim_id in ("C_FUNC", "C_REC", "C_TARGET"):
            self.assertEqual(self.r.get_claim(claim_id)["status"], NEEDS_REVIEW)
        # 结构与酵母证据不涉及该样本，保持原状
        self.assertEqual(self.r.get_claim("C_STRUCT")["status"], EXTRAPOLATED)
        self.assertEqual(self.r.get_claim("C_RESCUE")["status"], EXTRAPOLATED)

    def test_withdrawn_sample_data_is_not_deleted(self):
        self.r.withdraw_sample("SAM_KO", "原因")
        # 原始样本、观察、分析仍可读取，仅带 withdrawn 标记
        exp = self.r.experiments["E_HUMAN"]
        self.assertTrue(exp["samples"]["SAM_KO"]["withdrawn"])
        self.assertIn("O_JX", exp["observations"])
        self.assertIn("A_JX", exp["analyses"])

    def test_redundant_withdrawal_is_rejected(self):
        self.r.withdraw_sample("SAM_KO", "第一次")
        with self.assertRaises(ConflictError):
            self.r.withdraw_sample("SAM_KO", "重复撤回")

    def test_resolve_review_recomputes_from_remaining_evidence(self):
        self.r.withdraw_sample("SAM_KO", "原因")
        # SAM_KO 的两条分析失效，C_FUNC 无有效证据 -> 尚未验证
        self.r.resolve_review("C_FUNC", note="失效证据移除后重评")
        self.assertEqual(self.r.get_claim("C_FUNC")["status"], UNVALIDATED)
        # 复核只能人工清除；C_REC 仍在待复核
        self.assertEqual(self.r.get_claim("C_REC")["status"], NEEDS_REVIEW)

    def test_resolve_without_flag_is_conflict(self):
        with self.assertRaises(ConflictError):
            self.r.resolve_review("C_STRUCT", note="并未待复核")

    def test_analysis_withdrawal_propagates_through_chain(self):
        self.r.withdraw_analysis("A_YRES", "统计模型误用")
        # C_RESCUE 直接受影响；C_REC 依赖它；C_TARGET 再依赖 C_REC
        self.assertEqual(self.r.get_claim("C_RESCUE")["status"], NEEDS_REVIEW)
        self.assertEqual(self.r.get_claim("C_REC")["status"], NEEDS_REVIEW)
        self.assertEqual(self.r.get_claim("C_TARGET")["status"], NEEDS_REVIEW)
        # 复核 C_RESCUE：唯一证据被撤回 -> 尚未验证
        self.r.resolve_review("C_RESCUE", note="分析撤回")
        self.assertEqual(self.r.get_claim("C_RESCUE")["status"], UNVALIDATED)

    def test_new_evidence_after_withdrawal_can_restore_support(self):
        self.r.withdraw_sample("SAM_KO", "原因")
        self.r.resolve_review("C_FUNC", note="待补证据")
        self.assertEqual(self.r.get_claim("C_FUNC")["status"], UNVALIDATED)
        # 用全新独立样本补上同物种支持
        self.r.register_sample("E_HUMAN", "SAM_KO3", "cell_line", genotype="KO#3")
        self.r.record_observation("O_JX3", "E_HUMAN", "SAM_KO3",
                                  "junction_integrity", -0.4, controls=["CTL_WT"])
        self.r.record_statistical_analysis(
            "A_JX3", "E_HUMAN", "welch_t_test",
            observation_ids=["O_JX3"], sample_ids=["SAM_KO3"], p_value=0.002,
        )
        self.r.add_evidence("C_FUNC", "analysis", "A_JX3", RELATION_SUPPORTS)
        self.assertEqual(self.r.get_claim("C_FUNC")["status"], SUPPORTED)


class PaperSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.r = new_registry()
        build_tm184c_scenario(self.r)

    def test_submitted_snapshot_freezes_status_at_that_time(self):
        before = {cid: self.r.get_claim(cid)["status"]
                  for cid in ("C_STRUCT", "C_FUNC", "C_RESCUE", "C_REC")}
        self.r.submit_paper("PAPER_A")
        snapshot = self.r.frozen_snapshots["PAPER_A"]
        frozen = {c["claim_id"]: c["status_at_snapshot"]
                  for c in snapshot["citations"]}
        self.assertEqual(frozen, before)

    def test_later_withdrawal_does_not_rewrite_snapshot(self):
        self.r.submit_paper("PAPER_A")
        snapshot_seq_before = self.r.frozen_snapshots["PAPER_A"]["snapshot_seq"]
        self.r.withdraw_sample("SAM_KO", "事后撤回")
        snapshot = self.r.frozen_snapshots["PAPER_A"]
        self.assertEqual(snapshot["snapshot_seq"], snapshot_seq_before)
        # 冻结快照里 C_FUNC 仍是当时的 supported
        frozen_func = next(c for c in snapshot["citations"] if c["claim_id"] == "C_FUNC")
        self.assertEqual(frozen_func["status_at_snapshot"], SUPPORTED)
        # 当前投影已变为待复核，并在读模型上显式标出分歧
        paper = self.r.get_paper("PAPER_A")
        self.assertEqual(self.r.get_claim("C_FUNC")["status"], NEEDS_REVIEW)
        self.assertTrue(paper["citations"][1]["status_changed_since_citation"])

    def test_frozen_paper_rejects_citation_edits(self):
        self.r.submit_paper("PAPER_A")
        with self.assertRaises(ConflictError):
            self.r.add_paper_citation("PAPER_A", "ref-9", "C_TARGET")
        with self.assertRaises(ConflictError):
            self.r.submit_paper("PAPER_A")

    def test_paper_requires_at_least_one_citation(self):
        self.r.create_paper("PAPER_EMPTY", "空稿")
        with self.assertRaises(ValidationError):
            self.r.submit_paper("PAPER_EMPTY")


class PublicReleaseTest(unittest.TestCase):
    def setUp(self):
        self.r = new_registry()
        build_tm184c_scenario(self.r)
        self.r.submit_paper("PAPER_A")

    def test_unapproved_paper_is_not_public(self):
        self.assertEqual(self.r.public_papers(), [])
        with self.assertRaises(NotFoundError):
            self.r.public_paper("PAPER_A")

    def test_approved_paper_is_released_with_frozen_view(self):
        self.r.decide_release("PAPER_A", "approved", actor="director")
        public = self.r.public_paper("PAPER_A")
        self.assertEqual(public["paper_id"], "PAPER_A")
        self.assertEqual(len(public["citations"]), 4)
        # 公开视图标注每条引用是否有同物种直接证据
        ref2 = next(c for c in public["citations"] if c["ref_key"] == "ref-2")
        self.assertTrue(ref2["direct_evidence"])
        ref3 = next(c for c in public["citations"] if c["ref_key"] == "ref-3")
        self.assertFalse(ref3["direct_evidence"])
        self.assertIn("experimental", ref2["evidence_domains"])
        # ref-1 只有纯计算证据：不算同物种直接证据，只能外推
        ref1 = next(c for c in public["citations"] if c["ref_key"] == "ref-1")
        self.assertFalse(ref1["direct_evidence"])
        self.assertEqual(ref1["evidence_domains"], ["computational"])

    def test_unpublished_target_detail_is_redacted(self):
        # 未公开靶点蛋白 + 疾病靶点主张，即便论文获批也必须脱敏
        self.r.register_protein("P_SECRET", "SecretTarget", "human", target_public=False)
        self.r.raise_claim("C_SECRET", "P_SECRET", CLAIM_DISEASE_TARGET,
                           "human", "某未公开疾病靶点细节")
        self.r.create_paper("PAPER_SECRET", "内部转化稿")
        self.r.add_paper_citation("PAPER_SECRET", "s-1", "C_SECRET")
        self.r.submit_paper("PAPER_SECRET")
        self.r.decide_release("PAPER_SECRET", "approved")
        cite = self.r.public_paper("PAPER_SECRET")["citations"][0]
        self.assertTrue(cite["redacted"])
        self.assertNotIn("assertion", cite)
        # 已公开蛋白的非靶点主张不受影响
        self.r.decide_release("PAPER_A", "approved")
        ref1 = self.r.public_paper("PAPER_A")["citations"][0]
        self.assertFalse(ref1.get("redacted", False))


class TraceAndBatchTest(unittest.TestCase):
    def setUp(self):
        self.r = new_registry()
        build_tm184c_scenario(self.r)

    def test_trace_separates_direct_and_cross_species_evidence(self):
        trace = self.r.claim_trace("C_RESCUE")
        self.assertEqual(trace["cross_species_evidence"][0]["source_species"],
                         "S. cerevisiae")
        self.assertEqual(trace["direct_evidence"], [])
        self.assertEqual(
            trace["species_boundary"]["subject_species"], "human",
        )
        self.assertEqual(
            trace["species_boundary"]["extrapolated_from_species"],
            ["S. cerevisiae"],
        )

    def test_trace_inference_chain_is_transitive(self):
        chain = self.r.claim_trace("C_TARGET")["inference_chain"]
        pairs = {(edge["from"], edge["to"]) for edge in chain}
        self.assertIn(("C_TARGET", "C_REC"), pairs)
        self.assertIn(("C_REC", "C_STRUCT"), pairs)
        self.assertIn(("C_REC", "C_RESCUE"), pairs)

    def test_timeline_records_status_transitions(self):
        self.r.withdraw_sample("SAM_KO", "原因")
        self.r.resolve_review("C_FUNC", note="重评")
        statuses = [e["status_after"] for e in
                    self.r.claim_trace("C_FUNC")["timeline"]]
        self.assertEqual(statuses[0], SUPPORTED)       # 第一条证据 A_JX
        self.assertIn(NEEDS_REVIEW, statuses)
        self.assertEqual(statuses[-1], UNVALIDATED)

    def test_batch_impact_attributes_claims_to_computation_run(self):
        impact = self.r.batch_impact("B_V3")
        self.assertEqual(impact["batch"]["version"], "afdb_v3")
        self.assertEqual([p["id"] for p in impact["predictions"]], ["S_V3"])
        self.assertIn("C_STRUCT", [c["id"] for c in impact["claims"]])
        self.assertEqual(impact["claim_status_counts"][EXTRAPOLATED], 1)

    def test_compare_batches(self):
        self.r.register_computation_batch("B_V4", "AlphaFold DB", "afdb_v4")
        self.r.record_structure_prediction("S_V4", "P_TM184C", "B_V4", tm_score=0.91)
        self.r.raise_claim("C_STRUCT_V4", "P_TM184C", CLAIM_STRUCTURAL_SIMILARITY,
                           "human", "v4 复算的结构相似性",
                           computation_batch_id="B_V4")
        self.r.add_evidence("C_STRUCT_V4", "structure_prediction", "S_V4",
                            RELATION_SUPPORTS)
        comparison = self.r.compare_batches("B_V3", "B_V4")
        self.assertEqual(comparison["a"]["version"], "afdb_v3")
        self.assertEqual(comparison["b"]["version"], "afdb_v4")
        self.assertEqual(comparison["b"]["prediction_count"], 1)


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.r = new_registry()

    def test_duplicate_ids_rejected(self):
        self.r.register_protein("P1", "X", "human")
        with self.assertRaises(ConflictError):
            self.r.register_protein("P1", "Y", "mouse")

    def test_unknown_references_rejected(self):
        with self.assertRaises(NotFoundError):
            self.r.raise_claim("C1", "NOPE", CLAIM_FUNCTIONAL_INVOLVEMENT,
                               "human", "x")

    def test_dependency_cycle_rejected(self):
        self.r.register_protein("P1", "X", "human")
        self.r.raise_claim("C1", "P1", CLAIM_RECEPTOR_IDENTITY, "human", "a")
        self.r.raise_claim("C2", "P1", CLAIM_RECEPTOR_IDENTITY, "human", "b")
        self.r.add_claim_dependency("C1", "C2")
        with self.assertRaises(ValidationError):
            self.r.add_claim_dependency("C2", "C1")

    def test_observation_must_belong_to_experiment_sample(self):
        self.r.register_protein("P1", "X", "human")
        self.r.create_experiment("E1", "t", "human")
        self.r.register_sample("E1", "S1", "cell_line")
        # 属于别的实验的样本不能用于本实验观察
        self.r.create_experiment("E2", "t2", "human")
        with self.assertRaises(ValidationError):
            self.r.record_observation("O1", "E2", "S1", "endpoint", 1)


if __name__ == "__main__":
    unittest.main()
