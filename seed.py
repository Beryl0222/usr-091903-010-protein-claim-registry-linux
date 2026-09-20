#!/usr/bin/env python3
"""把 TM184C 场景种入一个只追加事件日志，便于本地联调与巡检演示。

用法：
    python3 seed.py --store data/demo.jsonl

种完后启动服务即可看到完整证据链：
    python3 service.py --store data/demo.jsonl --port 8000

脚本幂等：若目标日志里已有数据则拒绝覆盖（事件不可变原则）。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from registry import EventStore, Registry
from registry.scenario import build_tm184c_scenario


def main():
    parser = argparse.ArgumentParser(description="种入 TM184C 演示数据")
    parser.add_argument("--store", default="data/demo.jsonl",
                        help="事件日志路径（JSONL）")
    parser.add_argument("--approve", action="store_true",
                        help="同时审批 PAPER_A 进入公开接口")
    args = parser.parse_args()

    if os.path.exists(args.store) and os.path.getsize(args.store) > 0:
        sys.exit(f"拒绝写入：{args.store} 已有事件（事件日志不可覆盖）")

    os.makedirs(os.path.dirname(args.store) or ".", exist_ok=True)
    store = EventStore(args.store)
    registry = Registry(store)
    ids = build_tm184c_scenario(registry)

    # 演示一次“事后撤回不重写历史”的完整流程：
    # 提交论文 -> 审批 -> 撤回关键细胞系 -> 依赖结论进入待复核
    registry.submit_paper("PAPER_A", actor="corresponding-author")
    if args.approve:
        registry.decide_release("PAPER_A", "approved", note="同意公开", actor="director")

    print("=== 五类主张的置信状态（不同确定程度不再混写）===")
    for claim_id in ids["claims"]:
        claim = registry.get_claim(claim_id)
        print(f"  {claim_id:10s} {claim['claim_type']:24s} {claim['status']}")

    print("\n=== 同行评议追溯：C_RESCUE 的物种边界 ===")
    trace = registry.claim_trace("C_RESCUE")
    boundary = trace["species_boundary"]
    print(json.dumps(boundary, ensure_ascii=False, indent=2))

    print("\n=== 计算批次 B_V3 的影响 ===")
    impact = registry.batch_impact("B_V3")
    print(f"  版本 {impact['batch']['version']}，搜索空间：{impact['batch']['search_space']}")
    print(f"  下游主张：{[c['id'] for c in impact['claims']]}")
    print(f"  状态分布：{impact['claim_status_counts']}")

    print("\n=== 论文快照已冻结 ===")
    paper = registry.get_paper("PAPER_A")
    print(f"  论文状态：{paper['state']}，引用数：{len(paper['citations'])}")
    for citation in paper["citations"]:
        print(f"    {citation['ref_key']}: {citation['claim_id']} "
              f"引用时状态={citation['claim_status_at_citation']} "
              f"当前状态={citation['current_status']}")

    if args.approve:
        print("\n=== 公开接口（已脱敏；本场景蛋白均已公开）===")
        print(json.dumps(registry.public_papers(), ensure_ascii=False, indent=2))

    print(f"\n已写入 {store.seq()} 条事件 -> {args.store}")
    print("启动服务：python3 service.py --store", args.store, "--port 8000")
    store.close()


if __name__ == "__main__":
    main()
