#!/usr/bin/env python3
"""
AI Model Router — 任务分发编排器

架构设计 / 复杂决策：由 Claude（对话中）直接完成，不经过本脚本。
本脚本负责"实施类"和"基础类"任务的自动分发与故障转移：

  implementation 任务：优先 codex exec 本地执行(订阅额度) -> 失败且未修改任何文件时
                        自动降级 DeepSeek -> 再降级 Qwen3-Max
  basic 任务      ：优先 DeepSeek(最便宜) -> 失败时降级 Qwen3-Max

DeepSeek 的 --cwd 自主执行**默认交给本机 dsh harness 的 headless 模式**
（`dsh --profile headless "任务"`）跑，不再走自研 API 工具循环：harness 自带更完整的
agent 能力与沙盒，凭据/端点同样读 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL，模型由 harness
自己决定（默认 deepseek-v4-flash）。dsh CLI 不在 PATH 时自动退回自研循环；
backends.json 里给 deepseek 写 "runner": "api" 可强制直连 API（见 run_dsh_headless）。

Qwen（以及 runner=api 的 DeepSeek）仍走自研工具循环：读文件/写文件/列目录/执行 shell
命令，直到显式调用 task_done 才算完成。shell 命令通过 bwrap 沙盒执行：文件系统写入被
限制在 --cwd 内，其余目录只读、/tmp 隔离，网络默认放行（联网型任务如 pip install 能正常
工作）。沙盒可用性靠**真实探测**判定（有些环境装了 bwrap 却禁止创建 namespace），探测
失败时 run_shell 直接禁用，除非用户显式加 --allow-unsandboxed-shell 授权无隔离执行。
--shell-no-network / --allow-unsandboxed-shell 只作用于自研循环，对 dsh runner 不生效
（dsh 自带沙盒，网络策略由其自身配置决定）。
若存在 PROJECT_STATUS.md，会自动读入作为背景上下文。

后端不是写死的：同目录放一个 backends.json 就能增删 OpenAI-compatible 后端和降级链，
详见 DEFAULT_BACKENDS 上方注释。

用法：
  python3 router.py --type implementation --cwd /path/to/project "任务描述"
  python3 router.py --type basic "写一个函数的任务描述"
  python3 router.py --type basic --cwd /path/to/project "改一下xxx文件"
  echo "长任务描述..." | python3 router.py --type implementation --cwd /path --stdin
  python3 router.py --check-sandbox        # 只探测沙盒是否真的可用

日志：每次调用/每次工具执行都追加写入
      /root/projects/ai-model-router/logs/router.jsonl（已脱敏 API key），方便事后审查产出质量。
"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.realpath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
BACKENDS_FILE = os.path.join(BASE_DIR, "backends.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "router.jsonl")

# OpenAI-compatible 后端表。要加第三、第四个 API（Moonshot / GLM / OpenRouter / SiliconFlow…），
# 在同目录放一个 backends.json 覆盖/追加即可，不用改代码：
#   {"backends": {"glm": {"base_url": "...", "model": "glm-4.6", "key_env": "GLM_API_KEY"}},
#    "chains":   {"basic": ["deepseek", "glm", "qwen"]}}
# 每个后端的 base_url 也能被 <KEY_ENV 去掉 _API_KEY>_BASE_URL 形式的环境变量覆盖（见 base_url_env）。
# 可选的 "runner" 字段选择 --cwd 自主执行用什么跑：deepseek 默认 "dsh"（本地 dsh harness，
# headless 模式），其余后端默认 "api"（本文件自研的 OpenAI 工具循环）；显式写 "api" 即强制直连。
DEFAULT_BACKENDS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "runner": "dsh",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3-max",
        "key_env": "QWEN_API_KEY",
        "base_url_env": "QWEN_BASE_URL",
        "runner": "api",
    },
}

# "codex" 是特殊后端（本地 CLI + 自带沙盒），其余名字都必须能在 backends 表里查到。
DEFAULT_CHAINS = {
    "implementation": ["codex", "deepseek", "qwen"],
    "basic": ["deepseek", "qwen"],
}

# 架构设计咨询用的 Claude 模型。理想是 claude-fable-5(最强)，但当前账号订阅不含 Fable 5，
# 需要额外购买 usage credits(claude.ai/settings/usage)才能用，用了会报
# "Fable 5 requires usage credits"。开通后把这行改成 "claude-fable-5" 即可切换，无需改别处。
FABLE_MODEL = "claude-opus-5"

CODEX_QUOTA_MARKERS = [
    "usage limit", "rate limit", "rate_limit", "429",
    "quota", "insufficient_quota", "too many requests",
    "exceeded your current", "resets at",
]

MAX_TOOL_ITERS = 25
HTTP_CALL_TIMEOUT = 120          # 每次 API 请求的超时
TOOL_SHELL_TIMEOUT = 120         # 每次 shell 命令的超时
MAX_FILE_BYTES = 300_000
STATUS_DOC_NAMES = ["PROJECT_STATUS.md", "README.md"]

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_.\-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_.\-]{8,}"),
]


def redact(text: str) -> str:
    if not text:
        return text
    for pat in _SECRET_PATTERNS:
        text = pat.sub("***REDACTED***", text)
    return text


def load_env():
    env = dict(os.environ)
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


def load_backends() -> tuple[dict, dict]:
    """返回 (backends, chains)。可选的 backends.json 会覆盖/追加默认表，读坏了就退回默认值。"""
    backends = {k: dict(v) for k, v in DEFAULT_BACKENDS.items()}
    chains = {k: list(v) for k, v in DEFAULT_CHAINS.items()}
    if not os.path.exists(BACKENDS_FILE):
        return backends, chains
    try:
        with open(BACKENDS_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"警告: 无法解析 {BACKENDS_FILE}（{e}），使用内置后端配置", file=sys.stderr)
        return backends, chains

    # 逐层校验类型：合法 JSON 不等于结构正确。`[]` 或 {"backends": []} 以前会直接
    # AttributeError 崩溃，和"解析失败就回退默认值"的承诺不符。
    if not isinstance(cfg, dict):
        print(f"警告: {BACKENDS_FILE} 根节点应为对象，实际是 {type(cfg).__name__}，使用内置后端配置",
              file=sys.stderr)
        return backends, chains

    raw_backends = cfg.get("backends", {})
    if not isinstance(raw_backends, dict):
        print(f"警告: backends 应为对象，实际是 {type(raw_backends).__name__}，忽略该段", file=sys.stderr)
        raw_backends = {}

    for name, spec in raw_backends.items():
        if not isinstance(spec, dict):
            print(f"警告: backends.'{name}' 应为对象，实际是 {type(spec).__name__}，已忽略", file=sys.stderr)
            continue
        # 先合并到已有默认值上：对内置后端做部分覆盖(如只改 runner)也成立；
        # 全新后端没有默认可合，下面仍要求 base_url/model 齐备才接受。
        merged = backends.get(name, {})
        merged.update(spec)
        if not isinstance(merged.get("base_url"), str) or not isinstance(merged.get("model"), str) \
                or not merged["base_url"] or not merged["model"]:
            print(f"警告: backends.'{name}' 的 base_url/model 缺失或不是非空字符串，已忽略", file=sys.stderr)
            continue
        key_env = merged.get("key_env")
        if not isinstance(key_env, str) or not key_env:
            merged["key_env"] = f"{name.upper()}_API_KEY"
        if not isinstance(merged.get("base_url_env", ""), str):
            merged.pop("base_url_env", None)
        if "runner" in merged and merged["runner"] not in ("api", "dsh"):
            print(f"警告: backends.'{name}' 的 runner 应为 api 或 dsh，实际是 {merged['runner']!r}，已回默认",
                  file=sys.stderr)
            merged["runner"] = "dsh" if name == "deepseek" else "api"
        backends[name] = merged

    raw_chains = cfg.get("chains", {})
    if not isinstance(raw_chains, dict):
        print(f"警告: chains 应为对象，实际是 {type(raw_chains).__name__}，忽略该段", file=sys.stderr)
        raw_chains = {}

    for task_type, chain in raw_chains.items():
        if not isinstance(chain, list) or not chain or not all(isinstance(b, str) for b in chain):
            print(f"警告: chains.{task_type} 应为非空字符串数组，该链保持默认", file=sys.stderr)
            continue
        unknown = [b for b in chain if b != "codex" and b not in backends]
        if unknown:
            print(f"警告: chains.{task_type} 里有未定义的后端 {unknown}，该链保持默认", file=sys.stderr)
            continue
        chains[task_type] = chain
    return backends, chains


def log_event(event: dict):
    os.makedirs(LOG_DIR, exist_ok=True)
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    for k, v in list(event.items()):
        if isinstance(v, str):
            event[k] = redact(v)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    os.chmod(LOG_FILE, 0o600)


def looks_like_quota_error(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in CODEX_QUOTA_MARKERS)


def snapshot_dir(root: str) -> dict:
    """给工作目录拍快照，用于判断某次执行是否改动过东西。

    记录每个条目的 (类型, 权限, 大小, 纳秒 mtime, symlink 目标)，外加每个目录的子项列表——
    只记 (size, mtime) 的话，空目录的增删、权限变更、文件被换成符号链接都看不见。

    刻意跳过 `.git` 和 `__pycache__`：跑一次 `git status` 或任何 python 都会碰它们，
    纳进来会把正常操作误判成"改过文件"从而白白中断降级链。router 自己的日志目录同理。
    这是个防"半成品被二次修改"的保护，不是防篡改审计：存心构造同大小同 mtime 的替换仍能绕过。
    """
    snap = {}
    log_dir_real = os.path.realpath(LOG_DIR)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                        if d not in (".git", "__pycache__")
                        and os.path.realpath(os.path.join(dirpath, d)) != log_dir_real]
        rel_dir = os.path.relpath(dirpath, root)
        # 目录项列表本身入快照：空目录的创建/删除、重命名才能被发现。
        snap["dir:" + rel_dir] = sorted(dirnames) + sorted(filenames)
        for fn in filenames + dirnames:
            p = os.path.join(dirpath, fn)
            try:
                st = os.lstat(p)          # lstat：不跟随符号链接，链接本身的变化才可见
                link = os.readlink(p) if stat.S_ISLNK(st.st_mode) else None
                snap[os.path.relpath(p, root)] = (
                    stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode),
                    st.st_size, st.st_mtime_ns, link,
                )
            except OSError:
                pass
    return snap


def run_codex(prompt: str, cwd: str | None, timeout: int) -> tuple[bool, str, str]:
    """Returns (success, output_text, raw_stderr)."""
    args = [
        "codex", "exec",
        "--sandbox", "workspace-write",
        "--skip-git-repo-check",
    ]
    if cwd:
        args += ["-C", cwd]
    args.append(prompt)

    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "", "codex exec timed out"
    except FileNotFoundError:
        return False, "", "codex CLI not found on PATH"

    combined = proc.stdout + "\n" + proc.stderr
    if proc.returncode == 0:
        return True, proc.stdout.strip(), proc.stderr
    return False, "", combined


def run_dsh_headless(prompt: str, cwd: str, timeout: int) -> tuple[bool, str]:
    """把 DeepSeek 的 --cwd 自主执行任务交给本地 dsh harness 的 headless 模式跑。

    `dsh --profile headless "任务"` 启动一个一次性 agent 会话：以调用目录为工作区，
    模型/凭据由 harness 自己解析（同样读 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL 环境变量，
    默认模型 deepseek-v4-flash，见 dsh-llm-deepseek）。最后一个回合正常结束 -> 退出码 0，
    最终助手文本打到 stdout；agent 出错则非零退出、原因写到 stderr。
    与 codex 一样，它是自带沙盒的本地 CLI：router 的 bwrap 工具循环和
    --allow-unsandboxed-shell / --shell-no-network 对它都不适用。

    dsh 只接受一整段任务文本，没有独立的 system 消息通道，所以 PROJECT_STATUS.md/README.md
    背景（`_load_status_doc`）像 run_agent_loop 一样自动读取后，直接拼在任务文本前面——
    不这样做的话，"--cwd 模式下 DeepSeek 会自动读交接文档"这条承诺对 dsh 路径就不成立，
    只能指望模型自己碰巧去翻文件。
    """
    status_doc = _load_status_doc(cwd)
    task_text = f"{status_doc}\n\n---\n\n{prompt}" if status_doc else prompt
    args = ["dsh", "--profile", "headless", task_text]
    try:
        proc = subprocess.run(
            args, cwd=cwd, env=load_env(), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"dsh headless 超过 {timeout} 秒时间预算，任务未完成"
    except FileNotFoundError:
        return False, "dsh CLI not found on PATH"
    if proc.returncode == 0:
        return True, (proc.stdout or "").strip()
    reason = (proc.stderr or proc.stdout or "").strip()
    return False, reason or f"dsh headless 退出码 {proc.returncode}"


def run_fable_design(prompt: str, cwd: str | None, timeout: int) -> tuple[bool, str, str]:
    """架构设计咨询：调用 claude CLI（非交互），只读 plan 模式，不修改任何文件。
    没有降级链——这一环本来就是"Claude 不可用时没人能替代"的那部分，参见 PROJECT_STATUS.md。
    """
    args = [
        "claude", "-p",
        "--model", FABLE_MODEL,
        "--permission-mode", "plan",
        prompt,
    ]
    try:
        proc = subprocess.run(
            args, cwd=cwd or None, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "", "claude 调用超时"
    except FileNotFoundError:
        return False, "", "claude CLI not found on PATH"

    combined = proc.stdout + "\n" + proc.stderr
    if proc.returncode == 0:
        return True, proc.stdout.strip(), proc.stderr
    return False, "", combined


# ---------------------------------------------------------------------------
# 工具定义 —— 让 DeepSeek / Qwen 能像 codex 一样读写文件、跑命令
# ---------------------------------------------------------------------------

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录下的文件和子目录",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作目录的路径，留空表示根目录"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容(文本)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作目录的文件路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入文件内容(覆盖整个文件，不存在则创建，自动建父目录；原子写入)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作目录的文件路径"},
                    "content": {"type": "string", "description": "要写入的完整文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "在工作目录下执行一条 shell 命令(沙盒内，文件系统写入被限制在工作目录)，"
                "返回 stdout/stderr/exit code。非零退出码一律算失败(ERROR)；"
                "如果某条命令的非零退出是预期内的，请在命令里自行判断，"
                "例如 `grep -q foo bar || true`、`test $? -eq 1`"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_done",
            "description": (
                "任务确认完成时调用，附上最终总结。"
                "同一轮里只要有任何一个工具调用失败(ERROR)，无论它在 task_done 之前还是之后，"
                "task_done 都会被拒绝——请先把问题解决掉再单独确认完成"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "完成情况总结，建议包含你如何验证结果"},
                },
                "required": ["summary"],
            },
        },
    },
]


TOOL_NAMES = {t["function"]["name"] for t in AGENT_TOOLS}


def _shell_program(cmd: str) -> str:
    """取 shell 命令里被执行的第一个程序名，用来判断"重跑的是不是同一件事"。"""
    for tok in (cmd or "").split():
        if "=" in tok.split("/")[0]:      # 跳过 FOO=bar 这种前置环境变量赋值
            continue
        return os.path.basename(tok)
    return ""


def _record_unresolved(current: dict | None, kind: str, fname: str, fargs: dict) -> dict | None:
    """记下一个未解决的失败。已有 strict 时不被更弱的失败覆盖。"""
    if kind == "shell_failed":
        prog = _shell_program(fargs.get("command", ""))
        return {
            "kind": "strict", "prog": prog,
            "reject": (f"还有未解决的 shell 失败（`{prog or '命令'}`）。"
                       f"请修好后重新成功跑通 `{prog or '同一条命令'}`，用它证明问题已解决，"
                       "再确认完成——跑一条无关的命令（比如 true/echo）不算数"),
        }
    if kind == "refused":
        # 策略拒绝(沙盒关着还调 run_shell)不是任务失败，但也不能当没发生：
        # 任务没被验证过。跨轮保留，可以被任意一次真正的工作解除，但光调 task_done 不行。
        return current if current and current["kind"] == "strict" else {
            "kind": "refused", "reject": (
                "run_shell 被拒绝执行，任务未经验证。若能改用 list_dir/read_file/write_file "
                "完成并自证，请先做那些操作；若这个任务本质上必须跑命令，请说明原因而不是直接确认完成"),
        }
    # 普通工具错误。工具名不认识时(未知工具)记成 None，否则同名工具永远不会成功 → 死锁。
    tool = fname if fname in TOOL_NAMES else None
    if current and current["kind"] == "strict":
        return current
    return {
        "kind": "loose", "tool": tool,
        "reject": (f"还有未解决的工具失败（{tool or fname}）。"
                   f"请修好后重新成功调用一次 {tool or '对应工具'} 来证明，再确认完成"),
    }


def _clear_unresolved(current: dict | None, fname: str, fargs: dict) -> dict | None:
    """一次成功的工具调用能否解除未解决状态。

    关键点：必须证明**原来那个**失败已经解决，而不只是"后来有别的调用成功了"。
    否则 pytest 挂了之后跑一句 `true` 就能解锁，等于没管。
    """
    if current is None:
        return None
    kind = current["kind"]
    if kind == "strict":
        if fname != "run_shell":
            return current
        prog = current.get("prog") or ""
        # prog 为空(命令解析不出程序名)时退回"任意成功的 run_shell 即可解除"，避免死锁。
        if not prog or _shell_program(fargs.get("command", "")) == prog:
            return None
        return current
    if kind == "loose":
        tool = current.get("tool")
        return None if (tool is None or fname == tool) else current
    if kind == "refused":
        # 任何一次真正的工具调用成功都算做了实事；只有裸 task_done 解除不了。
        return None
    return current


def _safe_path(root: str, rel_path: str) -> str | None:
    """把相对路径解析到 root 内部；越权(../逃逸)返回 None。"""
    rel_path = (rel_path or "").lstrip("/")
    candidate = os.path.realpath(os.path.join(root, rel_path))
    root_real = os.path.realpath(root)
    if candidate != root_real and not candidate.startswith(root_real + os.sep):
        return None
    return candidate


# 沙盒里只读暴露的系统目录白名单。刻意不含 /root /home /srv /mnt /media /data——
# 以前用 `--ro-bind / /` 把整个宿主挂进去，模型能读到工作目录外的 .env、~/.ssh、云凭据，
# 再借放行的网络发出去，边界比路径工具的"只能碰 cwd"宽得多。
SANDBOX_RO_DIRS = ["/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32"]
# 只暴露常用命令所需的公共系统数据。不要整体挂载 /etc、/var、/opt：这些目录中可能
# 有服务口令、数据库数据、备份或云凭据，列举并遮蔽热点无法形成可靠的安全边界。
SANDBOX_RO_PATHS = [
    "/etc/alternatives", "/etc/ca-certificates", "/etc/ssl/certs",
    "/etc/passwd", "/etc/group", "/etc/nsswitch.conf", "/etc/hosts",
    "/etc/gai.conf", "/etc/localtime", "/etc/timezone", "/etc/gitconfig",
    "/etc/ld.so.cache",
]

# 沙盒里保留的最小环境变量。其余一律用 --clearenv 清掉——沙盒进程继承宿主 environ 的话，
# 即使 .env 文件被遮蔽，预先 export 的 DEEPSEEK_API_KEY / AWS_* / GITHUB_TOKEN /
# 带认证信息的 *_PROXY 仍然能被 `env` 读出来，再借放行的网络发走。
SANDBOX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _sandbox_env() -> dict:
    """无沙盒退化路径用的最小环境（bwrap 路径用 --clearenv + --setenv 达到同样效果）。"""
    return {
        "PATH": SANDBOX_PATH,
        "HOME": "/sandbox-home",
        "TERM": "dumb",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TZ": os.environ.get("TZ", ""),
    }


def _bwrap_prefix(root: str, allow_net: bool = True, state_dir: str | None = None) -> list:
    """构造 bwrap 沙盒前缀。

    只读暴露程序/动态库目录和 SANDBOX_RO_PATHS 中明确列出的公共系统数据，
    工作目录 root 可写，其余宿主路径在沙盒里根本不存在，环境变量全部清空。
    传 state_dir 时，HOME 和 /tmp 绑到它下面的子目录，从而在同一次 agent 循环的多次
    run_shell 之间保持（`pip install --user`、npm/cargo 配置、上一条命令写的中间产物）；
    不传就退回一次性的 tmpfs。
    网络默认放行（很多实施任务要 pip/apt/curl），可用 allow_net=False 关掉。
    """
    args = ["bwrap", "--clearenv"]
    for p in SANDBOX_RO_DIRS:
        if not os.path.exists(p):
            continue
        if os.path.islink(p):
            # merged-usr 系统上 /bin -> usr/bin，直接 ro-bind 会绕开白名单语义，照原样复刻符号链接。
            args += ["--symlink", os.readlink(p), p]
        else:
            args += ["--ro-bind", p, p]

    for p in SANDBOX_RO_PATHS:
        if os.path.exists(p):
            args += ["--ro-bind", p, p]

    args += ["--dev", "/dev", "--proc", "/proc"]

    if state_dir:
        home_dir = os.path.join(state_dir, "home")
        tmp_dir = os.path.join(state_dir, "tmp")
        os.makedirs(home_dir, exist_ok=True)
        os.makedirs(tmp_dir, exist_ok=True)
        args += ["--bind", tmp_dir, "/tmp", "--bind", home_dir, "/sandbox-home"]
    else:
        args += ["--tmpfs", "/tmp", "--tmpfs", "/sandbox-home"]

    for k, v in _sandbox_env().items():
        if v:
            args += ["--setenv", k, v]

    args += [
        # 工作目录放在最后绑定：即使它落在被遮蔽/未暴露的路径下（如 /root/projects/x），
        # bwrap 也会在沙盒里补出父目录，只让这一个目录可写。
        "--bind", root, root,
        "--chdir", root,
        "--unshare-pid",
        "--die-with-parent",
    ]

    # router 自己的 .env 存着 DeepSeek/Qwen 的 key。当工作目录就是本项目时（自举改造场景），
    # 它会随 --bind 一起进沙盒，必须单独盖掉。
    env_real = os.path.realpath(ENV_FILE)
    root_real = os.path.realpath(root)
    if os.path.exists(env_real) and env_real.startswith(root_real + os.sep):
        args += ["--ro-bind", "/dev/null", env_real]

    if allow_net:
        # 解析域名要 /etc/resolv.conf。宿主上它常是指向 /run/systemd/resolve/... 的符号链接，
        # 而 /run 不在白名单（有 socket 和 /run/secrets 之类，不该整个暴露），所以这里把
        # **解析后的真实文件**直接绑到沙盒的 /etc/resolv.conf 上。
        # 注意：/etc 不再整体挂载后，沙盒里的 /etc 是由上面逐条 bind 拼出来的，
        # 没有悬空符号链接挡路，可以直接绑这个路径（早先整挂 /etc 时则必须绑到链接目标）。
        resolv_real = os.path.realpath("/etc/resolv.conf")
        if os.path.isfile(resolv_real):
            args += ["--ro-bind", resolv_real, "/etc/resolv.conf"]
    else:
        args += ["--unshare-net"]
    return args


_BWRAP_PROBE: tuple[bool, str] | None = None


def bwrap_status() -> tuple[bool, str]:
    """真正试跑一次沙盒，而不是只看二进制在不在。

    宿主可能装了 bwrap 但禁止创建 user/mount namespace（容器、内核 sysctl 限制等），
    这种情况下 `shutil.which` 会返回路径，实际每条命令却都以
    "Creating new namespace failed: Operation not permitted" 失败。
    结果缓存，一个进程只探测一次。返回 (可用, 不可用时的原因)。
    """
    global _BWRAP_PROBE
    if _BWRAP_PROBE is not None:
        return _BWRAP_PROBE

    if not shutil.which("bwrap"):
        _BWRAP_PROBE = (False, "PATH 上找不到 bwrap")
        return _BWRAP_PROBE

    probe_dir = tempfile.mkdtemp(prefix="router-bwrap-probe-")
    try:
        # 探测要覆盖真实用法的两个关键点：能建 namespace，且绑定目录可写。
        proc = subprocess.run(
            _bwrap_prefix(probe_dir) + ["bash", "-c", "printf ok > probe.txt"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0 and os.path.exists(os.path.join(probe_dir, "probe.txt")):
            _BWRAP_PROBE = (True, "")
        else:
            reason = (proc.stderr or proc.stdout).strip().splitlines()
            _BWRAP_PROBE = (False, reason[-1][:200] if reason else f"bwrap 退出码 {proc.returncode}")
    except (subprocess.TimeoutExpired, OSError) as e:
        _BWRAP_PROBE = (False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    return _BWRAP_PROBE


def execute_tool(name: str, args: dict, root: str,
                  allow_unsandboxed_shell: bool = False,
                  allow_net: bool = True,
                  state_dir: str | None = None) -> tuple[bool, str, str]:
    """执行一个工具，返回 (成功, 给模型看的结果文本, 失败种类)。

    成功与否由这里的返回值决定，不再靠调用方去 startswith("ERROR") 猜——
    否则一个内容恰好以 "ERROR" 开头的文件会被误判成工具失败。

    失败种类决定"未解决错误"要卡多严（见 run_agent_loop）：
      "ok"           成功
      "shell_failed" shell 命令真的跑挂了（非零退出/超时）——必须再跑通一条命令才算解决
      "error"        其他工具错误（路径越权、文件不存在等）——任意一次成功调用即可解除
      "refused"      策略拒绝（沙盒不可用时调 run_shell），不是任务失败，不跨轮累积
    """
    try:
        if name == "list_dir":
            p = _safe_path(root, args.get("path", ""))
            if p is None:
                return False, "ERROR: 路径超出工作目录范围，拒绝访问", "error"
            if not os.path.isdir(p):
                return False, f"ERROR: 目录不存在: {args.get('path', '')}", "error"
            entries = sorted(os.listdir(p))
            return True, "\n".join(entries) if entries else "(空目录)", "ok"

        elif name == "read_file":
            p = _safe_path(root, args.get("path", ""))
            if p is None:
                return False, "ERROR: 路径超出工作目录范围，拒绝访问", "error"
            if not os.path.isfile(p):
                return False, f"ERROR: 文件不存在: {args.get('path', '')}", "error"
            if os.path.getsize(p) > MAX_FILE_BYTES:
                return False, f"ERROR: 文件过大(>{MAX_FILE_BYTES}字节)，请用 run_shell 配合 head/sed 查看片段", "error"
            with open(p, encoding="utf-8", errors="replace") as f:
                return True, f.read(), "ok"

        elif name == "write_file":
            p = _safe_path(root, args.get("path", ""))
            if p is None:
                return False, "ERROR: 路径超出工作目录范围，拒绝访问", "error"
            parent = os.path.dirname(p) or root
            os.makedirs(parent, exist_ok=True)
            content = args.get("content", "")
            tmp_path = p + f".tmp.{os.getpid()}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, p)  # 原子替换，避免中断留下残缺文件
            return True, f"OK: 已写入 {args.get('path', '')} ({len(content)} 字节)", "ok"

        elif name == "run_shell":
            cmd = args.get("command", "")
            sandboxed, reason = bwrap_status()
            shell_env = None
            if sandboxed:
                full_cmd = _bwrap_prefix(root, allow_net, state_dir) + ["bash", "-c", cmd]
                sandbox_note = "" if allow_net else "\n(注: 沙盒已断网)"
            elif allow_unsandboxed_shell:
                # 用户用 --allow-unsandboxed-shell 明确授权过，才允许裸跑；否则一律拒绝。
                # 即使没有沙盒也不把宿主 environ 交出去——凭据泄露和文件隔离是两回事。
                full_cmd = ["bash", "-c", cmd]
                shell_env = _sandbox_env()
                shell_env["HOME"] = os.path.join(state_dir, "home") if state_dir else root
                os.makedirs(shell_env["HOME"], exist_ok=True)
                sandbox_note = f"\n(警告: 沙盒不可用（{reason}），本次命令在无隔离环境执行)"
            else:
                return False, (
                    f"ERROR: 沙盒不可用（{reason}），run_shell 已被禁用。"
                    "路径类工具(list_dir/read_file/write_file)仍可正常使用；"
                    "若确认要在无沙盒环境执行命令，请由用户重新以 --allow-unsandboxed-shell 运行 router。"
                ), "refused"
            proc = subprocess.run(
                full_cmd, cwd=root, capture_output=True, text=True,
                timeout=TOOL_SHELL_TIMEOUT, env=shell_env,
            )
            out = proc.stdout[-4000:]
            err = proc.stderr[-2000:]
            body = f"exit_code={proc.returncode}\nstdout:\n{out}\nstderr:\n{err}{sandbox_note}"
            if proc.returncode != 0:
                # 非零退出码就是失败。以前它返回普通结果，导致测试/构建挂了模型照样能 task_done。
                return False, f"ERROR: 命令以非零退出码结束\n{body}", "shell_failed"
            return True, body, "ok"

        elif name == "task_done":
            return True, "OK", "ok"

        return False, f"ERROR: 未知工具 {name}", "error"
    except subprocess.TimeoutExpired:
        return False, f"ERROR: 命令执行超过 {TOOL_SHELL_TIMEOUT} 秒，已终止", "shell_failed"
    except Exception as e:
        return False, f"ERROR: {type(e).__name__}: {e}", "error"


def _post_chat(base_url: str, api_key: str, model: str, messages: list,
                timeout: int, tools=None) -> dict:
    body = {"model": model, "messages": messages, "temperature": 0.2}
    if tools:
        body["tools"] = tools
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_message(data: dict) -> dict:
    """校验 API 返回结构，缺字段时抛出明确错误而不是让调用方 KeyError 崩溃。"""
    choices = data.get("choices")
    if not choices or not isinstance(choices, list):
        raise ValueError(f"API 返回缺少 choices 字段: {json.dumps(data, ensure_ascii=False)[:300]}")
    msg = choices[0].get("message")
    if not msg:
        raise ValueError(f"API 返回缺少 message 字段: {json.dumps(data, ensure_ascii=False)[:300]}")
    return msg


def _load_status_doc(cwd: str) -> str | None:
    for name in STATUS_DOC_NAMES:
        p = os.path.join(cwd, name)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    content = f.read()[:8000]
                return f"[{name} 内容，作为项目背景参考]\n{content}"
            except OSError:
                pass
    return None


def run_agent_loop(base_url: str, api_key: str, model: str, prompt: str,
                    cwd: str, timeout: int, backend_name: str,
                    allow_unsandboxed_shell: bool = False,
                    allow_net: bool = True) -> tuple[bool, str]:
    """带工具调用的自主循环：读写文件、跑命令，直到显式调用 task_done 或达到步数/时间上限。

    错误状态是**跨轮**的：一次工具失败会一直挡着 task_done，直到模型真的做了点什么把它解决掉。
    只按批次判断的话，模型第一轮跑挂测试、第二轮光调 task_done 就能宣布成功。
    """
    if not api_key:
        return False, f"missing API key for {base_url}"
    if not os.path.isdir(cwd):
        return False, f"工作目录不存在: {cwd}（不会自动创建，请先确认路径正确）"

    system = (
        "你是一个在服务器上独立工作的编程助手，工作目录已经就绪。"
        "你可以用提供的工具读写文件、列目录、执行 shell 命令来完成用户的任务。"
        "所有路径都相对于工作目录。必须实际调用工具去执行，不要只用文字描述。"
        "只有确认改动已生效(建议用 run_shell 验证，比如跑测试/检查文件内容)后才调用 task_done；"
        "只要有工具调用返回过 ERROR，就必须先处理好，不能直接 task_done——"
        "这个限制跨轮生效，换一轮再调 task_done 同样会被拒绝。"
        "特别地，如果是 shell 命令失败(测试/构建挂了)，必须再成功跑通一条 run_shell 命令来证明已修好。"
    )
    sandboxed, sandbox_reason = bwrap_status()
    if not sandboxed and not allow_unsandboxed_shell:
        system += (
            f"\n注意：当前环境沙盒不可用（{sandbox_reason}），run_shell 已被禁用，调用它只会返回 ERROR。"
            "请只用 list_dir/read_file/write_file 完成任务；"
            "如果任务本质上必须执行命令(比如跑测试、装依赖)，不要硬试，直接说明原因并停止。"
        )
    status_doc = _load_status_doc(cwd)
    messages = [{"role": "system", "content": system}]
    if status_doc:
        messages.append({"role": "system", "content": status_doc})
    messages.append({"role": "user", "content": prompt})

    deadline = time.time() + max(timeout, 300)
    per_call_timeout = min(timeout, HTTP_CALL_TIMEOUT) if timeout else HTTP_CALL_TIMEOUT

    # 跨轮的未解决错误，见 _record_unresolved / _clear_unresolved。None 表示当前没有欠账。
    unresolved = None
    # 整个循环共用一个沙盒状态目录，让 HOME 和 /tmp 在多次 run_shell 之间保持。
    state_dir = tempfile.mkdtemp(prefix=f"router-{backend_name}-")
    try:
        return _agent_steps(base_url, api_key, model, messages, timeout, per_call_timeout,
                             deadline, cwd, backend_name, allow_unsandboxed_shell,
                             allow_net, state_dir, unresolved)
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def _agent_steps(base_url, api_key, model, messages, timeout, per_call_timeout,
                  deadline, cwd, backend_name, allow_unsandboxed_shell,
                  allow_net, state_dir, unresolved) -> tuple[bool, str]:
    for step in range(MAX_TOOL_ITERS):
        if time.time() > deadline:
            return False, f"超过整体时间预算({max(timeout, 300)}秒)，任务未完成"
        try:
            data = _post_chat(base_url, api_key, model, messages, per_call_timeout, tools=AGENT_TOOLS)
            msg = _extract_message(data)
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:500]}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            # agent 模式下要求显式 task_done，纯文字回复视为未完成，提醒后重试一次
            messages.append(msg)
            messages.append({
                "role": "user",
                "content": "请实际调用工具执行任务；确认完成后必须调用 task_done 工具，不要只回复文字。",
            })
            continue

        messages.append(msg)

        # 两趟处理：先跑完这一批里所有非 task_done 的调用，确认整批无错，才接受 task_done。
        # 单趟的话，模型只要把 task_done 排在失败工具前面，就能骗到一个"成功"。
        parsed = []
        for tc in tool_calls:
            fname = tc["function"]["name"]
            try:
                fargs = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                fargs = {}
            parsed.append((tc, fname, fargs))

        results = {}
        for tc, fname, fargs in parsed:
            if fname == "task_done":
                continue
            ok_tool, result, kind = execute_tool(
                fname, fargs, cwd, allow_unsandboxed_shell, allow_net, state_dir)
            results[tc["id"]] = result

            if ok_tool:
                unresolved = _clear_unresolved(unresolved, fname, fargs)
            else:
                unresolved = _record_unresolved(unresolved, kind, fname, fargs)

        done_summary = None
        for tc, fname, fargs in parsed:
            if fname != "task_done":
                continue
            if unresolved is not None:
                results[tc["id"]] = "REJECTED: " + unresolved["reject"]
            else:
                results[tc["id"]] = "OK"
                if done_summary is None:
                    done_summary = fargs.get("summary", "任务已完成")

        # 消息按原始顺序回填，保证每个 tool_call 都有对应的 tool 消息。
        for tc, fname, fargs in parsed:
            result = results[tc["id"]]
            log_event({"backend": backend_name, "tool_call": fname,
                       "args_preview": json.dumps(fargs, ensure_ascii=False)[:200],
                       "result_preview": result[:200]})
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

        if done_summary is not None:
            return True, done_summary

    return False, f"达到最大步数({MAX_TOOL_ITERS})仍未显式确认完成(task_done)，可能任务过大，建议拆分"


def call_openai_compatible(base_url: str, api_key: str, model: str,
                            prompt: str, timeout: int) -> tuple[bool, str]:
    """无工具的纯文本问答（无 --cwd 时使用）。"""
    if not api_key:
        return False, f"missing API key for {base_url}"
    try:
        data = _post_chat(base_url, api_key, model,
                           [{"role": "user", "content": prompt}], timeout)
        msg = _extract_message(data)
        return True, msg.get("content", "") or "(无输出)"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:500]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _halt_on_partial_changes(backend: str, cwd: str, err: str) -> int:
    """某个后端执行到一半失败、且已经改过文件：停止降级，让人来确认。

    对每个能写文件的后端都适用，不只是 codex——否则 DeepSeek 留下半成品后 Qwen 继续改，
    正好重现这条保护本来要避免的交叉修改。
    """
    print(
        f"[{backend} 执行到一半失败，且已修改了工作目录里的文件] "
        "为避免二次模型在半成品上继续修改造成叠加损坏，已停止自动降级。"
        f"请先检查 {cwd} 里的改动，确认后再手动重试。\n原始错误: {err[-300:]}",
        file=sys.stderr,
    )
    return 1


def dispatch(task_type: str, prompt: str, cwd: str | None, timeout: int,
              allow_unsandboxed_shell: bool = False, allow_net: bool = True) -> int:
    env = load_env()
    backends, chains = load_backends()

    if task_type == "architecture":
        # 单独一环，没有降级链：架构设计本来就该由 Claude 判断，其他模型不能替代这个角色。
        start = time.time()
        ok, output, err = run_fable_design(prompt, cwd, timeout)
        duration = round(time.time() - start, 1)
        log_event({"backend": FABLE_MODEL, "task_type": "architecture", "ok": ok,
                   "duration_s": duration, "prompt_preview": prompt[:200],
                   "error_tail": None if ok else err[-500:]})
        if ok:
            print(output)
            return 0
        print(f"架构咨询失败（{FABLE_MODEL}，无降级）: {err[-500:]}", file=sys.stderr)
        return 1

    chain = chains.get(task_type)
    if not chain:
        print(f"未知任务类型: {task_type}（应为 architecture/{'/'.join(chains)}）", file=sys.stderr)
        return 2

    attempts = []
    for backend in chain:
        start = time.time()
        if backend == "codex":
            before_snap = snapshot_dir(cwd) if cwd and os.path.isdir(cwd) else None
            ok, output, err = run_codex(prompt, cwd, timeout)
            duration = round(time.time() - start, 1)
            if ok:
                log_event({"backend": "codex", "task_type": task_type, "ok": True,
                           "duration_s": duration, "prompt_preview": prompt[:200]})
                print(output)
                return 0

            after_snap = snapshot_dir(cwd) if cwd and os.path.isdir(cwd) else None
            changed = before_snap is not None and before_snap != after_snap
            quota_hit = looks_like_quota_error(err)
            attempts.append({"backend": "codex", "ok": False, "duration_s": duration,
                              "quota_hit": quota_hit, "changed_files": changed,
                              "error": err[-500:]})
            log_event({"backend": "codex", "task_type": task_type, "ok": False,
                       "duration_s": duration, "quota_hit": quota_hit, "changed_files": changed,
                       "error_tail": err[-500:], "prompt_preview": prompt[:200]})

            if changed:
                return _halt_on_partial_changes("codex", cwd, err)
            if not quota_hit:
                print(f"[codex 失败但未修改任何文件，非明确配额信号，仍降级尝试下一模型] {err[-300:]}",
                      file=sys.stderr)
            continue

        else:
            spec = backends.get(backend)
            if not spec:
                print(f"[跳过未定义的后端 {backend}]", file=sys.stderr)
                continue
            api_key = env.get(spec["key_env"], "")
            base_url = env.get(spec.get("base_url_env", ""), "") or spec["base_url"]
            model_name = spec["model"]
            # --cwd 自主执行用什么跑：deepseek 默认 dsh(本地 harness headless)，
            # 其它后端默认 api(自研 OpenAI 工具循环)；backends.json 可显式覆盖。
            runner = spec.get("runner") or ("dsh" if backend == "deepseek" else "api")
            if runner not in ("dsh", "api"):
                runner = "api"
            used_runner = "api"          # 实际执行路径，写日志用
            changed = False
            if cwd:
                # 和 codex 一样做前后快照：后端写了半成品再失败时，不能让下一家接着改，
                # 那正是"禁止交叉修改"要防的场景。dsh 路径同样受此保护。
                before_snap = snapshot_dir(cwd) if os.path.isdir(cwd) else None
                if runner == "dsh":
                    if not shutil.which("dsh"):
                        # 机器上没装 dsh harness：DeepSeek 槽位自动退回自研 API 循环(旧行为)。
                        log_event({"backend": backend, "runner": "dsh->api",
                                   "reason": "PATH 上没有 dsh CLI", "prompt_preview": prompt[:200]})
                    else:
                        used_runner = "dsh"
                if used_runner == "dsh":
                    if not api_key:
                        # dsh harness 与直连 API 共用同一把 DEEPSEEK_API_KEY，缺 key 就不白启动一趟。
                        ok, output = False, f"缺少 {spec['key_env']}（dsh harness 也用它调 DeepSeek API）"
                    else:
                        ok, output = run_dsh_headless(prompt, cwd, timeout)
                        if not allow_net:
                            print("[注意] --shell-no-network 只作用于 runner=api 的自研沙盒循环；"
                                  "dsh harness 自带沙盒，网络策略由其自身配置决定。", file=sys.stderr)
                else:
                    ok, output = run_agent_loop(base_url, api_key, model_name, prompt, cwd,
                                                 timeout, backend, allow_unsandboxed_shell, allow_net)
                after_snap = snapshot_dir(cwd) if os.path.isdir(cwd) else None
                changed = before_snap is not None and before_snap != after_snap
            else:
                # 纯文字问答不启动 harness（启动整套 agent 开销大，且没必要把文件系统暴露给它）。
                ok, output = call_openai_compatible(base_url, api_key, model_name, prompt, timeout)

            duration = round(time.time() - start, 1)
            log_event({"backend": backend, "task_type": task_type, "ok": ok,
                       "duration_s": duration, "runner": used_runner, "agentic": bool(cwd),
                       "changed_files": changed,
                       "error_tail": None if ok else output[-500:],
                       "prompt_preview": prompt[:200]})
            if ok:
                print(output)
                return 0
            attempts.append({"backend": backend, "ok": False, "duration_s": duration,
                              "changed_files": changed, "error": output[-500:]})
            if changed:
                return _halt_on_partial_changes(backend, cwd, output)
            continue

    print("全部后端都失败了：", file=sys.stderr)
    for a in attempts:
        print(f"  - {a['backend']}: {a.get('error', '')}", file=sys.stderr)
    return 1


def main():
    parser = argparse.ArgumentParser(description="AI Model Router")
    # 不用 required=True：--check-sandbox 是纯诊断子命令，不该被迫带 --type。
    parser.add_argument("--type", default=None,
                         choices=["architecture", "implementation", "basic"],
                         help="任务类型：architecture(Claude/Opus5做设计,无降级) / "
                              "implementation(走codex优先) / basic(走deepseek优先)")
    parser.add_argument("--cwd", default=None,
                         help="工作目录(必须已存在)。传入后 DeepSeek/Qwen 也会获得读写文件+执行命令的能力")
    parser.add_argument("--timeout", type=int, default=600,
                         help="整体时间预算(秒)，agent 模式下单次API调用固定用较短超时")
    parser.add_argument("--stdin", action="store_true", help="从 stdin 读取任务描述")
    parser.add_argument("--allow-unsandboxed-shell", action="store_true",
                         help="沙盒(bwrap)不可用时，仍允许 DeepSeek/Qwen 无隔离地执行 shell 命令。"
                              "默认关闭：探测不到可用沙盒就直接禁用 run_shell，而不是静默降级")
    parser.add_argument("--shell-no-network", action="store_true",
                         help="沙盒内断网执行 shell 命令(--unshare-net)。默认放行网络，"
                              "因为 pip/apt/curl 类任务需要；处理敏感代码时可以用这个进一步收紧")
    parser.add_argument("--check-sandbox", action="store_true",
                         help="只探测 bwrap 沙盒是否真的可用并退出（不调用任何模型）")
    parser.add_argument("prompt", nargs="?", default=None, help="任务描述")
    args = parser.parse_args()

    if args.check_sandbox:
        ok, reason = bwrap_status()
        print("沙盒可用: bwrap 能创建 namespace 且绑定目录可写" if ok
              else f"沙盒不可用: {reason}\n"
                   "  -> run_shell 默认被禁用；要无沙盒执行须显式加 --allow-unsandboxed-shell")
        sys.exit(0 if ok else 1)

    if not args.type:
        print("需要 --type（architecture/implementation/basic）", file=sys.stderr)
        sys.exit(2)

    if args.stdin:
        prompt = sys.stdin.read()
    elif args.prompt:
        prompt = args.prompt
    else:
        print("需要提供任务描述（位置参数或 --stdin）", file=sys.stderr)
        sys.exit(2)

    if args.cwd and not os.path.isdir(args.cwd):
        print(f"错误: --cwd 指定的目录不存在: {args.cwd}（请先创建，脚本不会自动建目录）", file=sys.stderr)
        sys.exit(2)

    if args.cwd and args.allow_unsandboxed_shell:
        sandboxed, reason = bwrap_status()
        if not sandboxed:
            print(f"警告: 沙盒不可用（{reason}），已按 --allow-unsandboxed-shell 允许无隔离执行 "
                  f"shell 命令，模型对 {args.cwd} 以外的路径也会有写权限。", file=sys.stderr)

    sys.exit(dispatch(args.type, prompt, args.cwd, args.timeout,
                       args.allow_unsandboxed_shell, not args.shell_no_network))


if __name__ == "__main__":
    main()
