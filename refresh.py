"""GitHub Actions 上运行的代理池刷新脚本
流程: 抓取免费源 -> 并发验证 -> 已存在代理保留分数, 新代理以100分入池
仅用 requests 一个依赖, 通过 Upstash REST API 读写 (两次HTTP往返搞定)
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-500.txt",
]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
IP_PORT = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}\b")

TEST_URL = os.getenv("TEST_URL", "https://httpbin.org/ip")
TEST_TIMEOUT = float(os.getenv("TEST_TIMEOUT", 6))
WORKERS = int(os.getenv("WORKERS", 200))
POOL_KEY = "proxy:pool"
META_KEY = "proxy:meta"


def crawl() -> set:
    proxies = set()
    for url in SOURCES:
        try:
            text = requests.get(url, headers=HEADERS, timeout=20).text
            found = set(IP_PORT.findall(text))
            print(f"[crawl] {url.split('/')[-1]}: {len(found)}")
            proxies |= found
        except Exception as e:
            print(f"[crawl] {url} 失败: {e}")
    print(f"[crawl] 候选总数: {len(proxies)}")
    return proxies


def check(proxy):
    try:
        resp = requests.get(
            TEST_URL,
            proxies={"http": f"http://{proxy}", "https": f"http://{proxy}"},
            timeout=TEST_TIMEOUT,
        )
        if resp.status_code == 200:
            return proxy, resp.elapsed.total_seconds()
    except Exception:
        pass
    return None


def validate(candidates) -> list:
    alive = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(check, p) for p in candidates]
        for f in as_completed(futs):
            r = f.result()
            if r:
                alive.append(r)
    return alive


def send_pipeline(base: str, token: str, cmds: list) -> list:
    """Upstash /pipeline: 一次HTTP往返执行多条命令"""
    resp = requests.post(
        base.rstrip("/") + "/pipeline",
        headers={"Authorization": f"Bearer {token}"},
        json=cmds,
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()
    for r in results:
        if not r.get("success", True):
            print(f"[warn] 命令失败: {r}")
    return results


def main():
    base = os.environ["UPSTASH_REDIS_REST_URL"]
    token = os.environ["UPSTASH_REDIS_REST_TOKEN"]

    candidates = crawl()
    alive = validate(candidates)
    print(f"[validate] 存活 {len(alive)} / {len(candidates)}")

    if not alive:
        print("[done] 无存活代理, 本次结束")
        return

    # 第1次往返: 批量查已存在的分数 (新代理才给初始100分, 不覆盖老代理的积累分)
    score_cmds = [["ZSCORE", POOL_KEY, p] for p, _ in alive]
    score_res = send_pipeline(base, token, score_cmds)
    existing = {p: float(r["result"]) for (p, _), r in zip(alive, score_res)
                if r.get("result") is not None}

    # 第2次往返: 写入
    now = int(time.time())
    write_cmds = []
    for proxy, speed in alive:
        if proxy not in existing:
            write_cmds.append(["ZADD", POOL_KEY, "100", proxy])
        meta = {"speed": round(speed, 3), "checks": 1, "last_seen": now}
        write_cmds.append(["HSET", META_KEY, proxy, json.dumps(meta)])
    send_pipeline(base, token, write_cmds)

    total = requests.post(base, headers={"Authorization": f"Bearer {token}"},
                          json=["ZCARD", POOL_KEY], timeout=10).json()["result"]
    print(f"[done] 新增 {len(alive) - len(existing)} 个, "
          f"更新 {len(existing)} 个, 池内总数 {total}")


if __name__ == "__main__":
    main()
