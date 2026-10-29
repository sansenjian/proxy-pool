"""GitHub Actions 代理池刷新脚本 v2
优化:
- 候选限量采样(默认1500), 控制Upstash命令消耗
- 入池保留原分, 新代理100分
- 每轮重验池内100个老代理, 失败-15分, 驱动淘汰
- 淘汰: 分数<30移出; 池子上限500, 超出按分数删尾部
仅用 requests 一个依赖, 通过 Upstash REST API 读写
"""
import json
import os
import random
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
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", 1500))
POOL_MAX = int(os.getenv("POOL_MAX", 500))
MIN_SCORE = float(os.getenv("MIN_SCORE", 30))
RECHECK_N = int(os.getenv("RECHECK_N", 100))
POOL_KEY = "proxy:pool"
META_KEY = "proxy:meta"


def crawl() -> list:
    proxies = set()
    for url in SOURCES:
        try:
            text = requests.get(url, headers=HEADERS, timeout=20).text
            proxies |= set(IP_PORT.findall(text))
        except Exception as e:
            print(f"[crawl] {url} 失败: {e}")
    print(f"[crawl] 候选总数: {len(proxies)}")
    if len(proxies) > MAX_CANDIDATES:
        proxies = set(random.sample(sorted(proxies), MAX_CANDIDATES))
        print(f"[crawl] 采样至 {MAX_CANDIDATES}")
    return sorted(proxies)


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


class Upstash:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def cmd(self, *args):
        r = requests.post(self.base, headers=self.h, json=list(args), timeout=15)
        r.raise_for_status()
        return r.json()["result"]

    def pipeline(self, cmds):
        r = requests.post(self.base + "/pipeline", headers=self.h, json=cmds, timeout=30)
        r.raise_for_status()
        return r.json()


def main():
    db = Upstash(os.environ["UPSTASH_REDIS_REST_URL"],
                 os.environ["UPSTASH_REDIS_REST_TOKEN"])

    # ---------- 1. 重验池内老代理 (驱动淘汰) ----------
    existing = db.cmd("ZRANGE", POOL_KEY, 0, -1) or []
    print(f"[recheck] 池内现有 {len(existing)} 个")
    if existing:
        sample = random.sample(existing, min(RECHECK_N, len(existing)))
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            dead = [p for p, ok in zip(sample, ex.map(lambda p: check(p) is None, sample)) if ok]
        if dead:
            db.pipeline([["ZINCRBY", POOL_KEY, str(SCORE_FAIL := -15), p] for p in dead])
            print(f"[recheck] {len(dead)} 个失效, 各-15分")
        # 淘汰低分
        removed = db.cmd("ZREMRANGEBYSCORE", POOL_KEY, "-inf", str(MIN_SCORE))
        if removed:
            print(f"[evict] 淘汰 {removed} 个低分代理 (<{MIN_SCORE})")

    # ---------- 2. 抓取+验证新候选 ----------
    alive = validate(crawl())
    print(f"[validate] 存活 {len(alive)}")

    if alive:
        score_res = db.pipeline([["ZSCORE", POOL_KEY, p] for p, _ in alive])
        existing_scores = {p: float(r["result"]) for (p, _), r in zip(alive, score_res)
                           if r.get("result") is not None}
        now = int(time.time())
        writes = []
        for proxy, speed in alive:
            if proxy not in existing_scores:
                writes.append(["ZADD", POOL_KEY, "100", proxy])
            writes.append(["HSET", META_KEY, proxy,
                           json.dumps({"speed": round(speed, 3), "checks": 1, "last_seen": now})])
        db.pipeline(writes)

    # ---------- 3. 池子上限控制 ----------
    while int(db.cmd("ZCARD", POOL_KEY)) > POOL_MAX:
        dropped = db.cmd("ZPOPMIN", POOL_KEY)
        if not dropped:
            break
    print(f"[done] 池内总数 {db.cmd('ZCARD', POOL_KEY)} / 上限 {POOL_MAX}")


if __name__ == "__main__":
    main()
