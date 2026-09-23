# AI Model Router — 项目状态交接文档

给任何后续接手的工具/会话看（Codex / DeepSeek / Qwen / 新开的 Claude 会话）。
这份文件是唯一能跨工具读取的"记忆"——请在做出重要决策后更新它。
`--cwd` 模式下 DeepSeek/Qwen 会自动把这份文件读进上下文，不用额外交代背景。

## 这是什么

一个任务分发脚本，让 Claude 在对话中做架构设计，把"实施类"和"基础类"子任务
自动派发给 Codex / DeepSeek / Qwen3-Max，Codex 额度不足时自动降级。
定位：**Claude 和 Codex 都不可用时的兜底手段**，所以安全边界和失败语义的优先级
高于功能扩展。

## 核心文件

- `router.py` — 主脚本，全局命令名 `model-router`（软链接在 `/usr/local/bin/model-router`）
- `.env` — DeepSeek / Qwen 的 API key（chmod 600，不要提交到 git，`.gitignore` 已排除）
- `backends.json` — **可选**，不存在就用内置默认值。用来增删 OpenAI-compatible 后端和降级链，
  不用改代码（见下方"扩展后端"）
- `test_router.py` — 离线回归测试（9 个独立 unittest，用 subTest 覆盖多种坏配置），
  `python3 -m unittest -v test_router` 直接跑，全过退出码 0。
  不依赖 pytest，不调用任何付费 API（模型响应用桩回放），也不往项目目录写任何文件
  （`backends.json` 用例走临时目录），所以只读环境里也能跑。**改完 router.py 请先跑它。**
- `logs/router.jsonl` — 每次调用/每次工具执行的日志（已对 API key 脱敏，chmod 600）

## 用法

```
model-router --type architecture --cwd <目录> "设计问题"       # Claude(Opus5)只读做设计，无降级
model-router --type implementation --cwd <目录> "任务描述"   # 优先 codex，失败降级 deepseek/qwen
model-router --type basic "任务描述"                          # 纯文本问答，无文件权限
model-router --type basic --cwd <目录> "任务描述"              # 也能读写文件+跑命令
model-router --check-sandbox                                  # 只探测沙盒是否真的可用，不调模型
```

`--cwd` 目录必须已存在（脚本不会自动创建，避免路径打错导致意外新建目录）。

传 `--cwd` 时，**DeepSeek 默认把任务交给本机 dsh harness 的 headless 模式执行**
（`dsh --profile headless "任务"`，一次性 agent 会话；dsh CLI 不在 PATH 时自动退回自研工具循环）。
Qwen / runner=api 的 DeepSeek 仍走自研循环：拿到 `list_dir`/`read_file`/`write_file`/`run_shell`/`task_done`
工具自主执行，**必须显式调用 `task_done` 才算完成**（纯文字回复会被要求重新执行）。
纯文字问答（无 `--cwd`）不启动 harness，始终直连 API。

`task_done` 的准入规则（三层，都由 `run_agent_loop` 强制）：
1. 同一轮里有任何工具失败就拒绝，**无论 `task_done` 排在失败工具之前还是之后**（两趟处理）。
2. 错误状态**跨轮保留**：第一轮跑挂测试、第二轮光调 `task_done` 同样会被拒。
3. 解除条件是"**证明原来那个失败已经解决**"，不是"后来有别的调用成功了"：
   - shell 失败 → 必须**重新成功跑通同一个程序**（比对命令的第一个程序名，`pytest` 挂了
     就得再跑通 `pytest`）。跑一句 `true`/`echo` 蒙混不过去。
   - 其他工具错误 → 必须**重新成功调用同一个工具**（`read_file` 失败就得再成功 `read_file`）。
   - 沙盒禁用导致的 `run_shell` 拒绝也**跨轮保留**（任务毕竟没被验证过），但可以被任意一次
     真实的工具调用成功解除——只有裸调 `task_done` 解除不了。
   防死锁的兜底：程序名解析不出来、或失败的是未知工具名时，退回"任意一次成功即可解除"。

## 扩展后端（不用改代码）

在项目目录放 `backends.json`，会覆盖/追加内置的 `DEFAULT_BACKENDS` / `DEFAULT_CHAINS`：

```json
{
  "backends": {
    "glm": {"base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model": "glm-4.6", "key_env": "GLM_API_KEY"}
  },
  "chains": {"basic": ["deepseek", "glm", "qwen"]}
}
```

key 从 `.env` / 环境变量里按 `key_env` 取；可选的 `base_url_env` 允许再用环境变量覆盖 base_url。
`codex` 是特殊后端（本地 CLI，自带沙盒），链里可以用但不需要在 `backends` 里定义。
配置解析失败或链里引用了未定义的后端时，会打警告并退回默认值，不会中断任务。

