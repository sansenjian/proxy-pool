/**
 * Cloudflare Worker - 免费代理池 API
 * 数据存 Upstash Redis (REST API), 从环境变量读取连接信息
 * 路由: /get /list /report /stats
 */

const SCORE_MAX = 100;
const SCORE_SUCCESS = 5;
const SCORE_FAIL = -15;

async function upstash(cmd) {
  const resp = await fetch(UPSTASH_REDIS_REST_URL, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${UPSTASH_REDIS_REST_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(cmd),
  });
  const data = await resp.json();
  if (data.error) throw new Error(data.error);
  return data.result;
}

async function upstashPipeline(cmds) {
  const resp = await fetch(UPSTASH_REDIS_REST_URL + "/pipeline", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${UPSTASH_REDIS_REST_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(cmds),
  });
  return await resp.json();
}

const POOL = "proxy:pool";
const META = "proxy:meta";

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const cors = { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" };

    if (request.method === "OPTIONS")
      return new Response(null, { status: 204, headers: cors });

    try {
      switch (url.pathname) {
        case "/get":
          return json(await handleGet(url), cors);
        case "/report":
          return json(await handleReport(request), cors);
        case "/list":
          return json(await handleList(url), cors);
        case "/stats":
          return json(await handleStats(), cors);
        default:
          return json({ name: "free-proxy-pool", endpoints: ["/get", "/list", "/report", "/stats"] }, cors);
      }
    } catch (e) {
      return json({ code: 0, msg: String(e) }, cors, 500);
    }
  },
};

function json(data, headers, status = 200) {
  return new Response(JSON.stringify(data), { status, headers });
}

async function handleGet(url) {
  const strategy = url.searchParams.get("strategy") || "random";
  const members = await upstash(["ZREVRANGE", POOL, 0, 9]); // 前10名
  if (!members.length) return { code: 0, msg: "池为空, 等待GitHub Actions刷新" };

  let picked;
  if (strategy === "best") {
    picked = members[0];
  } else if (strategy === "weighted") {
    const scores = await upstashPipeline(members.map((m) => ["ZSCORE", POOL, m]));
    const weights = scores.map((r) => Math.max(parseFloat(r.result) || 1, 1));
    picked = weightedPick(members, weights);
  } else {
    picked = members[Math.floor(Math.random() * members.length)];
  }
  return { code: 1, proxy: picked, strategy };
}

function weightedPick(items, weights) {
  const total = weights.reduce((a, b) => a + b, 0);
  let r = Math.random() * total;
  for (let i = 0; i < items.length; i++) {
    r -= weights[i];
    if (r <= 0) return items[i];
  }
  return items[items.length - 1];
}

async function handleReport(request) {
  const body = await request.json();
  const { proxy, success } = body;
  if (!proxy) return { code: 0, msg: "缺少 proxy 字段" };

  const score = parseFloat(await upstash(["ZSCORE", POOL, proxy]));
  if (isNaN(score)) return { code: 0, msg: "代理不在池中" };

  let newScore;
  if (success) {
    newScore = Math.min(score + SCORE_SUCCESS, SCORE_MAX);
  } else {
    newScore = score + SCORE_FAIL;
  }

  const now = Math.floor(Date.now() / 1000);
  if (newScore <= 0 && !success) {
    // 扣光淘汰
    await upstashPipeline([["ZREM", POOL, proxy], ["HDEL", META, proxy]]);
    return { code: 1, proxy, score: 0, evicted: true };
  }

  await upstashPipeline([
    ["ZADD", POOL, String(newScore), proxy],
    ["HSET", META, proxy, JSON.stringify({ last_seen: now })],
  ]);
  return { code: 1, proxy, score: newScore };
}

async function handleList(url) {
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "100"), 500);
  const withScores = await upstash(["ZREVRANGE", POOL, 0, limit - 1, "WITHSCORES"]);
  const members = [];
  for (let i = 0; i < withScores.length; i += 2) members.push(withScores[i]);
  const metas = await upstashPipeline(members.map((m) => ["HGET", META, m]));

  const data = members.map((m, i) => {
    const item = { proxy: m, score: parseFloat(withScores[i * 2 + 1]) };
    if (metas[i] && metas[i].result) {
      try { Object.assign(item, JSON.parse(metas[i].result)); } catch {}
    }
    return item;
  });
  return { code: 1, count: await upstash(["ZCARD", POOL]), data };
}

async function handleStats() {
  const total = await upstash(["ZCARD", POOL]);
  const top = await upstash(["ZREVRANGE", POOL, 0, 0, "WITHSCORES"]);
  return { code: 1, data: { total, top_score: top.length ? parseFloat(top[1]) : 0 } };
}
