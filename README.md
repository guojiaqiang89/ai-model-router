# AI 协作系统

一台新加坡云服务器，跑着 Claude Code、Codex、DeepSeek Harness 三套 AI 能力，外加一个把它们串起来的调度器。

**你要做的只是说清楚想要什么，剩下的设计、实现、测试、部署、发链接，由这套系统完成。**

---

## 目录

- [这套系统能做什么](#这套系统能做什么)
- [相比本机装 WorkBuddy 或单个 harness，优势在哪](#相比本机装-workbuddy-或单个-harness优势在哪)
- [系统构成](#系统构成)
- [前置条件](#前置条件)
- [部署步骤](#部署步骤)
- [验证清单](#验证清单)
- [日常使用方式](#日常使用方式)
- [安全与维护](#安全与维护)
- [已知限制](#已知限制)

---

## 这套系统能做什么

### 用法就是一句话需求

你不需要懂架构、不需要选模型、不需要自己拆任务。把目标讲清楚就行：

| 你说 | 系统做的事 |
|---|---|
| “做一个每天早上汇总行业新闻、推送到飞书群的 agent” | Claude 设计方案 → Codex 写代码 → 跑通测试 → 配好定时任务 → 接上飞书机器人 → 告诉你第一条推送什么时候到 |
| “做个内部用的报价计算网页，销售在手机上能打开” | 设计交互 → 实现前后端 → 部署到服务器 → 绑好域名和 HTTPS → 给你一个能直接发到群里的链接 |
| “把这堆 Excel 按客户拆开，算出每家的年度汇总” | 直接在服务器上处理完，结果丢给你，或者做成一个以后能重复跑的脚本 |
| “把我们的系统能力写成文档发给同事” | 写文档 → 发布到你自己的域名 → 给你带访问密码的链接 |
| “这段代码有没有安全问题” | Claude 审查，必要时叫 Codex 交叉复核，给出带复现步骤的结论 |

### 交付形态

做完的东西不会停在服务器的某个目录里，而是能直接用、直接发出去：

- **一个网址** —— 自动签发 HTTPS 证书，手机电脑都能打开，可加密码保护
- **一个常驻服务** —— 注册成系统服务，开机自启、崩溃自动重启
- **GitHub 仓库或 PR** —— 代码直接推上去
- **飞书消息 / 文档 / 日历事件** —— 通过官方 CLI 直接发到群里或建成云文档
- **一个可重复执行的命令** —— 以后你自己敲一行就能再跑一遍

### 关于“创作 agent”

这套系统本身就是用来造 agent 的，造出来的 agent 有三种常见形态：

1. **定时型** —— 挂系统定时器，按点自己跑（日报、巡检、数据同步）
2. **触发型** —— 收到飞书消息、GitHub webhook、API 调用时被唤醒
3. **常驻对话型** —— 带网页界面，你或同事随时打开跟它对话干活

第三种可以直接复用 DeepSeek Harness 的网页界面，不用从零写前端。

---

## 相比本机装 WorkBuddy 或单个 harness，优势在哪

先说结论：**单机工具适合"帮我写这段代码"，这套系统适合"帮我把这件事做完并交付出去"。**

| 维度 | 本机 WorkBuddy / 单个 harness | 这套系统 |
|---|---|---|
| **任务能不能长跑** | 合上笔记本就断，睡眠、断网、重启都会中断 | 服务器 7×24 在线，任务交代完就可以关电脑，回头看结果 |
| **能不能交付链接** | 只能在本机跑，给别人看要截图或者自己想办法部署 | 有公网 IP 和域名，做完直接给网址，自动 HTTPS |
| **额度用完怎么办** | 绑死一家，额度耗尽就停摆 | Claude 订阅 + Codex 订阅 + DeepSeek/Qwen API 三套独立额度，自动降级，一家挂了活还能继续 |
| **谁来把关质量** | 一个模型自己写自己交，没人复核 | Claude 固定做架构和审查，其余模型只做执行，产出要过 Claude 这关 |
| **模型乱执行命令的风险** | 多数工具靠"询问你是否允许"，点多了就麻木 | 模型执行的 shell 命令跑在 bwrap 沙盒里：只有工作目录可写、`/root` 和 `/home` 在沙盒里根本不存在、环境变量全清空，防止密钥被读走外传 |
| **换个工具还记得上下文吗** | 各家会话历史互不相通，换工具等于从头讲 | 每个项目一份 `PROJECT_STATUS.md`，任何工具接手都先读它 |
| **对外沟通** | 手动复制粘贴 | 飞书、GitHub、网页发布都在服务器上，一条命令送出去 |
| **多人协作** | 每人装一套，各自为战 | 同事 SSH 进同一台机器，或直接访问你发布的网址 |
| **成本** | 一份订阅 | 多一台 ECS（2 核 4G，很便宜）+ API 按量费用 |

**诚实地说不适合的场景**：只是偶尔让 AI 补几行代码、不需要交付给别人、也不介意关电脑就中断 —— 那本机工具更省事，不用为这套系统付出运维成本。

---

## 系统构成

| 组件 | 承担角色 | 计费方式 |
|---|---|---|
| **Claude Code** | 架构设计、代码审查、任务编排的“大脑”——所有需求先经过它判断 | Claude 订阅（Pro/Max） |
| **Codex CLI** | 具体实施的主力——写代码、跑测试、改文件 | ChatGPT 订阅（不计 API 费） |
| **DeepSeek Harness（`dsh`）+ DeepSeek/Qwen API** | 便宜的基础任务，以及 Codex 额度不足时的自动降级备份；自带网页对话界面 | 按 token 计费 |
| **Model Router（`model-router`，本仓库）** | 调度中枢：架构类只找 Claude，实施类优先 Codex、失败自动转 DeepSeek→Qwen，基础类直接走 DeepSeek→Qwen | 无额外费用 |

**核心设计原则**：Claude 是唯一的架构把关人，其余模型都是执行层；即使 Claude 或 Codex 额度耗尽，`model-router` 仍能独立调用 DeepSeek/Qwen 顶上基础工作，项目不会整个停摆。

---

## 前置条件

### 服务器

**阿里云 ECS，2 核 4G 起步**（本系统实际运行在 `ecs.e-c1m2.large`：2 核 / 4G 内存 / 40G 系统盘 / 4G swap，新加坡 `ap-southeast-1` 地域，日常跑满足够用）。

- 系统选 **Ubuntu 24.04 或 26.04 LTS**
- 磁盘 40G 起，Node 依赖和会话数据占空间
- 建议开 swap（本机开了 4G），dsh 构建和多模型并发时内存会吃紧

### 域名（建议在阿里云一并买）

买一个域名，把一个子域名（比如 `ai.你的域名.com`）的 **A 记录**指向 ECS 公网 IP。作用有两个：

1. **发布网页应用** —— 做好的工具、文档、agent 对话界面都通过域名对外提供，Caddy 自动签发 HTTPS 证书
2. **方便 SSH** —— 以后换 IP 或加机器，记域名比记 IP 方便，客户端配置也不用改

> **服务器在新加坡，中国大陆访问无需 ICP 备案。** 域名绑到境外服务器上可以立即使用，省掉备案流程（备案只针对大陆境内服务器）。代价是从国内访问会比境内服务器慢一些，做面向内部的工具完全够用。

### 账号与订阅

- **Claude 订阅**（Pro 或 Max）—— Claude Code CLI 走 OAuth 登录
- **ChatGPT 订阅**（Plus 或 Pro）—— Codex CLI 走 OAuth 登录，**不是** OpenAI API key
- **DeepSeek 开放平台 API Key** —— https://platform.deepseek.com/
- 可选：**阿里云 DashScope（Qwen）API Key** —— DeepSeek 失败时的下一级备份
- 可选：**飞书开放平台**账号 —— 创建自建应用，用于消息/文档/日历集成
- **GitHub 账号** —— 代码托管

> Claude Code 和 Codex CLI 用的都是订阅内额度，不按 API 计费，团队已有订阅就不用额外买。

---

## 部署步骤

### 步骤一：基础环境

```bash
apt update && apt install -y git curl python3 build-essential bubblewrap

# Node.js 22+
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt install -y nodejs
npm install -g pnpm

# Caddy（自动 HTTPS 的反向代理）
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

验证（括号内为本机实测版本）：

```bash
node --version      # v22.22.1
python3 --version   # Python 3.14.4
git --version       # git 2.53.0
bwrap --version     # bubblewrap 0.11.1
caddy version       # v2.11.4
```

> `bubblewrap`（`bwrap`）是必装项：Model Router 用它把模型执行的 shell 命令关进沙盒。没有它 `run_shell` 会被直接禁用。

### 步骤二：Claude Code 与 Codex CLI

```bash
npm install -g @anthropic-ai/claude-code
npm install -g @openai/codex

claude    # 首次运行打印登录链接，浏览器登录 Claude 账号
codex     # 同理，跳转 ChatGPT 登录
```

> SSH 环境下登录会给一个 `http://localhost:...` 回调地址，用 `ssh -L` 做端口转发后在本地浏览器打开，或直接把链接贴到本地浏览器按提示走完。

登录后两者都支持非交互调用（Model Router 靠这两条命令调度）：

```bash
claude -p "这个项目最大的技术债是什么？" --model claude-opus-5 --permission-mode plan
codex exec --sandbox workspace-write --skip-git-repo-check -C /path/to/project "把 xxx 重构成 yyy"
```

### 步骤三：DeepSeek Harness（dsh）

```bash
npm install -g @deepseek-ai/dsh@0.1.5-rc.3

mkdir -p /root/projects/deepseek-harness-workspace
cat > /root/projects/deepseek-harness-workspace/.env << 'EOF'
DEEPSEEK_API_KEY=你的DeepSeek密钥
EOF
chmod 600 /root/projects/deepseek-harness-workspace/.env
```

做成开机自启的常驻服务（`你的域名` 换成步骤六要绑的域名）：

```bash
cat > /etc/systemd/system/dsh-web.service << 'EOF'
[Unit]
Description=DeepSeek Harness Web UI
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/dsh web --no-open --host 127.0.0.1 --port 3080 --trusted-host 你的域名
WorkingDirectory=/root/projects/deepseek-harness-workspace
Restart=on-failure
RestartSec=5
User=root
Environment=HOME=/root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now dsh-web
```

**三个必须知道的坑：**

1. **`--trusted-host` 必须填反代它的域名**，否则挂到公网后静态页面能打开，但所有 `/api` 请求会被它的 browser-trust 机制拦成 403。
2. **网页界面需要 token 才能进**。启动时会在日志里打印入口地址，且**每次重启 token 都会变**：
   ```bash
   journalctl -u dsh-web -o cat | grep -o 'token=[A-Za-z0-9_-]*' | tail -1
   ```
   访问 `https://你的域名/?token=<token>`，浏览器会自动跳转并种 cookie。目前没有固定 token 的配置项。
3. **版本要锁死，不要随手升级**。dsh 是 developer preview，核心 CLI 版本和 profile 里的插件版本必须匹配，升级容易崩（详见[安全与维护](#安全与维护)）。

### 步骤四：Model Router（本仓库）

```bash
cd /root/projects
gh repo clone guojiaqiang89/ai-model-router
cd ai-model-router

# 密钥不在仓库里（.gitignore 已排除 .env），需要自己建
cat > .env << 'EOF'
DEEPSEEK_API_KEY=你的DeepSeek密钥
QWEN_API_KEY=你的Qwen密钥
EOF
chmod 600 .env

chmod +x router.py
ln -s /root/projects/ai-model-router/router.py /usr/local/bin/model-router

# 上线前先跑测试（全离线，不调任何付费 API）
python3 -m unittest test_router test_sandbox
```

三档路由：

```bash
# 架构设计：只找 Claude，只读不改文件，没有降级链
model-router --type architecture --cwd <目录> "这个模块该怎么拆？"

# 实施类：优先 Codex，失败且未改过文件时自动降级 DeepSeek → Qwen
model-router --type implementation --cwd <目录> "把 xxx 功能实现出来，并补上测试"

# 基础类：直接走 DeepSeek（经 dsh headless）→ Qwen，不占用订阅额度
model-router --type basic --cwd <目录> "把这个目录下的日志按日期归档"
model-router --type basic "纯文字问答，不给文件权限"

# 诊断：不调模型，只检测 bwrap 沙盒是否真的可用
model-router --check-sandbox
```

**关键安全语义**（多轮对抗式代码审查后加固，不要随意改动）：

- 任何后端执行到一半失败、**且已经改过文件**，不会自动降级给下一个模型接着改——避免两个模型交叉修改同一批文件造成叠加损坏。只有“失败但没动过文件”才降级。
- `run_shell` 跑在 bwrap 沙盒里：只有工作目录可写，`/root`、`/home` 在沙盒里根本不存在，环境变量全部清空（防止预先 export 的密钥被读走再借网络发出）。
- 沙盒可用性靠**真实探测**判定（真跑一次 bwrap），探测失败就直接禁用 `run_shell`，除非人工显式加 `--allow-unsandboxed-shell` 授权。

### 步骤五：飞书 / Lark 集成

```bash
npm install -g @larksuite/cli
```

1. 打开[飞书开放平台](https://open.feishu.cn/)，创建**自建应用**，记下 App ID / App Secret
2. 在“权限管理”里按需开通权限（发消息、读写云文档、日历、邮件等），发布版本
3. 把应用拉进需要它干活的群
4. 配置：

```bash
lark-cli config          # 填 App ID / App Secret，加密存到 ~/.local/share/lark-cli/
lark-cli auth login      # 可选：需要以"人"的身份操作时走扫码授权
lark-cli doctor          # 健康检查
```

常用：

```bash
lark-cli im --help                     # 消息域命令列表
lark-cli schema im.message.create      # 查参数、类型、所需权限
lark-cli api GET /open-apis/...        # 万能兜底
```

> 每条命令 `--help` 会标注 `read` / `write` / `high-risk-write`。high-risk-write 需要 `--yes`，且应先跟人确认——让 AI 驱动时尤其注意。

### 步骤六：GitHub 与域名发布

**GitHub：**

```bash
apt install -y gh
gh auth login      # GitHub.com → HTTPS → 浏览器登录
gh auth status
```

建议 scope：`repo`、`workflow`、`read:org`、`gist`。

**域名与网页发布：**

先在阿里云域名控制台把子域名的 **A 记录**指向 ECS 公网 IP，等生效（`getent hosts 你的域名` 能解析）。境外服务器无需备案，解析生效即可用。

```bash
# 为网页界面生成访问密码
PASS=$(openssl rand -base64 18 | tr -d '=+/' | head -c 20)
echo "$PASS" > /root/projects/deepseek-harness-workspace/.web-ui-password.txt
chmod 600 /root/projects/deepseek-harness-workspace/.web-ui-password.txt
caddy hash-password --plaintext "$PASS"     # 哈希填进下面配置
cat /root/projects/deepseek-harness-workspace/.web-ui-password.txt   # 记住明文密码

# 文档站用另一套独立凭据
mkdir -p /var/www/published-docs && chown -R caddy:caddy /var/www/published-docs
DOCPASS=$(openssl rand -base64 15 | tr -d '=+/' | head -c 16)
echo "$DOCPASS"
caddy hash-password --plaintext "$DOCPASS"
```

编辑 `/etc/caddy/Caddyfile`：

```caddyfile
你的域名 {
    # 文档/静态页面：独立凭据，只能读文件
    handle_path /docs/* {
        basic_auth {
            docs 文档站的哈希
        }
        root * /var/www/published-docs
        file_server browse
    }

    # 其余路径：dsh 网页界面
    handle {
        basic_auth {
            dsh dsh的哈希
        }
        reverse_proxy 127.0.0.1:3080
    }
}
```

生效：

```bash
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

> **两个必须遵守的点：**
> 1. **dsh 界面必须加 Basic Auth。** 它能在服务器上执行命令，不加密码挂公网等于把命令行开放给任何知道域名的人。
> 2. **文档站要用独立凭据。** 发文档给同事只给 `docs` 那套密码，别把 dsh 的密码分发出去。
> 3. 静态文件要放在 `/var/www` 下。Caddy 以 `caddy` 用户运行，进不了权限为 `0700` 的 `/root`，放那里会 403。

---

## 验证清单

逐条跑一遍，全部通过再交付使用：

```bash
claude -p "1+1=?"                                  # 返回 2
codex exec --sandbox workspace-write --skip-git-repo-check "echo ok"   # 退出码 0
dsh --profile headless "说 ok"                     # 输出文本，退出码 0
model-router --check-sandbox                       # 报告 bwrap 可用
model-router --type basic "1+1=?"                  # 走通完整路由链
lark-cli doctor                                    # "ok": true
gh auth status                                     # Logged in to github.com
curl -I https://你的域名                            # 401（有 Basic Auth）
systemctl is-active dsh-web caddy                  # 两个都是 active
cd /root/projects/ai-model-router && python3 -m unittest test_router test_sandbox   # OK
```

常见失败：

| 现象 | 原因 | 处理 |
|---|---|---|
| 网页能打开但对话报错，`/api` 返回 403 | `--trusted-host` 没填或填错 | 改 `dsh-web.service`，`systemctl daemon-reload && systemctl restart dsh-web` |
| 网页返回 401 且密码正确 | dsh 的 token 认证 | 从日志取 token，访问 `https://域名/?token=<token>` |
| 文档站返回 403 | 静态文件放在 `/root` 下，caddy 用户读不到 | 移到 `/var/www/published-docs` 并 `chown caddy:caddy` |
| `model-router --check-sandbox` 报不可用 | 宿主禁止创建 user namespace | 确认内核允许非特权 userns；不要随手加 `--allow-unsandboxed-shell` |
| Codex 报配额/限流 | 订阅额度用完 | 正常现象，router 自动降级；OpenAI 不提供额度查询接口 |

---

## 日常使用方式

1. 从任意设备 `ssh root@你的域名` 进入服务器（手机用 Termius，电脑用终端）
2. 进项目目录跑 `claude`，**把需求、想法、报错都先讲给它**——它负责拆解任务、决定要不要分派、以及审查其他模型的产出
3. 实施类的活：`model-router --type implementation --cwd <项目目录> "..."`
4. 琐碎的活：`model-router --type basic "..."`，不占用订阅额度
5. 想盯着进度看：浏览器打开 `https://你的域名/?token=<token>`
6. 对外同步：`lark-cli` 发飞书、`gh pr create` 推代码、或把网页链接直接发出去

### 一个必须养成的习惯：每个项目放一份 `PROJECT_STATUS.md`

Claude Code、Codex、DeepSeek 各自的会话历史存储格式互不相通，**谁也读不到谁的对话记录**。磁盘上的一份文件是唯一能跨工具共享的“记忆”。

写什么：项目是干什么的、核心文件、怎么跑，以及**不显而易见的决策及其原因**（为什么选 A 不选 B，哪些坑踩过了）。重要决策一做完就更新。

`model-router --cwd` 模式下，这份文件会被**自动读进模型上下文**，不用每次重新交代背景。

---

## 安全与维护

### 凭据

- 所有 API key 只放 `.env`，`chmod 600`，`.gitignore` 必须排除。别贴进聊天窗口（贴过的就当已泄露，找机会轮换）
- 飞书 App Secret 由 `lark-cli` 加密存在 `~/.local/share/lark-cli/`，不要手动拷贝该目录
- 网页密码定期轮换；换完同步更新 Caddyfile 哈希并 `systemctl reload caddy`。文档站和 dsh 用两套独立凭据

### DeepSeek Harness 的风险定位

官方标注 **developer preview**，SAFETY.md 原话是“未经安全审计，不保证隔离”。实测默认姿态合理（`workspace-write` 沙盒 + 无人批准时自动拒绝提权操作），但它对使用者是黑盒，验证程度远不如 Model Router 自己的 bwrap 沙盒。**涉及敏感代码库时谨慎使用，或用 `backends.json` 把 deepseek 的 `runner` 切回 `api`。**

### ⚠️ 不要配置自动更新

2026-09-23 的实际教训：把 dsh 从 `0.1.5-rc.1` 升到 npm 的 `latest`（`0.1.5-rc.2`），服务立刻崩溃反复重启，报 `plugin(s) failed to load: @deepseek-ai/dsh-sandbox-local`。**而且回滚到原版本也修不好**——之前能运行纯粹是因为历次 npm 升级残留了一个旧版插件包，做干净重装后这个"垫脚石"消失，问题才暴露。

最终恢复方式：把 `~/.dsh/profiles` 整个移开让 dsh 重建（会话记录、凭据、storages 是分开存的，不受影响），再把 CLI 版本对齐到重建后 profile 实际拉取的插件版本（`0.1.5-rc.3`）。

**结论：核心 CLI 版本和 profile 插件版本必须一致；升级要手动、要挑时间、升完立刻跑验证清单。** dsh 明言会有不兼容变更，一次 patch 级更新就能让服务起不来。

### 服务维护

```bash
systemctl status dsh-web          # 服务状态
journalctl -u dsh-web -f          # 实时日志
systemctl restart dsh-web         # 改配置后重启（重启后 token 会变）
tail -f /root/projects/ai-model-router/logs/router.jsonl   # 调度/工具执行审计日志（已脱敏）
```

### 其它

- 每次改完 `router.py`，先跑 `python3 -m unittest test_router test_sandbox` 再用
- Codex 的额度检测是**被动的**（报错才知道），OpenAI 不提供订阅额度查询接口——这是已知限制，不是 bug

---

## 已知限制

以下是代码审查中提出、但判断为当前阶段不值得做的（单人/小团队内部工具，避免过度工程）：

- 没有拆包成正式项目结构，单文件够用
- 没有健康检查/熔断、成本统计、流式输出、`--dry-run`
- `run_shell` 沙盒未做 CPU/内存限制，只有超时兜底
- `snapshot_dir()` 是防“半成品被二次修改”的保护，**不是防篡改审计**
- Codex 配额只能被动检测，无法提前预警
- dsh 网页界面的 token 每次重启变化，且无固定 token 的配置项

更详细的演进记录、每一轮代码审查的问题与修复，见 [`PROJECT_STATUS.md`](PROJECT_STATUS.md)。