可选的 `runner` 字段（`dsh`/`api`）决定某个后端在 `--cwd` 自主任务里用什么执行器：
`dsh` = 本机 DeepSeek Harness 的 headless 模式（deepseek 的默认值，见下方"DeepSeek 接 dsh headless"），
`api` = 本文件自研的 OpenAI 工具循环（qwen 等其余后端默认）。`dsh` 是"接法"不是独立后端——
链上名字仍是 deepseek，dsh CLI 不在 PATH 时自动退回 `api`。已有内置后端支持只改个别字段的
部分覆盖（如只写 `{"deepseek": {"runner": "api"}}`），不必整份重发 spec。

## 安全边界（2026-09-03 加固后）

- `run_shell` 通过 **bwrap 沙盒**执行，只读暴露程序/动态库目录（`/usr /bin /sbin /lib*`）
  和明确列举的公共系统数据（证书、用户名称解析、时区、动态链接缓存等），不再整体暴露
  `/etc`、`/var`、`/opt`；只有 `--cwd` 目录可写，
  `/tmp` 和 `HOME` 用隔离的 tmpfs，网络默认放行（pip/curl 等联网任务能正常跑）。
  **`/root`、`/home`、`/srv`、`/mnt` 在沙盒里根本不存在**——早期版本用 `--ro-bind / /` 把整个
  宿主挂进去，模型能读到工作目录外的 `.env`、`~/.ssh`、云凭据再借网络发出去，边界比路径工具
  宽得多。采用“只暴露所需文件”而不是“挂载大目录后列举敏感热点”的方式，避免遗漏未知凭据；
  当工作目录就是本项目时，router 自己的 `.env` 会被 `/dev/null` 盖掉。
- **环境变量全部清空**（`--clearenv`），只重新注入 `PATH`/`HOME`/`TERM`/`LANG`/`TZ`。
  否则即使 `.env` 文件被遮蔽，预先 `export` 的 `DEEPSEEK_API_KEY`、`AWS_*`、`GITHUB_TOKEN`、
  带认证信息的 `*_PROXY` 仍能被 `env` 读出来再借网络发走。无沙盒退化路径同样只给最小环境。
- 同一次 agent 循环共用一个沙盒状态目录：`HOME` 和 `/tmp` 在多次 `run_shell` 之间**保持**
  （`pip install --user`、npm/cargo 配置、上一条命令的中间产物不会丢），循环结束即删除。
- `--shell-no-network` 可以让沙盒内断网（`--unshare-net`），处理敏感代码时进一步收紧。
  默认放行网络是因为大量实施任务要 pip/apt/curl。
- **沙盒可用性靠真实探测判定**，不是只看 `which bwrap`：启动时在临时目录里真跑一次
  `bwrap ... printf ok > probe.txt`，确认能建 namespace 且绑定目录可写（结果按进程缓存一次）。
  探测失败时 **`run_shell` 直接禁用并返回 ERROR**，路径类工具不受影响，system prompt 里也会
  告诉模型别硬试。要在无沙盒环境执行命令，必须由用户显式加 `--allow-unsandboxed-shell`
  主动授权，绝不静默降级。
- `read_file`/`write_file`/`list_dir` 用路径解析校验(`_safe_path`)防止 `../` 越权，独立于 bwrap。
- `write_file` 原子写入（先写临时文件再 `os.replace`），避免中断留下残缺文件。
- 日志写入前做 API key 脱敏（正则匹配 `sk-*`/`Bearer *`），文件权限 600。

## 故障转移的安全语义

- **任何后端执行到一半失败、且已经修改过文件**：**不会**自动降级给下一个模型继续改
  （通过执行前后对工作目录做文件快照对比判断）。会直接停止并提示人工检查，避免两个模型
  交叉修改同一批文件造成叠加损坏。统一走 `_halt_on_partial_changes()`。
- 这条保护**覆盖 codex 和所有 agentic 后端**（DeepSeek/Qwen 及 backends.json 里新增的）。
  早期版本只在 codex 前后做快照，DeepSeek 写了半成品后 Qwen 仍会接着改，正好重现这条保护
  本来要避免的场景。
- 只有"失败但没有改动任何文件"时才会自动降级到下一个后端。
- Codex 配额检测是被动的：OpenAI 不提供订阅剩余额度查询接口，只能在调用报错
  （限流/配额耗尽关键词）时才切换。用户已确认接受这个限制。

## 已知限制（仍未修，有意跳过）

以下是代码审查中提出但判断为"当前阶段不值得做"的项目（单人内部工具，避免过度工程）：
- 没有拆包成 `cli.py`/`backends/`/`tests/` 这种正式项目结构，单文件够用。
- 没有健康检查/熔断、成本统计、流式输出、`--dry-run`。（后端配置已在 2026-09-03
  第三轮做成 `backends.json`，见"扩展后端"。）
- `run_shell` 沙盒未做 CPU/内存限制，只有超时兜底。
- `snapshot_dir()` 是防"半成品被二次修改"的保护，**不是防篡改审计**：存心构造同类型/同权限/
  同大小/同纳秒 mtime 的替换仍能绕过。另外刻意跳过 `.git` 和 `__pycache__`——跑一次
  `git status` 或任何 python 都会碰它们，纳进来会把正常操作误判成"改过文件"而白白中断降级链。
