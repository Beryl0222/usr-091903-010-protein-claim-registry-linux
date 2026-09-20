"""受控词表：主张类型、置信状态与证据关系。

这些取值是全系统的协议：不同论文草稿里“结构相似 / 参与调节 / 疾病靶点”
必须落在不同的主张类型上，置信状态也只允许这五个互斥取值，
从而避免把不同确定程度写成同一种断言。
"""

# --- 主张粒度（claim types） -------------------------------------------------

# 纯计算：预测结构与某已知受体折叠相似
CLAIM_STRUCTURAL_SIMILARITY = "structural_similarity"
# 直接实验：在某体系中参与/调节某生物学过程
CLAIM_FUNCTIONAL_INVOLVEMENT = "functional_involvement"
# 跨物种推断：在模式生物中由同源蛋白救援所支撑的功能保守性
CLAIM_ORTHOLOG_RESCUE = "ortholog_rescue"
# 机制：作为某信号/配体的受体发挥作用
CLAIM_RECEPTOR_IDENTITY = "receptor_identity"
# 转化层面：与某疾病相关并可作为干预靶点
CLAIM_DISEASE_TARGET = "disease_target"

CLAIM_TYPES = (
    CLAIM_STRUCTURAL_SIMILARITY,
    CLAIM_FUNCTIONAL_INVOLVEMENT,
    CLAIM_ORTHOLOG_RESCUE,
    CLAIM_RECEPTOR_IDENTITY,
    CLAIM_DISEASE_TARGET,
)

# 主张类型的默认证据域：标注该类断言原则上需要什么种类的依据
CLAIM_EVIDENCE_DOMAIN = {
    CLAIM_STRUCTURAL_SIMILARITY: "computational",
    CLAIM_FUNCTIONAL_INVOLVEMENT: "experimental",
    CLAIM_ORTHOLOG_RESCUE: "experimental_cross_species",
    CLAIM_RECEPTOR_IDENTITY: "experimental",
    CLAIM_DISEASE_TARGET: "translational",
}

# 仅靠该域的证据时，主张至多能被赋予的状态（防止跨域过度断言）
# computational 只能产生 extrapolated（仅可外推），不能直接产生 supported
DOMAIN_MAX_STATUS = {
    "computational": "extrapolated",
    "experimental_cross_species": "extrapolated",
    "experimental": "supported",
    "translational": "supported",
}

# --- 置信状态 ---------------------------------------------------------------

SUPPORTED = "supported"        # 支持：有同物种直接证据
REFUTED = "refuted"            # 反驳：有方向相反的有效证据
UNVALIDATED = "unvalidated"    # 尚未验证：只有提议/预测，无合格证据
EXTRAPOLATED = "extrapolated"  # 仅可外推：依据来自跨物种或纯计算
NEEDS_REVIEW = "needs_review"  # 待复核：所依赖的样本/分析被撤回

STATUSES = (SUPPORTED, REFUTED, UNVALIDATED, EXTRAPOLATED, NEEDS_REVIEW)

# --- 证据条目间 / 证据到主张的关系 -------------------------------------------

RELATION_SUPPORTS = "supports"
RELATION_CONTRADICTS = "contradicts"
RELATION_EXTRAPOLATION = "extrapolation"  # 跨物种/跨体系外推，携带推断边界
RELATION_DEPENDS_ON = "depends_on"        # 主张依赖另一主张（推断链）

EVIDENCE_RELATIONS = (
    RELATION_SUPPORTS,
    RELATION_CONTRADICTS,
    RELATION_EXTRAPOLATION,
    RELATION_DEPENDS_ON,
)

# --- 撤回对象类型 -------------------------------------------------------------

WITHDRAWN_SAMPLE = "sample"
WITHDRAWN_ANALYSIS = "analysis"
WITHDRAWN_KINDS = (WITHDRAWN_SAMPLE, WITHDRAWN_ANALYSIS)

# --- 论文/发布状态 ------------------------------------------------------------

PAPER_DRAFT = "draft"
PAPER_SUBMITTED = "submitted"  # 已提交：快照冻结
PAPER_PUBLICATION_STATES = (PAPER_DRAFT, PAPER_SUBMITTED)

RELEASE_PENDING = "pending"
RELEASE_APPROVED = "approved"
RELEASE_REJECTED = "rejected"
RELEASE_STATES = (RELEASE_PENDING, RELEASE_APPROVED, RELEASE_REJECTED)
