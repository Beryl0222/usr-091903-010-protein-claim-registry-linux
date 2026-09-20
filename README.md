# 蛋白发现主张登记 (protein-claim-registry)

科学主张登记后端：把**蛋白别名、物种、结构预测版本、实验材料、对照、观察结果、
统计分析**连接到粒度明确的主张，并以四值置信状态（支持 / 反驳 / 尚未验证 /
仅可外推）区分确定性；撤回不删除数据而传播为「待复核」；论文快照与当时引文
冻结不可改写；公开接口只释放已批准并脱敏的版本。

## 运行

```bash
python3 service.py --check          # 配置 + 存储 + 审计链自检
python3 service.py --port 8000      # 默认落盘 data/registry.db
python3 service.py --port 8000 --db :memory:
npm test                            # 运行全部契约测试（21 项）
```

仅依赖 Python 3 标准库与 SQLite，无需安装第三方包。

## 角色

请求头携带 `X-Role` 与 `X-Actor`（演示级边界）：

| 角色 | 能力 |
|---|---|
| `researcher` | 录入蛋白、批次、实验链、主张、证据，发起状态评估 |
| `curator`/`pi` | 撤回、待复核处置、审批快照；`pi` 独占批次影响比较 |
| `reviewer` | 只读：沿结论追踪直接证据与跨物种推断边界 |
| 无角色（public） | 仅 `/api/public/**` |

## 数据模型与规则

- **主张 `claims`**：`claim_type`（structural_similarity / regulatory_involvement /
  disease_target / functional / localization / identity）× `granularity`
  （molecular_structure / cellular / organism / cross_species …）× `species_scope`，
  敏感主张可标 `sensitive`。
- **置信状态**：`supported` / `contradicted` / `unverified` / `extrapolable`，
  撤回期间为 `needs_review`。状态迁移有证据守卫（如 supported 必须有未撤回的
  直接支持证据；有直接证据不得降为 extrapolable；仅待复核后可重置 unverified），
  每次迁移写入 `status_history`。
- **证据链 `evidence_links`**：`experimental` / `computational` 为直接证据；
  `inferential` 必须记录来源主张与 `species_from → species_to`，显式划出
  跨物种推断边界。
- **撤回 `retractions`**：材料 / 实验 / 观察 / 分析标记撤回后不删除任何行；
  依赖沿证据链与推断链传播，受影响主张进入 `needs_review`，依赖路径保存在
  `retraction_impacts`。
- **论文快照 `paper_snapshots`**：提交时冻结正文、引文与摘要（sha256 digest）。
  后续置信变化只在读取时并置当前状态，**绝不回写**快照或引文原文。
- **审批与公开发布**：先 `internal` 批准、再 `public` 批准；公开批准时生成
  **冻结的脱敏投影**——`sensitive` 主张引文整体遮蔽，发布时点为
  unverified / needs_review / contradicted 的引文同样遮蔽，正文中
  `confidential:true` 对象与 `target_details` 等敏感章节结构化遮蔽。
  未批准的快照在公开接口一律 404（不暴露存在性）。
- **审计**：所有写操作进入哈希链 `audit_events`（前链 + 载荷 sha256），
  `GET /api/audit` 可校验完整性。

## 主要接口

```
POST /api/proteins | /api/aliases
POST /api/batches | /api/predictions
POST /api/materials | /api/experiments
POST /api/experiments/{id}/materials        # role: sample|control|reagent
POST /api/observations | /api/analyses
POST /api/claims
POST /api/claims/{id}/evidence              # 可带 assess_status 与证据同事件生效
POST /api/claims/{id}/assess
GET  /api/claims?status=&claim_type=&protein=
GET  /api/claims/{id}                       # 评审追踪：直接证据/推断边界/历史/撤回影响
POST /api/retractions                       # material_id|experiment_id|observation_id|analysis_id
POST /api/papers  ; POST /api/papers/{id}/snapshots
GET  /api/snapshots/{id}
POST /api/snapshots/{id}/decision           # scope=internal|public, decision=approved|rejected
GET  /api/batches/compare?a=&b=             # pi；按批次影响时点重放历史状态
GET  /api/public/snapshots/{id}             # 仅已批准的脱敏冻结版本
GET  /api/audit
```

## 典型叙事（见 `registry_contract.py`）

TM184C 经两亿余结构的计算批次被识别为潜在受体；细胞连接实验支撑「参与调节」，
自噬实验未完成而保持「尚未验证」；酵母同源救援直接支持酵母内功能主张，
人类「疾病靶点」主张仅经跨物种推断成立（extrapolable，且标记敏感）。
重算批次反驳结构相似性后当前置信改变，但已投稿快照原文不变；污染样本与
撤回的救援分析使直接结论与人类靶点结论沿推断链依次进入待复核；公开版本中
敏感靶点章节与未获支持的引文均被遮蔽。