- Qwen 的 key 前缀是 `sk-ws-`（不是常见的 DashScope `sk-` 格式），目前用
  `https://dashscope.aliyuncs.com/compatible-mode/v1` + `qwen3-max` 验证可用，如果以后失效，
  优先检查是否需要换 base_url（`.env` 里可加 `QWEN_BASE_URL` 覆盖）。

## Codex 复评意见的处理结果（2026-09-03，已全部落地）

Codex 二次复评提出 P0×2 / P1 / P2 共四项，Claude 已全部修复并验证。原始意见的完整描述见
git 历史或本节各条的"原问题"。

### P0-1：bwrap 存在但不能创建 namespace —— 已修（采纳"安全优先"+显式授权）

- 原问题：代码只用 `shutil.which("bwrap")` 判断沙盒可用性。Codex 所在的受限上下文里
  `/usr/bin/bwrap` 存在，实际执行却报 `Creating new namespace failed: Operation not permitted`，
  于是每个 `run_shell` 都失败，还进不了"无 bwrap"的警告退化路径。
- 修法：新增 `bwrap_status()` 做**真实 capability probe**（临时目录里真跑一次带 `--bind` 的写入，
  结果按进程缓存）。探测失败 -> `run_shell` 直接禁用返回 ERROR，并在 system prompt 里告诉模型
  不要硬试；要无沙盒执行必须由用户显式加 `--allow-unsandboxed-shell`（同时打印越权警告）。
  即 Codex 的方案 1 和 2 合并：默认安全，逃生口需要人主动开。
- 另加 `--check-sandbox` 诊断开关，不调模型就能确认当前上下文的沙盒状态。
- 关于"原文档验证记录可能来自不同权限上下文"：确认属实。Claude 会话里 probe 通过，
  Codex 的上下文里不通过——**这正是不能靠 `which` 判断的证据**，现在两种环境都能自洽。

### P0-2：shell 非零退出码不阻止 task_done —— 已修

- 原问题：`run_shell` 对任意退出码都返回普通结果，agent 循环靠 `result.startswith("ERROR")`
  判断失败，所以测试/构建返回 1 后模型仍能在同一批 `task_done`，路由器报成功。
- 修法：非零退出码返回 `ERROR: 命令以非零退出码结束\nexit_code=N ...`。工具描述里明确要求
  "预期内的非零退出请在命令里自行处理（`... || true`）"，而不是由路由器默认当成功。
- 顺带修掉一个反向误判：`execute_tool` 改为返回 `(ok: bool, text)`，成功与否不再靠字符串
  前缀猜——否则一个内容恰好以 `ERROR` 开头的文件会被误判成工具失败。

### P1：同批 task_done 顺序漏洞 —— 已修

- 原问题：逐个处理 tool calls，模型只要把 `task_done` 排在失败工具前面就能骗到"成功"。
- 修法：改成两趟。第一趟跑完这一批所有非 `task_done` 调用，第二趟才判定 `task_done`——
  整批只要有一个失败就 REJECTED。tool 消息仍按原始顺序回填，保证每个 `tool_call_id` 都有应答。

### P2：不是通用可配置 API harness —— 已修（轻量版）

- 修法：抽出 `DEFAULT_BACKENDS` / `DEFAULT_CHAINS`，并支持可选的 `backends.json` 覆盖/追加，
  用法见上方"扩展后端"。现在加 Moonshot / GLM / OpenRouter / SiliconFlow 只要写配置 + 加 key，
  不用改代码。用 JSON 而非 TOML：标准库直接支持，不引入依赖。
- **有意没做**的部分（符合"单人内部工具"定位）：没有做健康检查/熔断/成本统计；
  `architecture` 仍然没有降级链（这是刻意的，见"关键决策记录"第 1 条）。
- 关于 codex-cli 的 `app-server`/`exec-server`/MCP 和 `--oss --local-provider`：确认这些是
  外部 harness 接口和本地推理入口，**不是额外的免费云模型额度**，接入价值低，暂不做。

### 本轮验证方式（第二轮意见）

- 离线用例（monkeypatch `_post_chat`，不花钱）14 项全过：非零退出码、ERROR 开头文件内容不误判、
  `../` 逃逸拒绝、沙盒禁用/显式授权两条路径、抢跑 task_done 被拒且顺序正确、backends 配置加载。
- 真实沙盒越权测试：工作目录内可写，向 `/root/` 写入被 bwrap 拦成 `Read-only file system`。
- 真实端到端：`--type basic --cwd` 走 DeepSeek agent 循环建文件 + run_shell 验证 + task_done，
  退出码 0，文件内容正确。
- `backends.json` 覆盖测试：追加 glm 后端、改写 basic 链生效；引用不存在后端的链被拒并回退默认。

## Codex 第三轮复审意见的处理结果（2026-09-03，已全部落地）

