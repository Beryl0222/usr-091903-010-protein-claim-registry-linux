"""只追加事件日志：登记系统唯一的事实来源。

任何写操作都不覆盖既有数据，而是 append 一条事件；
实体、主张状态、论文快照全部由事件重放得到。
持久化为 JSONL，每行一个事件，追加时加锁并 fsync。
"""

import json
import os
import threading
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ValidationError(ValueError):
    """命令参数不满足领域约束（映射为 HTTP 400）。"""


class ConflictError(ValueError):
    """与已追加的历史冲突，例如重复标识或修改已冻结快照（映射为 HTTP 409）。"""


class NotFoundError(LookupError):
    """引用的实体不存在（映射为 HTTP 404）。"""


class EventStore:
    def __init__(self, path=None, clock=utc_now):
        self.path = path
        self._clock = clock
        self._lock = threading.RLock()
        self._events = []
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        self._events.append(json.loads(line))
        self._file = open(path, "a", encoding="utf-8") if path else None

    # --- 基本读取 -------------------------------------------------------------

    @property
    def events(self):
        return tuple(self._events)

    def seq(self):
        return len(self._events)

    # --- 追加 -----------------------------------------------------------------

    def append(self, event_type, payload, actor=None):
        """校验由上层 Registry 完成；此处只负责编号、计时与落盘。"""
        event = {
            "seq": len(self._events) + 1,
            "ts": self._clock(),
            "type": event_type,
            "actor": actor,
            "payload": payload,
        }
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._events.append(event)
            if self._file is not None:
                self._file.write(line + "\n")
                self._file.flush()
                os.fsync(self._file.fileno())
        return event

    def close(self):
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
