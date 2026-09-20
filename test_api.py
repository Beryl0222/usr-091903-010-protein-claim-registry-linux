"""HTTP 接口契约测试：命令入口、读路由、公开接口与错误码。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from registry import EventStore, Registry
from registry.api import make_handler
from registry.scenario import build_tm184c_scenario
from service import health_payload


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.registry = Registry(EventStore())
        build_tm184c_scenario(self.registry)
        handler = make_handler(self.registry, health_payload)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    # --- 工具 ----------------------------------------------------------------

    def get_json(self, path):
        with urlopen(f"{self.base}{path}", timeout=3) as response:
            return response.status, json.load(response)

    def command(self, name, args=None, actor=None):
        body = json.dumps({"command": name, "args": args or {}, "actor": actor},
                          ensure_ascii=False).encode("utf-8")
        request = Request(f"{self.base}/commands", data=body,
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=3) as response:
            return response.status, json.load(response)

    def post_raw(self, path, body):
        request = Request(f"{self.base}{path}", data=body,
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            urlopen(request, timeout=3)
        except HTTPError as error:
            return error.code, json.load(error)
        self.fail(f"{path} 应当返回错误")

    def get_error(self, path):
        try:
            urlopen(f"{self.base}{path}", timeout=3)
        except HTTPError as error:
            return error.code, json.load(error)
        self.fail(f"{path} 应当返回错误")

    # --- 读接口 --------------------------------------------------------------

    def test_health_contract_unchanged(self):
        status, body = self.get_json("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, health_payload())

    def test_list_claims_with_status_filter(self):
        _, rows = self.get_json("/claims?" + urlencode({"status": "extrapolated"}))
        self.assertEqual({c["id"] for c in rows}, {"C_STRUCT", "C_RESCUE"})

    def test_claim_trace_endpoint(self):
        _, trace = self.get_json("/claims/C_REC/trace")
        self.assertEqual(trace["claim"]["id"], "C_REC")
        self.assertTrue(trace["inference_chain"])
        self.assertEqual(trace["species_boundary"]["subject_species"], "human")

    def test_protein_view_includes_aliases_orthologs_and_claims(self):
        _, protein = self.get_json("/proteins/P_TM184C")
        self.assertIn("TMEM184C", protein["aliases"])
        self.assertEqual(len(protein["claims"]), 5)
        self.assertEqual(protein["orthologs"][0]["protein_id"], "P_YEAST")

    def test_experiment_view_keeps_materials_controls_observations(self):
        _, exp = self.get_json("/experiments/E_HUMAN")
        ids = lambda rows: {row["id"] for row in rows}
        self.assertIn("SAM_KO", ids(exp["samples"]))
        self.assertIn("CTL_WT", ids(exp["controls"]))
        self.assertIn("O_JX", ids(exp["observations"]))
        self.assertIn("A_JX", ids(exp["analyses"]))
        a_jx = next(a for a in exp["analyses"] if a["id"] == "A_JX")
        self.assertEqual(a_jx["p_value"], 0.003)

    def test_batch_impact_and_compare(self):
        _, impact = self.get_json("/batches/B_V3/impact")
        self.assertEqual(impact["batch"]["version"], "afdb_v3")
        self.command("register_computation_batch",
                     {"batch_id": "B_V4", "source": "AlphaFold DB", "version": "afdb_v4"})
        _, compared = self.get_json("/batches/compare?a=B_V3&b=B_V4")
        self.assertEqual(compared["a"]["version"], "afdb_v3")
        self.assertEqual(compared["b"]["version"], "afdb_v4")

    def test_events_log_is_exposed_for_audit(self):
        _, events = self.get_json("/events")
        self.assertGreater(len(events), 10)
        self.assertTrue(all("payload" in e and "seq" in e for e in events))

    # --- 写命令 --------------------------------------------------------------

    def test_command_append_and_changes_projection(self):
        status, result = self.command(
            "raise_claim",
            {
                "claim_id": "C_NEW", "protein_id": "P_TM184C",
                "claim_type": "functional_involvement",
                "subject_species": "human",
                "assertion": "通过 API 新建的主张",
            },
            actor="reviewer-li",
        )
        self.assertEqual(status, 201)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["event"]["actor"], "reviewer-li")
        _, claim = self.get_json("/claims/C_NEW")
        self.assertEqual(claim["status"], "unvalidated")

    def test_command_validation_error_is_400(self):
        code, payload = self.post_raw("/commands", json.dumps(
            {"command": "raise_claim", "args": {}}).encode())
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"], "validation_error")

    def test_unknown_command_is_400(self):
        code, payload = self.post_raw("/commands", b'{"command": "drop_tables"}')
        self.assertEqual(code, 400)
        self.assertIn("drop_tables", payload["detail"])

    def test_conflict_is_409_and_not_found_is_404(self):
        code, _ = self.get_error("/claims/NO_SUCH_CLAIM")
        self.assertEqual(code, 404)
        # 重复标识 -> 409
        code, _ = self.post_raw("/commands", json.dumps({
            "command": "register_protein",
            "args": {"protein_id": "P_TM184C", "canonical_name": "dup",
                     "organism": "human"},
        }).encode())
        self.assertEqual(code, 409)

    # --- 撤回级联通过 API 可见 ------------------------------------------------

    def test_withdrawal_via_api_cascades(self):
        status, result = self.command(
            "withdraw_sample",
            {"sample_id": "SAM_YHV", "reason": "菌株污染"},
            actor="qa",
        )
        self.assertEqual(status, 201)
        self.assertEqual(
            set(result["affected_claims"]),
            {"C_RESCUE", "C_REC", "C_TARGET"},
        )
        self.assertEqual(result["flagged"], 3)
        self.assertIn(
            "菌株污染",
            self.registry.get_claim("C_RESCUE")["review_flags"][0]["reason"],
        )
        _, rescue = self.get_json("/claims/C_RESCUE")
        self.assertEqual(rescue["status"], "needs_review")
        _, receptor = self.get_json("/claims/C_REC")
        self.assertEqual(receptor["status"], "needs_review")
        _, target = self.get_json("/claims/C_TARGET")
        self.assertEqual(target["status"], "needs_review")
        # 不相关的结构主张与人源功能主张不受影响
        _, struct = self.get_json("/claims/C_STRUCT")
        self.assertEqual(struct["status"], "extrapolated")
        _, func = self.get_json("/claims/C_FUNC")
        self.assertEqual(func["status"], "supported")

    def test_resolve_review_via_api_recomputes_status(self):
        self.command("withdraw_analysis",
                     {"analysis_id": "A_YRES", "reason": "模型误用"})
        _, before = self.get_json("/claims/C_RESCUE")
        self.assertEqual(before["status"], "needs_review")
        self.command("resolve_review",
                     {"claim_id": "C_RESCUE", "note": "确认分析失效"}, actor="qa")
        _, after = self.get_json("/claims/C_RESCUE")
        self.assertEqual(after["status"], "unvalidated")

    # --- 论文快照与公开接口 ---------------------------------------------------

    def test_paper_freeze_and_public_release_flow(self):
        self.command("submit_paper", {"paper_id": "PAPER_A"})
        # 未批准 -> 公开接口 404，内部仍可读
        code, _ = self.get_error("/public/papers/PAPER_A")
        self.assertEqual(code, 404)
        self.command("decide_release",
                     {"paper_id": "PAPER_A", "decision": "approved",
                      "note": "同意公开"}, actor="director")
        _, public = self.get_json("/public/papers/PAPER_A")
        self.assertEqual(len(public["citations"]), 4)
        _, listing = self.get_json("/public/papers")
        self.assertIn("PAPER_A", [p["paper_id"] for p in listing])
        # 冻结的是提交时刻状态：ref-2（C_FUNC）为 supported
        ref2 = next(c for c in public["citations"] if c["ref_key"] == "ref-2")
        self.assertEqual(ref2["status_at_snapshot"], "supported")
        # 酵母救援引用标注为无同物种直接证据
        ref3 = next(c for c in public["citations"] if c["ref_key"] == "ref-3")
        self.assertFalse(ref3["direct_evidence"])

    def test_snapshot_survives_later_withdrawal_but_current_view_flags_change(self):
        self.command("submit_paper", {"paper_id": "PAPER_A"})
        self.command("decide_release",
                     {"paper_id": "PAPER_A", "decision": "approved"})
        self.command("withdraw_sample",
                     {"sample_id": "SAM_KO", "reason": "身份核查失败"})
        # 公开快照不变
        _, public = self.get_json("/public/papers/PAPER_A")
        ref2 = next(c for c in public["citations"] if c["ref_key"] == "ref-2")
        self.assertEqual(ref2["status_at_snapshot"], "supported")
        # 内部当前视图标出分歧
        _, paper = self.get_json("/papers/PAPER_A")
        self.assertTrue(paper["citations"][1]["status_changed_since_citation"])
        _, func = self.get_json("/claims/C_FUNC")
        self.assertEqual(func["status"], "needs_review")

    def test_public_endpoint_is_read_only(self):
        code, _ = self.post_raw("/public/papers", b'{"command": "raise_claim"}')
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