Codex 在我上一轮修复后又复审了一遍，提出 4 项（3×P1 + 1×P2）。**注意**：它当时尝试通过
`router.py` 把修复任务推给 Claude Opus 自动实施，但 Opus 服务端连续两次约 164 秒超时，
没有产生任何改动——所以那一轮的意见一直悬着，是本轮才真正修的。

### P1-1：工具失败只挡当批，挡不住下一轮直接宣布完成 —— 已修

- 原问题：`batch_had_error` 每轮重置。模型第一轮跑挂测试，第二轮只调 `task_done` 就被接受。
  Codex 用离线桩复现出 `(True, 'done despite unresolved failure')`。
- 修法：改成跨轮的 `unresolved` 状态，并采纳 Codex 建议里更稳妥的那档——shell 失败必须
  **再成功跑通一条 `run_shell`** 才解除，不是随便一次成功调用就算。详见"用法"里的三层准入规则。
- 额外处理了 Codex 没提到的死锁风险：沙盒禁用时 `run_shell` 的拒绝属于"策略拒绝"而非任务失败，
  如果也跨轮累积，`run_shell` 永远跑不通 → 任务永远无法完成。所以 `execute_tool` 现在返回
  失败**种类**（`shell_failed` / `error` / `refused`），三者卡的严格程度不同。

### P1-2："改过文件禁止降级"只保护 codex —— 已修

- 原问题：只在 codex 前后做快照。DeepSeek 写入半成品后超时，Qwen 仍会接着改，
  正好重现该保护要避免的交叉修改。
- 修法：抽出 `_halt_on_partial_changes()`，对**每个 agentic 后端**统一做执行前后快照，
  失败且有改动就停止整条链。

### P1-3：沙盒限制了写入，却能读取整个宿主 —— 已修（本轮最实质的一项）

- 原问题：`--ro-bind / /` 把整个宿主挂进沙盒，加上放行的网络，模型可以读工作目录外的
  `.env`、`~/.ssh`、云凭据并发出去——边界比路径工具的"只能碰 cwd"宽得多。
- 修法：换成系统目录**白名单**（`SANDBOX_RO_DIRS`），`/root`/`/home`/`/srv`/`/mnt` 在沙盒里
  直接不存在；白名单内再遮蔽 `/etc/ssh`、`/etc/shadow`、`/var/log` 等；工作目录是本项目时
  单独用 `/dev/null` 盖掉 router 自己的 `.env`；HOME 指向 tmpfs。
- 网络仍默认放行（大量任务要 pip/apt/curl），但新增 `--shell-no-network` 可以断网。
  取舍理由：凭据已经读不到了，剩下能外传的只有 cwd 内容——那本来就是模型在正常处理的东西。
- 踩到的坑：白名单化后 DNS 断了，因为 `/etc/resolv.conf` 是指向 `/run/systemd/resolve/...`
  的符号链接而 `/run` 不在白名单。解法是把符号链接指向的**真实文件**绑进沙盒
  （绑 `/etc/resolv.conf` 本身会因为是悬空链接而报 `Can't create file`）。

### P2：结构错误的 backends.json 会崩溃 —— 已修

- 原问题：`[]` 或 `{"backends": []}` 是合法 JSON 但结构不对，直接
  `AttributeError: 'list' object has no attribute 'get'`，和"解析失败就回退默认值"的承诺不符。
- 修法：逐层校验根节点/`backends`/`chains`/每个 spec 的类型，统一警告后回退默认配置。

### 本轮验证方式（第三轮意见）

- 新增 `test_router.py`（**30 项全过**，退出码 0，不调付费 API）。除上一轮的用例外，新增覆盖：
  跨轮 shell 失败挡住 task_done、只有成功的 run_shell 能解除、普通错误不死锁、
  策略拒绝不死锁、沙盒看不到 `/root`、python/网络仍可用、`--shell-no-network` 生效、
  5 种结构错误的 backends.json 全部回退默认、改文件后失败停止降级且下一后端未被调用、
  未改文件时仍正常降级。
- 真实端到端：`--type basic --cwd` 让 DeepSeek 写 `calc.py` + `test_calc.py`，
  跑 `python3 test_calc.py` 验证通过后 task_done，退出码 0——确认收紧沙盒没有破坏正常任务。
- 沙盒能力实测：python 3.14 / pip 25.1.1 / git 2.53 / curl+HTTPS / DNS 全部可用；
  `/root`、`/root/.ssh`、`.env`、`/etc/shadow` 全部读不到。

## 关键决策记录（why）

1. Claude 额度用完 ≠ 本脚本能处理的范围：Claude Code 本身没有切换到其他模型的机制，这是产品
   边界。Claude 不可用时，用户需要手动切到 `codex`（交互式）或直接调用本脚本。
