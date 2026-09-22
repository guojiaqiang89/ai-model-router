"""沙盒围栏的**真实执行**测试：真的起 bwrap 跑命令，验证边界确实成立。

和 test_router.py 的分工：
- `test_router.py` 是纯逻辑单元测试，完全离线、不依赖宿主 bwrap，任何环境都能跑。
- 本文件必须真起沙盒，所以依赖宿主能创建 namespace；探测不到就整体 skip。
  存在的理由：环境变量泄露、凭据路径可见性这类问题，只检查 bwrap 参数列表是发现不了的——
  当初 `--clearenv` 缺失就是靠"在沙盒里 echo 哨兵变量"才暴露的。

跑法：python3 test_sandbox.py
"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import router

SANDBOX_OK, SANDBOX_REASON = router.bwrap_status()


@unittest.skipUnless(SANDBOX_OK, f"当前环境沙盒不可用: {SANDBOX_REASON}")
class SandboxContainmentTests(unittest.TestCase):
    """凭据和宿主文件系统确实进不来。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sbtest-")

    def _sh(self, cmd, **kw):
        ok, out, kind = router.execute_tool("run_shell", {"command": cmd}, self.root, **kw)
        stdout = out.split("stdout:")[1].split("stderr:")[0].strip() if "stdout:" in out else ""
        return ok, stdout

    def test_host_environment_is_not_inherited(self):
        """宿主 environ 不能进沙盒——否则 export 的 API key 能被 env 读走再借网络发出去。"""
        probe = subprocess.run(
            [sys.executable, "-c",
             "import sys, tempfile; sys.path.insert(0, %r); import router\n"
             "print(router.execute_tool('run_shell', {'command': 'env'}, tempfile.mkdtemp())[1])"
             % os.path.dirname(os.path.realpath(__file__))],
            capture_output=True, text=True,
            env={**os.environ,
                 "SENTINEL_SECRET": "sk-CANARY-must-not-leak",
                 "AWS_SECRET_ACCESS_KEY": "canary-aws"},
        )
        self.assertNotIn("CANARY", probe.stdout, "哨兵 secret 泄进了沙盒")
        self.assertNotIn("canary-aws", probe.stdout, "AWS 凭据泄进了沙盒")
        self.assertIn("PATH=", probe.stdout, "最小环境里应该保留 PATH")

    def test_host_paths_are_invisible(self):
        for hidden in ["/root", "/home", "/var", "/opt", "/run", "/srv",
                       "/etc/shadow", "/etc/ssh", "/etc/ssl/private"]:
            with self.subTest(path=hidden):
                ok, out = self._sh(f"test -e {hidden} && echo VISIBLE || echo hidden")
                self.assertEqual(out, "hidden", f"{hidden} 在沙盒里可见")

    def test_router_own_env_file_is_masked_when_cwd_is_this_project(self):
        """自举改造场景：工作目录就是本项目时，router 自己的 .env 必须被盖掉。"""
        if not os.path.exists(router.ENV_FILE):
            self.skipTest(".env 不存在")
        ok, out, _ = router.execute_tool(
            "run_shell", {"command": "cat .env | wc -c"}, router.BASE_DIR)
        body = out.split("stdout:")[1].split("stderr:")[0].strip()
        self.assertEqual(body, "0", "工作目录是本项目时 .env 内容仍可读")

    def test_writes_confined_to_workdir(self):
        ok, _ = self._sh("printf ok > inside.txt")
        self.assertTrue(ok, "工作目录内应该可写")
        ok, _ = self._sh("printf bad > /usr/ESCAPED.txt")
        self.assertFalse(ok, "工作目录外不该可写")
        self.assertFalse(os.path.exists("/usr/ESCAPED.txt"))


@unittest.skipUnless(SANDBOX_OK, f"当前环境沙盒不可用: {SANDBOX_REASON}")
class SandboxUsabilityTests(unittest.TestCase):
    """收紧到只读白名单后，常见构建任务仍然跑得动——否则这个工具就没用了。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sbtest-")

    def _sh(self, cmd, **kw):
        ok, out, kind = router.execute_tool("run_shell", {"command": cmd}, self.root, **kw)
        return ok, out

    def test_toolchain_available(self):
        for cmd in ["python3 -c 'print(1+1)'", "git --version",
                    "python3 -m pip --version", "which gcc make"]:
            with self.subTest(cmd=cmd):
                ok, out = self._sh(cmd)
                self.assertTrue(ok, f"{cmd} 在沙盒里跑不了: {out[:200]}")

    def test_network_and_tls_work_by_default(self):
        ok, out = self._sh("getent hosts api.deepseek.com >/dev/null && echo dns-ok")
        self.assertTrue(ok, f"DNS 解析失败(白名单漏了 /etc/resolv.conf?): {out[:200]}")
        ok, out = self._sh(
            "curl -sS -o /dev/null -w '%{http_code}' https://api.deepseek.com --max-time 20")
        self.assertTrue(ok, f"HTTPS 失败(白名单漏了 CA 证书?): {out[:200]}")

    def test_shell_no_network_flag_cuts_network(self):
        ok, _ = self._sh("getent hosts api.deepseek.com", allow_net=False)
        self.assertFalse(ok, "--shell-no-network 没有真正断网")

    def test_state_dir_persists_home_and_tmp_across_calls(self):
        """同一次 agent 循环里 pip install --user / 中间产物不能每条命令都丢。"""
        state = tempfile.mkdtemp(prefix="sbstate-")
        self._sh("echo v > $HOME/cfg && echo w > /tmp/mid", state_dir=state)
        ok_home, _ = self._sh("cat $HOME/cfg", state_dir=state)
        ok_tmp, _ = self._sh("cat /tmp/mid", state_dir=state)
        self.assertTrue(ok_home, "HOME 没有跨 run_shell 保持")
        self.assertTrue(ok_tmp, "/tmp 没有跨 run_shell 保持")

    def test_without_state_dir_tmp_is_ephemeral(self):
        """不传 state_dir 的独立调用仍是一次性 tmpfs，默认隔离性没被放松。"""
        self._sh("echo z > /tmp/eph")
        ok, _ = self._sh("cat /tmp/eph")
        self.assertFalse(ok, "无 state_dir 时 /tmp 竟然被保留了")


if __name__ == "__main__":
    if not SANDBOX_OK:
        print(f"警告: 沙盒不可用（{SANDBOX_REASON}），本文件的用例全部 skip。"
              f"围栏未被验证，不要据此认为边界成立。", file=sys.stderr)
    unittest.main(verbosity=2)
