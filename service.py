"""蛋白发现主张登记的运行入口。

用法：
  python3 service.py --check                # 配置与存储自检
  python3 service.py --port 8000            # 启动服务（默认 SQLite 落盘 data/registry.db）
  python3 service.py --port 8000 --db :memory:

身份头：X-Role（researcher/curator/reviewer/pi），X-Actor 记录操作者。
公开接口 /api/public/** 无需内部角色。
"""

import argparse
import json
import os
from http.server import ThreadingHTTPServer

from registry.api import create_handler
from registry.domain import Registry
from registry.store import Store

SERVICE_ID = "protein-claim-registry"
SERVICE_NAME = "蛋白发现主张登记"
DEFAULT_DB = os.environ.get("CLAIM_REGISTRY_DB", "data/registry.db")


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_registry(db_path=DEFAULT_DB):
    store = Store(db_path)
    return Registry(store)


# 保持向后兼容：旧契约测试直接以 Handler 启动服务器（导入期使用独立内存库）。
Handler = create_handler(
    build_registry(os.environ.get("CLAIM_REGISTRY_DB", ":memory:")),
    health=health_payload,
)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite 路径，:memory: 为内存库")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        check_store = Store(":memory:")
        check_registry = Registry(check_store)
        pid = check_registry.register_protein("自检蛋白", "demo", aliases=["CHK"])
        assert check_registry.get_protein(pid)["aliases"] == ["CHK"]
        chain = check_store.verify_audit_chain()
        assert chain["ok"] and chain["events"] >= 1
        check_store.close()
        print("基础检查通过")
        return

    registry = build_registry(args.db)
    handler = create_handler(registry, health=health_payload)
    try:
        ThreadingHTTPServer(("0.0.0.0", args.port), handler).serve_forever()
    finally:
        registry.store.close()


if __name__ == "__main__":
    main()