2. **已补上**（2026-09-03）：`--type architecture` 会调用 `claude -p --permission-mode plan`
   做只读架构咨询（不改任何文件），没有降级链——这本来就是"Claude 不可用时没人能替代"的部分。
   模型常量是 `FABLE_MODEL`（`router.py` 顶部）：**理想是 `claude-fable-5`，但当前账号订阅
   不含 Fable 5**，实测调用报错 `Fable 5 requires usage credits`。
   2026-09-03 查证：**Max 计划把 Fable 5 作为标准包含项**，可用掉每周额度的 50% 跑 Fable
   而不额外收费，超过 50% 后才需要 usage credits 或换模型；Pro 计划则一直要 credits。
   所以升级到 Max 之后把这一行改成 `"claude-fable-5"` 即可切换，不用改别处
   （另需 Claude Code ≥ 2.1.170）。用户当前先用 `claude-opus-5`。
3. API key 一律只写进 `.env`（chmod 600，已在 `.gitignore` 排除），不要贴进任何对话窗口或
   提交到版本库——贴出去过的 key 就按已泄露处理，尽快轮换。

## 更新记录

- 2026-09-03 初版：搭建 codex/deepseek/qwen 三条链路；升级 deepseek/qwen 为带工具调用的自主
  agent；建立本交接文档。
- 2026-09-03 加固：用户请 codex 做了一轮代码审查（原始内容见 `~/.codex/sessions/2026/09/03/
  rollout-2026-09-03T12-07-16-*.jsonl`，该轮因用户 `exit` 中断未真正写入本文档，由 Claude 事后
  从 session 日志中提取）。据此修复：run_shell 真沙盒(bwrap)、codex 半途失败禁止自动降级
  （文件快照对比）、task_done 强制显式确认+同批错误拒绝完成、日志脱敏、API 响应结构校验
  （避免 KeyError 崩溃）、write_file 原子写入、--cwd 不存在不再静默创建、agent 模式自动读取
  本文档作为上下文。跳过了 codex 建议里偏"生产化"的部分（见上方"已知限制"）。全部改动均用
  真实任务验证通过（bwrap 越权拦截、PROJECT_STATUS.md 自动注入、codex 正常链路回归测试）。
- 2026-09-03 补架构环节：新增 `--type architecture`，调用 `claude -p --permission-mode plan`
  做只读设计咨询，无降级链。发现当前账号订阅不含 Fable 5（需额外 credits），暂用 Opus 5，
  已用真实任务验证（分析本项目自身，只读未改动任何文件）。
- 2026-09-03 Codex 二次复评：没有改运行代码；记录当前权限上下文下 bwrap 虽存在但不可用、
  shell 非零退出码未进入 ERROR 协议、同批 task_done 顺序漏洞，以及通用 API/harness 的现状，
  供 Claude 下一轮提出意见和决定修复范围。
- 2026-09-03 Claude 处理 Codex 复评意见：四项全部修复——bwrap 真实 capability probe +
  探测失败禁用 run_shell + `--allow-unsandboxed-shell` 显式授权 + `--check-sandbox` 诊断；
  shell 非零退出码归为 ERROR；`execute_tool` 改返回 `(ok, text)` 消除字符串前缀误判；
  task_done 改两趟处理堵住顺序漏洞；后端抽成 `DEFAULT_BACKENDS`/`DEFAULT_CHAINS` +
  可选 `backends.json`。验证见"Codex 复评意见的处理结果 / 本轮验证方式"。
- 2026-09-03 Codex 第三轮复审 + Claude 修复：Codex 提出 4 项（跨轮错误状态、降级保护只覆盖
  codex、沙盒可读整个宿主、backends.json 结构校验），并尝试用本路由器把修复推给 Opus 自动
  实施——Opus 服务端两次 164 秒超时未产生改动。随后由 Claude 会话直接修完四项，并新增
  `test_router.py` 离线回归套件（30 项）。至此"没有自动化测试"这条已知限制不再成立。
- 2026-09-03 Codex 第四轮复审 + Claude 修复：Codex 判定上一轮 4 项里 3 项只部分成立，
  另提 3×P1+3×P2（沙盒继承环境变量致凭据外传、/etc·/var·/opt 遮蔽不足、unresolved 可被无关
  命令解除、refused 不跨轮、快照漏检、HOME 每次重建）。逐条实测复现确认属实后全部修复。
  过程中 codex 违反只读约束直接改代码并与 Claude 并发冲突（见下方"过程问题"），最终把
  测试拆成 `test_router.py`（纯逻辑 9 项）+ `test_sandbox.py`（真实执行围栏 9 项），
  沙盒收敛成白名单方案；期间也修掉一个测试自身的 monkeypatch 未还原导致的假通过。
- 2026-09-03 Claude 文档清理：发现上面"第四轮"小节因并发编辑被完整重复粘贴了一次
  （且残留旧版"30/53 项"的过时测试数字，与实测的 18 项矛盾），已删除重复段落，只保留
  下方与"Codex 最终修正"一致的版本。
