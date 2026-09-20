"""蛋白发现主张登记的运行入口。

- `python3 service.py --check`：核对服务配置与领域装配；
- `python3 service.py --port 8000`：启动服务，内存事件日志（联调）；
- `python3 service.py --store data/events.jsonl`：事件只追加落盘，重启重放。
"""

import argparse
import json
from http.server import ThreadingHTTPServer

from registry import EventStore, Registry
from registry.api import make_handler

SERVICE_ID = "protein-claim-registry"
SERVICE_NAME = "蛋白发现主张登记"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_registry(store_path=None):
    store = EventStore(store_path)
    return Registry(store)


def build_server(port, store_path=None):
    registry = build_registry(store_path)
    handler = make_handler(registry, health_payload)
    return ThreadingHTTPServer(("0.0.0.0", port), handler), registry


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--store", default=None,
                        help="只追加事件日志路径（JSONL）；缺省为内存存储")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        registry = build_registry(None)
        # 领域装配自检：空登记系统必须可派生
        assert registry.list_claims() == []
        assert registry.public_papers() == []
        print("基础检查通过")
        return

    server, registry = build_server(args.port, args.store)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        registry.store.close()


if __name__ == "__main__":
    main()
