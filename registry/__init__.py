"""蛋白发现主张登记领域包。

模型要点：
- 所有写操作只追加事件（event sourcing），实体与主张置信状态由事件重放得到；
- 论文快照在提交时冻结，后续证据变化只改变当前投影，不回写历史；
- 撤回事件使受影响的主张进入“待复核”，不删除任何数据。
"""

from .registry import Registry
from .store import EventStore
from .vocab import (
    CLAIM_TYPES,
    EXTRAPOLATED,
    NEEDS_REVIEW,
    REFUTED,
    RELATION_CONTRADICTS,
    RELATION_DEPENDS_ON,
    RELATION_EXTRAPOLATION,
    RELATION_SUPPORTS,
    STATUSES,
    SUPPORTED,
    UNVALIDATED,
)

__all__ = [
    "EventStore",
    "Registry",
    "CLAIM_TYPES",
    "STATUSES",
    "SUPPORTED",
    "REFUTED",
    "UNVALIDATED",
    "EXTRAPOLATED",
    "NEEDS_REVIEW",
    "RELATION_EXTRAPOLATION",
    "RELATION_DEPENDS_ON",
    "RELATION_CONTRADICTS",
    "RELATION_SUPPORTS",
]