- 2026-09-03 DeepSeek 接 dsh headless：deepseek 后端新增可选 `runner` 字段（默认 `dsh`），
  `--cwd` 自主任务改由本机 `dsh --profile headless` 执行，dsh CLI 缺失自动退回自研 API 循环；
  `backends.json` 可覆盖为 `api`，且内置后端支持"只改个别字段"的部分覆盖。详见下方对应小节。
- 2026-09-03 Claude 复审 + 修复：dsh 路径没有把 PROJECT_STATUS.md 传给模型，"自动读交接文档"
  的承诺对默认 runner 已经不成立（详见对应小节）。修法是拼进任务文本前面再传给 dsh；
  新增 2 项测试，真实复测（禁止模型翻文件的场景）确认修复生效。

## DeepSeek 接 dsh headless（2026-09-03）

背景：本机装了 DeepSeek Harness（`dsh --profile headless "任务"` 一次性 agent 会话，最后回合
正常结束退出码 0、最终文本打 stdout，出错才写 stderr；凭据/端点与 router 自研直连一致，都读
`DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`）。harness 自带完整 agent 工具与沙盒，router 给 DeepSeek
自研的 bwrap 工具循环（AGENT_TOOLS / task_done）在它面前是重复劳动，且 harness 的 agent 能力更强
（子任务、todo 等）。于是把 DeepSeek 的执行器从"直连 API"换成"本地 dsh harness"。

- 接法：deepseek 后端在 `DEFAULT_BACKENDS` 里带 `"runner": "dsh"`；`--cwd` 自主任务走
  `run_dsh_headless()`（subprocess 调 `dsh --profile headless`，工作区=--cwd，env=load_env()）。
  纯文字问答（无 `--cwd`）不启动 harness，始终直连 API——启动整套 agent 开销大，也没必要把文件系统
  暴露给纯问答任务。
- 失败语义（都在原 deepseek 槽位内）：
  - dsh CLI 不在 PATH → 自动退回 runner=api（自研循环，旧行为），只记一条 `dsh->api` 日志；
  - 缺 `DEEPSEEK_API_KEY` → 不白启动 harness，该槽位直接按失败降级 qwen；
  - dsh 跑失败且改过文件 → 与 codex/api 一样触发 `_halt_on_partial_changes()` 停掉整条链
    （前后快照对比，防止 Qwen 在 harness 的半成品上交叉修改）。
- 配置：`backends.json` 里 `{"deepseek": {"runner": "api"}}` 即可强制直连。为支持这种只改个别字段的
  用法，`load_backends()` 把"spec 必须先自带宽 base_url/model"改成"先合并到默认值上再校验"——
  对已有后端只写 `{"runner": ...}`（或只改 base_url）也生效；全新后端仍要求 base_url/model 齐备。
  runner 只接受 `api`/`dsh`，非法值警告后回默认。
- 已知限制（有意保持）：runner=dsh 时 `spec.model` 不生效——模型由 harness 自己的
  agent-default-model 决定（默认 deepseek-v4-flash，可在 `$DSH_HOME/settings.yaml` 或 profile
  `--patch` 里改）；`--shell-no-network` / `--allow-unsandboxed-shell` 只作用于 runner=api 的自研
  循环，dsh 后端忽略它们（会往 stderr 打一句提醒）。
- 验证：`test_router.py` 从 9 项扩到 **17 项全过**——新增 runner 默认值、部分覆盖+非法值校验、
  dsh 缺失退回 API 循环、dsh 在场改走 run_dsh_headless、纯问答仍走 API、dsh 半途改文件停止降级、
  缺 key 不启动 harness 直接降级 qwen、wrapper 的成功/非零退出/CLI 缺失/超时四条路径。
  `test_sandbox.py`（9 项）在本会话上下文照常按"bwrap 不可用"自动 skip，宿主上仍会真实执行。
  真实冒烟：`run_dsh_headless` 直接跑通（harness 内建文件+验证、退出码 0），完整 `dispatch` 走
  `--type basic --cwd` 也端到端通过（deepseek runner=dsh，日志记录 runner: dsh）。

### Claude 复审发现并修复：PROJECT_STATUS.md 没有传给 dsh 路径 —— 已修

- **原问题**：本文档开头承诺"`--cwd` 模式下 DeepSeek/Qwen 会自动把这份文件读进上下文"，但
  `run_dsh_headless()` 把 prompt 原样传给 `dsh --profile headless`，完全没调用负责这件事的
  `_load_status_doc()`（全文件只有 `run_agent_loop` 一处调用）。dsh 是个会主动探索文件系统的
  agent，小项目里"顺手"读到状态文件容易被误当成"没问题"，但这只是运气不是保证——**实测复现**：
  在测试目录里堆 15 个无关文件、并在提示词里明确要求"不要读文件、不要列目录"，DeepSeek 就
  答不出状态文件里预埋的暗号了。
