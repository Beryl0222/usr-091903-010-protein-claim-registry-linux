# 蛋白发现主张登记

科学主张登记后端：把**蛋白别名与物种、结构预测版本与计算批次、实验材料 / 样本 / 对照 / 观察 / 统计分析**连接到**粒度明确的主张**，并把“结构相似 / 参与调节 / 疾病靶点”区分为不同的主张类型与置信状态——而不是把它们写成同一种确定程度。

## 解决的问题

- **确定程度区分**：主张只有五个互斥状态
  `supported`（支持）/ `refuted`（反驳）/ `unvalidated`（尚未验证）/
  `extrapolated`（仅可外推）/ `needs_review`（待复核）。
  纯计算（两亿结构的 AI 筛选）与跨物种（酵母救援）证据至多产生 `extrapolated`；
  结构预测即便被误记为 supports 也会自动归一为 extrapolation。
- **历史不可变**：所有写操作只追加事件（event sourcing，JSONL 落盘）。
  论文**提交时**把每条引用连同当时的主张状态冻结为快照；之后新证据只改变当前投影，
  快照与当时引用永不重写，系统同时显式标出“当前状态与引用时已不同”。
- **撤回不删除**：样本或统计分析被撤回时，原始数据保留并打标记，
  所有直接或沿依赖链间接依赖它的主张进入 `needs_review`，
  经人工复核（`resolve_review`）后才按剩余有效证据重新派生状态。
- **评议追溯**：`/claims/{id}/trace` 沿一条结论给出同物种直接证据、
  跨物种推断的物种边界、完整推断链与状态时间线。
- **批次比较**：`/batches/{id}/impact` 与 `/batches/compare` 让负责人比较
  不同计算批次（如 afdb_v3 / afdb_v4）影响了哪些预测与下游主张。
- **公开接口受控**：`/public/*` 只读，只释放**已审批**论文在提交时的冻结快照；
  未公开蛋白的疾病靶点主张整段脱敏（`redacted`）。

## 领域模型

| 实体 | 关键字段 |
| --- | --- |
| protein | 规范名、物种、tax_id、别名 aliases、同源蛋白 orthologs、是否可公开 |
| computation_batch | 数据源、**结构预测版本** version、搜索空间 |
| structure_prediction | 蛋白 × 批次，target_pdb、TM-score、RMSD |
| experiment | 实验体系物种、assay、材料 materials |
| sample / control | 细胞系 / 菌株 / 组织，基因型，对照与样本配对 |
| observation | 样本上的指标、值、单位、所用对照、条件 |
| analysis | 统计方法、引用的观察与样本、p 值、效应量、CI、模型说明 |
| claim | 主张类型、**主体物种**、一句话断言、证据连接、主张间依赖 |
| paper | 草稿登记引用；提交后冻结快照；审批后才可公开 |

主张类型（粒度即协议）：
`structural_similarity`、`functional_involvement`、`ortholog_rescue`、
`receptor_identity`、`disease_target`。

## 运行

```bash
python3 service.py --check            # 配置与装配自检
python3 service.py --port 8000        # 内存事件日志（联调）
python3 service.py --store data/events.jsonl --port 8000   # 持久化，重启重放
```

种入 TM184C 演示场景（AI 筛选 → 细胞连接 / 自噬实验 → 酵母同源救援 → 论文快照）：

```bash
python3 seed.py --store data/demo.jsonl --approve
python3 service.py --store data/demo.jsonl --port 8000
```

## HTTP 接口

所有写操作统一为 `POST /commands`，`command` 即领域方法名，`args` 为其参数：

```bash
curl -s localhost:8000/commands -H 'Content-Type: application/json' -d '{
  "command": "withdraw_sample",
  "actor": "qa-zhang",
  "args": {"sample_id": "SAM_KO", "reason": "细胞系 STR 身份核查失败"}
}'
```

| 方法/路径 | 用途 |
| --- | --- |
| GET `/claims` | 主张列表，可按 `protein_id`、`status`、`claim_type` 过滤 |
| GET `/claims/{id}` | 单条主张当前投影（含有效/失效证据标记） |
| GET `/claims/{id}/trace` | 评议追溯：直接证据、跨物种边界、推断链、时间线 |
| GET `/proteins/{id}` | 别名、同源、该蛋白全部主张 |
| GET `/experiments/{id}` | 材料、样本、对照、观察、统计分析 |
| GET `/batches/{id}/impact` | 一个计算批次的预测与下游主张影响 |
| GET `/batches/compare?a=..&b=..` | 两个计算批次影响对比 |
| GET `/papers` `/papers/{id}` | 论文当前视图（含快照分歧标记） |
| GET `/events` | 只追加事件审计日志 |
| GET `/public/papers` `/public/papers/{id}` | **公开**：已审批冻结快照，靶点脱敏 |

错误码：参数不合法 400、引用不存在 404、与不可变历史冲突（重复 id、改冻结快照、重复撤回）409。

## 测试

```bash
npm test        # 等价于 python3 -m unittest service_contract test_domain test_api
```

47 个测试覆盖：五类置信状态派生、跨物种反证不降级、纯计算证据归一、
撤回级联与人工复核、新证据恢复支持、快照冻结不被撤回重写、公开脱敏、
批次影响比较，以及全部 HTTP 契约。
