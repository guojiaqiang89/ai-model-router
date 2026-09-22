"""完全离线、与宿主 bwrap 能力无关的回归测试。"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import router


def tool_call(call_id, name, args):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptedAPI:
    def __init__(self, messages):
        self.messages = iter(messages)

    def __call__(self, base_url, api_key, model, messages, timeout, tools=None):
        return {"choices": [{"message": next(self.messages)}]}


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="router-test-")
        self.root = self.tmp.name
        self.config = os.path.join(self.root, "backends.json")
        for patcher in (
            mock.patch.object(router, "BACKENDS_FILE", self.config),
            mock.patch.object(router, "LOG_FILE", os.path.join(self.root, "router.jsonl")),
            mock.patch.object(router, "LOG_DIR", self.root),
            mock.patch.object(router, "_BWRAP_PROBE", (False, "test: unavailable")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_loop(self, script):
        with mock.patch.object(router, "_post_chat", script), mock.patch.object(router, "log_event"):
            return router.run_agent_loop(
                "http://offline", "key", "model", "task", self.root, 30, "test",
                allow_unsandboxed_shell=True)

    def test_shell_exit_status(self):
        ok, text, kind = router.execute_tool(
            "run_shell", {"command": "exit 7"}, self.root, allow_unsandboxed_shell=True)
        self.assertFalse(ok)
        self.assertEqual(kind, "shell_failed")
        self.assertIn("exit_code=7", text)
        ok, _, kind = router.execute_tool(
            "run_shell", {"command": "true"}, self.root, allow_unsandboxed_shell=True)
        self.assertTrue(ok)
        self.assertEqual(kind, "ok")

    def test_cross_turn_shell_failure_requires_same_program(self):
        # Use run_shell calls; an unrelated successful program must not clear the failure.
        script = ScriptedAPI([
            {"role": "assistant", "tool_calls": [tool_call("1", "run_shell", {"command": "false"})]},
            {"role": "assistant", "tool_calls": [tool_call("2", "task_done", {"summary": "early"})]},
            {"role": "assistant", "tool_calls": [tool_call("3", "run_shell", {"command": "true"})]},
            {"role": "assistant", "tool_calls": [tool_call("4", "task_done", {"summary": "still early"})]},
            {"role": "assistant", "tool_calls": [tool_call("5", "run_shell", {"command": "false || true"})]},
            {"role": "assistant", "tool_calls": [tool_call("6", "task_done", {"summary": "verified"})]},
        ])
        self.assertEqual(self.run_loop(script), (True, "verified"))

    def test_refused_shell_blocks_bare_done(self):
        script = ScriptedAPI([
            {"role": "assistant", "tool_calls": [tool_call("1", "run_shell", {"command": "pytest"})]},
            {"role": "assistant", "tool_calls": [tool_call("2", "task_done", {"summary": "bad"})]},
        ])
        with mock.patch.object(router, "_post_chat", script), \
             mock.patch.object(router, "MAX_TOOL_ITERS", 2), mock.patch.object(router, "log_event"):
            ok, _ = router.run_agent_loop(
                "http://offline", "key", "model", "task", self.root, 30, "test")
        self.assertFalse(ok)

    def test_malformed_config_isolated_from_real_config(self):
        for payload in ["[]", '{"backends": []}', '{"chains": "bad"}',
                        '{"chains": {"basic": [{}]}}']:
            with self.subTest(payload=payload):
                with open(self.config, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                with contextlib.redirect_stderr(io.StringIO()):
                    backends, chains = router.load_backends()
                self.assertIn("deepseek", backends)
                self.assertEqual(chains["basic"], ["deepseek", "qwen"])

    def test_agentic_partial_change_stops_fallback(self):
        calls = []
        def fake_loop(base_url, api_key, model, prompt, cwd, timeout, backend_name,
                      allow_unsandboxed_shell=False, allow_net=True):
            calls.append(backend_name)
            if backend_name == "deepseek":
                with open(os.path.join(cwd, "partial.txt"), "w", encoding="utf-8") as handle:
                    handle.write("partial")
                return False, "failed after write"
            return True, "unexpected"
        env = {"DEEPSEEK_API_KEY": "x", "QWEN_API_KEY": "y"}
        # 本机可能装了 dsh，deepseek 默认 runner=dsh 会真去启动 harness——这里按 API 循环
        # 的语义测：显式关掉 dsh 发现（相当于机器上没有 dsh CLI，退回自研循环）。
        with mock.patch.object(router.shutil, "which", return_value=None), \
             mock.patch.object(router, "run_agent_loop", fake_loop), \
             mock.patch.object(router, "load_env", return_value=env), \
             mock.patch.object(router, "log_event"), contextlib.redirect_stderr(io.StringIO()):
            rc = router.dispatch("basic", "task", self.root, 30)
        self.assertEqual(rc, 1)
        self.assertEqual(calls, ["deepseek"])

    def test_snapshot_detects_empty_directory(self):
        before = router.snapshot_dir(self.root)
        os.mkdir(os.path.join(self.root, "empty"))
        self.assertNotEqual(before, router.snapshot_dir(self.root))

    def test_sandbox_prefix_has_narrow_read_boundary(self):
        prefix = router._bwrap_prefix(self.root)
        sources = [prefix[i + 1] for i, item in enumerate(prefix[:-2]) if item == "--ro-bind"]
        for forbidden in ("/", "/etc", "/var", "/opt"):
            self.assertNotIn(forbidden, sources)
        self.assertIn("--clearenv", prefix)
        self.assertIn(["--bind", self.root, self.root],
                      [prefix[i:i + 3] for i in range(len(prefix) - 2)])
        self.assertIn("--unshare-net", router._bwrap_prefix(self.root, allow_net=False))

    def test_bwrap_unavailable_refuses_shell(self):
        ok, text, kind = router.execute_tool("run_shell", {"command": "true"}, self.root)
        self.assertFalse(ok)
        self.assertEqual(kind, "refused")
        self.assertIn("已被禁用", text)

    def test_path_escape_is_rejected(self):
        ok, _, kind = router.execute_tool("read_file", {"path": "../../etc/passwd"}, self.root)
        self.assertFalse(ok)
        self.assertEqual(kind, "error")

    # ---- runner=dsh：DeepSeek 的 --cwd 自主任务默认交给本地 dsh headless ----

    @staticmethod
    def _which(value):
        return mock.patch.object(router.shutil, "which", return_value=value)

    def test_default_runners(self):
        backends, _ = router.load_backends()
        self.assertEqual(backends["deepseek"].get("runner"), "dsh")
        self.assertEqual(backends["qwen"].get("runner"), "api")

    def test_backends_json_runner_override_and_validation(self):
        # 只改 runner 的部分覆盖(不重发 base_url/model)对已有后端应生效；
        # 非法 runner 被警告并回默认。
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"backends": {
                "deepseek": {"runner": "api"},
                "qwen": {"base_url": "https://x", "model": "m",
                         "key_env": "K", "runner": "bogus"},
            }}))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            backends, _ = router.load_backends()
        self.assertEqual(backends["deepseek"]["runner"], "api")
        self.assertEqual(backends["qwen"]["runner"], "api")   # bogus -> 回默认
        self.assertIn("runner", err.getvalue())

    def test_dsh_cli_missing_degrades_to_agent_loop(self):
        calls = []
        def fake_loop(base_url, api_key, model, prompt, cwd, timeout, backend_name,
                      allow_unsandboxed_shell=False, allow_net=True):
            calls.append(backend_name)
            return True, "api-ran"
        env = {"DEEPSEEK_API_KEY": "x", "QWEN_API_KEY": "y"}
        with self._which(None), \
             mock.patch.object(router, "run_agent_loop", fake_loop), \
             mock.patch.object(router, "load_env", return_value=env), \
             mock.patch.object(router, "log_event"), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = router.dispatch("basic", "task", self.root, 30)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["deepseek"])

    def test_dsh_cli_present_routes_to_dsh_headless(self):
        dsh_calls = []
        def fake_dsh(prompt, cwd, timeout):
            dsh_calls.append((prompt, cwd, timeout))
            return True, "dsh-ran"
        env = {"DEEPSEEK_API_KEY": "x", "QWEN_API_KEY": "y"}
        with self._which("/usr/local/bin/dsh"), \
             mock.patch.object(router, "run_dsh_headless", fake_dsh), \
             mock.patch.object(router, "run_agent_loop",
                               side_effect=AssertionError("runner=dsh 不应再走自研循环")), \
             mock.patch.object(router, "load_env", return_value=env), \
             mock.patch.object(router, "log_event"), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = router.dispatch("basic", "task", self.root, 30)
        self.assertEqual(rc, 0)
        self.assertEqual(dsh_calls, [("task", self.root, 30)])

    def test_no_cwd_plain_qa_stays_api(self):
        api_calls = []
        def fake_api(base_url, api_key, model, prompt, timeout):
            api_calls.append(model)
            return True, "qa-ran"
        env = {"DEEPSEEK_API_KEY": "x"}
        with self._which("/usr/local/bin/dsh"), \
             mock.patch.object(router, "call_openai_compatible", fake_api), \
             mock.patch.object(router, "run_dsh_headless",
                               side_effect=AssertionError("纯问答不该启动 harness")), \
             mock.patch.object(router, "load_env", return_value=env), \
             mock.patch.object(router, "log_event"), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = router.dispatch("basic", "task", None, 30)
        self.assertEqual(rc, 0)
        self.assertEqual(api_calls, ["deepseek-chat"])

    def test_dsh_partial_changes_stop_fallback(self):
        def fake_dsh(prompt, cwd, timeout):
            with open(os.path.join(cwd, "partial.txt"), "w", encoding="utf-8") as handle:
                handle.write("partial")
            return False, "dsh failed after write"
        env = {"DEEPSEEK_API_KEY": "x", "QWEN_API_KEY": "y"}
        with self._which("/usr/local/bin/dsh"), \
             mock.patch.object(router, "run_dsh_headless", fake_dsh), \
             mock.patch.object(router, "run_agent_loop",
                               side_effect=AssertionError("改过文件后不该降级到 qwen")), \
             mock.patch.object(router, "load_env", return_value=env), \
             mock.patch.object(router, "log_event"), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            rc = router.dispatch("basic", "task", self.root, 30)
        self.assertEqual(rc, 1)
        self.assertIn("已停止自动降级", err.getvalue())

    def test_dsh_missing_key_falls_back_without_booting_harness(self):
        calls = []
        def fake_loop(base_url, api_key, model, prompt, cwd, timeout, backend_name,
                      allow_unsandboxed_shell=False, allow_net=True):
            calls.append(backend_name)
            return True, "qwen-ran"
        env = {"QWEN_API_KEY": "y"}   # 没有 DEEPSEEK_API_KEY
        with self._which("/usr/local/bin/dsh"), \
             mock.patch.object(router, "run_dsh_headless",
                               side_effect=AssertionError("缺 key 时不该启动 dsh")), \
             mock.patch.object(router, "run_agent_loop", fake_loop), \
             mock.patch.object(router, "load_env", return_value=env), \
             mock.patch.object(router, "log_event"), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = router.dispatch("basic", "task", self.root, 30)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["qwen"])

    def test_run_dsh_headless_wrapper(self):
        env = {"DEEPSEEK_API_KEY": "x", "DEEPSEEK_BASE_URL": "https://example.invalid"}
        def fake_ok(args, **kwargs):
            self.assertEqual(args, ["dsh", "--profile", "headless", "my task"])
            self.assertEqual(kwargs["cwd"], self.root)
            self.assertEqual(kwargs["env"], env)
            self.assertEqual(kwargs["timeout"], 30)
            class P:
                returncode = 0
                stdout = "final answer\n"
                stderr = ""
            return P()
        with mock.patch.object(router, "load_env", return_value=env), \
             mock.patch.object(router.subprocess, "run", fake_ok):
            ok, out = router.run_dsh_headless("my task", self.root, 30)
        self.assertTrue(ok)
        self.assertEqual(out, "final answer")

        def fake_fail(args, **kwargs):
            class P:
                returncode = 1
                stdout = ""
                stderr = "credentials error"
            return P()
        with mock.patch.object(router.subprocess, "run", fake_fail):
            ok, out = router.run_dsh_headless("t", self.root, 30)
        self.assertFalse(ok)
        self.assertIn("credentials error", out)

        with mock.patch.object(router.subprocess, "run",
                               side_effect=FileNotFoundError):
            ok, out = router.run_dsh_headless("t", self.root, 30)
        self.assertFalse(ok)
        self.assertIn("not found", out)

        with mock.patch.object(router.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("dsh", 5)):
            ok, out = router.run_dsh_headless("t", self.root, 30)
        self.assertFalse(ok)
        self.assertIn("时间预算", out)

    def test_run_dsh_headless_prepends_status_doc(self):
        # dsh 只吃一整段任务文本、没有独立的 system 消息通道，PROJECT_STATUS.md 必须
        # 拼进任务文本里，否则"--cwd 模式下 DeepSeek 会自动读交接文档"这条承诺对 dsh 路径不成立。
        with open(os.path.join(self.root, "PROJECT_STATUS.md"), "w", encoding="utf-8") as f:
            f.write("项目背景：XYZZY")

        seen = {}
        def fake_ok(args, **kwargs):
            seen["task_text"] = args[-1]
            class P:
                returncode = 0
                stdout = "ok\n"
                stderr = ""
            return P()
        with mock.patch.object(router, "load_env", return_value={}), \
             mock.patch.object(router.subprocess, "run", fake_ok):
            router.run_dsh_headless("my task", self.root, 30)
        self.assertIn("XYZZY", seen["task_text"])
        self.assertIn("my task", seen["task_text"])
        # 状态文档在前、原始任务在后，模型读到的顺序才是"先给背景，再给任务"。
        self.assertLess(seen["task_text"].index("XYZZY"), seen["task_text"].index("my task"))

    def test_run_dsh_headless_no_status_doc_leaves_prompt_unchanged(self):
        # 没有 PROJECT_STATUS.md/README.md 时不该给任务文本添任何多余包装。
        def fake_ok(args, **kwargs):
            self.assertEqual(args, ["dsh", "--profile", "headless", "my task"])
            class P:
                returncode = 0
                stdout = "ok\n"
                stderr = ""
            return P()
        with mock.patch.object(router, "load_env", return_value={}), \
             mock.patch.object(router.subprocess, "run", fake_ok):
            router.run_dsh_headless("my task", self.root, 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