- 修法：`run_dsh_headless` 内部调用 `_load_status_doc(cwd)`，有内容就拼在任务文本最前面
  （用 `---` 分隔），再整体传给 dsh——dsh 只吃一整段任务文本，没有独立的 system 消息通道，
  所以只能这样拼，不能像 `run_agent_loop` 那样单独发一条 system 消息。
- 验证：新增 2 项测试（`test_router.py` 共 **19 项全过**）——有状态文件时任务文本前面是背景、
  后面是原始任务，顺序正确；没有状态文件时任务文本原样不变。真实复测：同一个"15 个无关文件 +
  明确禁止翻文件"的场景，修复后正确答出暗号，证明这次是靠注入拿到的，不再是碰运气。

## Codex 第四轮复审意见的处理结果（2026-09-03，已全部落地）

Codex 复审了第三轮修复，判定 4 项里第 4 项成立，第 1/2/3 项只部分成立，另提出 3×P1 + 3×P2。
我逐条实测复现后确认**全部属实**，已全部修复。

### P1-1：沙盒继承宿主环境变量，凭据仍可外传 —— 已修（本轮最严重）

- 原问题：`bwrap` 没用 `--clearenv`，`subprocess.run()` 也没传 `env`。即使 `.env` 文件被遮蔽，
  预先 `export` 的 `DEEPSEEK_API_KEY` / `AWS_*` / GitHub token / 带认证的 `*_PROXY`
  照样能被 `env` 读出来，再借放行的网络发走。
- **实测复现**：设 `SENTINEL_SECRET=sk-CANARY-...` 后在沙盒内 `echo $SENTINEL_SECRET`
  原样读到了值。
- 修法：`--clearenv` + 只重新注入 `PATH`/`HOME`/`TERM`/`LANG`/`TZ`（`_sandbox_env()`）。
  无沙盒退化路径也传同一份最小 env——凭据泄露和文件隔离是两件事，不能因为沙盒没了就都放弃。

### P1-2：`/etc`、`/var`、`/opt` 下仍有未遮蔽的凭据面 —— 已修（部分采纳）

- 按 Codex 给的清单扩充 `SANDBOX_MASK_DIRS/FILES`：`/etc/ssl/private`、`/etc/wireguard`、
  `/etc/openvpn`、`/etc/ipsec.d`、`/etc/NetworkManager/system-connections`、`/etc/docker`、
  `/etc/kubernetes`、`/var/lib/docker`、`/var/lib/kubelet`、`/var/lib/cloud`、`/var/lib/aws`、
  `/var/backups`、`/var/mail`、`/etc/krb5.keytab`、`/etc/machine-id`。
- **没采纳**"按需精确挂载 + 默认断网"：见"已知限制"里的取舍说明。

### P1-3：`unresolved` 能被无关的成功命令解除 —— 已修

- 原问题：`strict` 的实际条件只是"之后有任意成功的 `run_shell`"，所以
  `pytest`(失败) → `true`(成功) → `task_done` 就能通过。`loose` 更宽。
  Codex 还指出**我的测试把 `run_shell("true")` 写成了正确解除方式，等于把漏洞固化进了测试**——
  这条批评完全成立，测试已改。
- 修法：解除条件改成"证明**原来那个**失败已解决"。shell 失败记下程序名（命令第一个 token，
  会跳过 `FOO=bar` 前缀），必须重新成功跑通**同一个程序**；其他工具错误必须重新成功调用
  **同一个工具**。防死锁兜底：程序名解析不出或工具名不认识时，退回"任意一次成功即可解除"。

### P2-1：`refused` 只挡当批，下一轮可直接 `task_done` —— 已修

- **实测复现**：沙盒禁用下 `run_shell`(refused) → 下一轮裸调 `task_done` → 返回
  `(True, '没跑成命令就宣布完成')`。
- 修法：`refused` 也跨轮保留。可以被任意一次**真实的**工具调用成功解除（这样"改用文件工具
  完成任务"的路仍然走得通），但光调 `task_done` 解除不了。

### P2-2：快照漏检 —— 已修

- `snapshot_dir()` 从 `(size, mtime)` 扩到 `(类型, 权限, 大小, 纳秒 mtime, symlink 目标)`，
  并把每个目录的子项列表也纳入快照——这样空目录的增删、权限变更、文件被换成符号链接才可见。
  用 `os.lstat` 不跟随符号链接。
- `logs` 的排除改成按 router 自己的 `LOG_DIR` 绝对路径判断，而不是排除任何叫 `logs` 的目录。
- 仍**有意**跳过 `.git`/`__pycache__`，理由和"这不是防篡改审计"的定位写进了"已知限制"。

### P2-3：HOME 每次 `run_shell` 重建，多步构建丢状态 —— 已修

- **实测复现**：第一次调用写 `$HOME/.local/bin/mytool`，第二次调用读不到。
- 修法：每次 agent 循环建一个 `state_dir`，`HOME` 和 `/tmp` 都绑到它下面，循环结束
  `shutil.rmtree` 清理。这样 `pip install --user`、npm/cargo 配置、上一条命令的中间产物
  能跨 `run_shell` 保持，同时不同任务之间仍然互相隔离。
