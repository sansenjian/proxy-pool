# 全免费代理池部署（GitHub Actions + Upstash + Cloudflare Workers）

零服务器、零信用卡，三个免费平台拼成的完整代理池：

```
GitHub Actions (每30分钟)   抓取+验证代理, 真实跑验证逻辑
        ↓ Upstash REST
    Upstash Redis            免费50万命令/月, 池子用量约1万/月
        ↑ Upstash REST
  Cloudflare Workers         对外API, 免费10万次请求/天
```

## 费用一览

| 组件 | 免费额度 | 代理池实际用量 |
|------|---------|--------------|
| GitHub Actions | 私有库2000分钟/月，公开库不限 | 每次刷新约3分钟 × 48次/天 ≈ 4300分钟/月 → **建议仓库设为 Public** |
| Upstash Redis | 50万命令/月 | 约1万命令/月 |
| Cloudflare Workers | 10万次请求/天 | 几十次/天 |

## 部署步骤（约20分钟）

### 第1步：创建 Upstash Redis（5分钟）

1. 打开 https://console.upstash.com ，用 GitHub 账号登录（免费，不绑卡）
2. Create Database → Regional 类型 → 区域选 `ap-southeast-1`（新加坡，国内访问快）
3. 进入数据库详情页，复制 **REST URL** 和 **REST TOKEN**

### 第2步：配置 GitHub 仓库（5分钟）

1. 把本项目 push 到你的 GitHub（新建仓库时选 **Public**，白嫖无限 Actions 分钟）
2. 仓库 → Settings → Secrets and variables → Actions → New repository secret，添加两个：
   - `UPSTASH_REDIS_REST_URL` = 第1步复制的 URL
   - `UPSTASH_REDIS_REST_TOKEN` = 第1步复制的 TOKEN
3. 到 Actions 页签，选中 "Refresh Proxy Pool" → Run workflow 手动触发第一次

### 第3步：部署 Cloudflare Worker（10分钟）

方式A（命令行）：
```bash
cd worker
npx wrangler login          # 浏览器授权
npx wrangler secret put UPSTASH_REDIS_REST_URL     # 粘贴URL
npx wrangler secret put UPSTASH_REDIS_REST_TOKEN   # 粘贴TOKEN
npx wrangler deploy         # 部署, 得到 https://free-proxy-pool.<你的>.workers.dev
```

方式B（网页）：https://workers.cloudflare.com 登录 → Create Worker → 粘贴 worker.js 全文 → Settings → Variables and Secrets 添加两个 secret → Deploy

## API 用法

```
GET  https://你的worker.workers.dev/get?strategy=best
GET  https://你的worker.workers.dev/list?limit=50
GET  https://你的worker.workers.dev/stats
POST https://你的worker.workers.dev/report   {"proxy":"x.x.x.x:port","success":true}
```

客户端接入：

```python
import requests

BASE = "https://你的worker.workers.dev"
proxy = requests.get(f"{BASE}/get").json()["proxy"]
proxies = {"http": f"http://{proxy}", "https": f"http://{proxy}"}

try:
    ok = requests.get("https://目标站.com", proxies=proxies, timeout=10).status_code == 200
except Exception:
    ok = False
requests.post(f"{BASE}/report", json={"proxy": proxy, "success": ok})
```

## 注意

- **workers.dev 域名在国内访问不稳定**：在 CF 控制台给 Worker 绑一个自己的域名（免费，域名需接入 Cloudflare NS），或用 Pages/自有网关转发
- 计分规则：成功+5（封顶100），失败-15，扣光淘汰，由调用方 `/report` 驱动
- 想提高代理质量：把 workflow 里的 `TEST_URL` 环境变量改成你的真实目标站
- GitHub 定时任务最短 15 分钟，且有随机延迟（可能晚几分钟触发），对代理池无影响