- 不传 `state_dir` 的独立调用仍是一次性 tmpfs，默认隔离性没有放松。

### 关于 Codex 对测试的批评

全部接受并已修：
- 加了哨兵 secret 测环境变量继承；加了白名单内部敏感目录（`/etc/ssl/private`、`/var/backups`）的遮蔽断言。
- 把 `run_shell("true")` 从"正确解除方式"改成"**不该**解除"的反例。
- 补了 `refused → 下一轮裸调 task_done` 的绕过路径。
- 快照用例补了空目录、权限、符号链接三种变化。
- `backends.json` 用例改用临时目录，不再往项目目录写文件——这正是 Codex 在只读模式下
  跑不了测试的原因。

另外自查发现一个**测试污染**：P1-2 用例 monkeypatch 了 `router.run_agent_loop` 却没还原，
导致其后的用例调到桩上假通过。已加还原，这也是本轮新用例第一次跑出 FAIL 才暴露的。

### 本轮验证方式（第四轮意见）

- `test_router.py`（纯逻辑，9 项）+ `test_sandbox.py`（真实执行围栏，9 项）**全过**。
- 沙盒实测：哨兵 secret 与 AWS 凭据变量均读不到、`PATH` 保留、
  `/root /home /var /opt /run /srv /etc/shadow /etc/ssh` 全部不可见、
  工作目录是本项目时 `.env` 读出 0 字节、写工作目录外被拒；
  同时 python/pip/git/gcc/make/DNS/HTTPS+TLS 全部可用，
  HOME 与 `/tmp` 跨 `run_shell` 保持而无 `state_dir` 时仍是一次性。
- 真实端到端：`--type basic --cwd` 让 DeepSeek 写 `calc.py` + `test_calc.py` 并跑通验证，
  退出码 0——确认这一轮收紧没有把正常任务卡死。

### ⚠️ 本轮的一个过程问题：codex 违反了只读约束

这轮复审是用 `codex exec --sandbox read-only` 启动的，提示词里也明确写了"不要修改任何文件"。
它交出审查意见后**没有退出**，而是继续直接改写了 `router.py` 和 `test_router.py`
（把黑名单换成白名单、把测试重写成 unittest），期间和 Claude 的编辑并发冲突，
一度造成 DNS 回归和测试文件被覆盖（53 项的版本丢失）。

处理：停掉进程、保留它改后的 `router.py` 副本、逐项评估后**保留了它的设计**
（白名单确实更好、unittest 版本确实更稳定），补回它引入的 DNS 回归，
并把丢失的真实执行测试重建为独立的 `test_sandbox.py`。

**教训**：下次让 codex 做纯审查时，除了提示词约束，还应该在只读副本目录上跑
（比如先 `cp -r` 到临时目录再 `-C` 指过去），不要让它直接指向正在编辑的工作目录。

## Codex 最终修正（2026-09-03）

- 沙盒不再整体暴露 `/etc`、`/var`、`/opt`；改为只挂载程序/动态库目录和明确列出的公共系统数据。
  这种 allowlist 不依赖持续猜测新的凭据热点。
- 将旧的环境相关脚本改成 9 个隔离的 `unittest`。shell 失败语义通过显式授权、仅在临时目录执行
  的本地 shell 测试；bwrap 本身用命令前缀结构断言验证，因此宿主禁止 user namespace 时也能可靠运行。
- 每项测试都把 `BACKENDS_FILE`、日志和工作目录指向临时目录，不覆盖或删除用户的
  `backends.json`，也不会调用模型或网络。
- 最终验证：`python3 -m py_compile router.py test_router.py` 与
  `python3 -m unittest -v test_router` 均通过（9 tests）。

## Claude/Codex 不可用时的 API 操作

项目已有 `.env` 时，直接使用 `basic` 类型即可跳过 Claude 和 Codex，优先调用 DeepSeek，失败再
降级 Qwen：

```bash
model-router --type basic "纯文字问题"
model-router --type basic --cwd /path/to/project "实施任务"
```

审查当前项目（只读意图写进提示词；模型仍会拥有路径工具）：

```bash
cd /root/projects/ai-model-router
model-router --type basic --cwd "$PWD" \
  "只做代码审查，不修改文件。阅读 router.py、test_router.py、PROJECT_STATUS.md；按 P0/P1/P2 列出有证据的问题、文件行号和复现方法。"
```

如果审查结论确认后要让 DeepSeek 修复，再单独运行：

```bash
model-router --type basic --cwd "$PWD" \
  "根据上一份审查逐项修复；运行 python3 -m py_compile router.py test_router.py 和 python3 -m unittest -v test_router，最后调用 task_done 总结。"
```

先用 `model-router --check-sandbox` 检查 shell 能力。若 bwrap 不可用，文件读写仍可用，但测试命令
会被拒绝；只有明确接受模型可写工作目录外路径的风险时才加 `--allow-unsandboxed-shell`。
