"""
Claude Code agent implementation.

Invokes the `claude` CLI in non-interactive (print) mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional

from app.agents.base import AgentConfig, BaseAgent
from app.models.schemas import AnalysisResult

logger = logging.getLogger("jarvis.agent.claude_code")

# Env vars that must not leak into the CLI subprocess: they trigger an interactive
# "Do you want to use this API key?" prompt that blocks non-TTY subprocess stdin.
_CLI_ENV_EXCLUDE = frozenset({
    "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_VERTEX_BASE_URL", "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "ANTHROPIC_VERTEX_PROJECT_ID",
})


def _make_cli_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in _CLI_ENV_EXCLUDE}


# 进行中/占位标记：模型常先写一份 "PRELIMINARY / 分析中" 的 checkpoint result.json 再继续。
# salvage 路径必须用它把占位结果挡掉——否则被外部 kill（看门狗/超时）时，会把占位当成品
# 标成 done（实测 fb_17b4fa0293：root_cause="PRELIMINARY - still investigating" 被标 done）。
# 与 Stop hook 的 check_result.py 保持同一套标记。
_PLACEHOLDER_MARKERS = (
    "preliminary", "analysis in progress", "in progress", "pending further",
    "pending grep", "still gathering", "still investigating", "to be analyzed",
    "分析中", "正在分析", "正在调查", "待进一步", "仍在调查", "仍需进一步",
)


def _is_placeholder_result(root_cause: str) -> bool:
    """root_cause 是否是"分析中"的占位 checkpoint（非终版）。"""
    rc = (root_cause or "").strip().lower()
    if len(rc) < 40:
        return True
    return any(m in rc for m in _PLACEHOLDER_MARKERS)


class ClaudeCodeAgent(BaseAgent):
    """Agent that delegates to the Claude Code CLI."""

    async def analyze(
        self,
        workspace: Path,
        prompt: str,
        on_progress: Optional[Callable[[int, str], Any]] = None,
    ) -> AnalysisResult:
        if on_progress:
            await _maybe_await(on_progress(60, "Claude Code 分析中..."))

        # Save prompt to file for debugging/audit, and pipe via stdin
        prompt_file = workspace / "prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")

        # L3: 在 workspace 写 .claude/settings.json，注入 Stop hook 强制 result.json 必须落地
        # 底层逻辑：模型想 stop 时 hook 先 check 文件是否存在，不存在就 block 它退出，强制再来一轮把文件写了。
        # 平台级合约，不靠 prompt 自觉。
        self._write_stop_hook(workspace)

        cmd = self._build_command()
        logger.info("Running Claude Code in %s (prompt: %d chars, piped via stdin)", workspace, len(prompt))

        cli_env = _make_cli_env()

        import time as _time
        _start_ts = _time.monotonic()

        async def _heartbeat() -> None:
            # Claude CLI 在 -p 模式不暴露中间进度，wrapper 自己造心跳；让 SSE
            # 端能看到「还在跑」而不是 60% 卡死错觉。每 15s 打一次。
            while True:
                await asyncio.sleep(15)
                if on_progress is None:
                    continue
                elapsed = int(_time.monotonic() - _start_ts)
                try:
                    pct = min(85, 60 + elapsed // 30)
                    await _maybe_await(on_progress(
                        pct,
                        f"Claude Code 分析中（已 {elapsed}s / 超时 {self.config.timeout}s）...",
                    ))
                except Exception:
                    pass

        proc: Optional[asyncio.subprocess.Process] = None
        hb_task: Optional[asyncio.Task] = None
        comm_task: Optional[asyncio.Task] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workspace),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=cli_env,
            )

            hb_task = asyncio.create_task(_heartbeat())
            # RC2 看门狗：不再用裸 wait_for，而是 poll 监控。两条退出线——
            #   (1) 总超时 self.config.timeout（硬线，已由 RC1 设为 pipeline−margin）
            #   (2) stall：result.json 首次落盘后 mtime 连续 stall_timeout 秒不动 → 某轮卡死
            # 两者都抛 asyncio.TimeoutError，复用下方已有的 salvage 路径捞回部分结果。
            comm_task = asyncio.create_task(
                proc.communicate(input=prompt.encode("utf-8"))
            )
            result_json_path = workspace / "output" / "result.json"
            stall_timeout = getattr(self.config, "stall_timeout", 0) or 0
            _last_mtime: Optional[float] = None
            _last_change_ts = _time.monotonic()
            try:
                while True:
                    done, _pending = await asyncio.wait({comm_task}, timeout=10)
                    now = _time.monotonic()
                    elapsed = int(now - _start_ts)
                    if comm_task in done:
                        stdout_bytes, stderr_bytes = comm_task.result()
                        break
                    if elapsed >= self.config.timeout:
                        raise asyncio.TimeoutError()
                    # stall 看门狗：仅在 result.json 已存在后生效（首轮长耗时是正常的）
                    if stall_timeout > 0:
                        try:
                            if result_json_path.exists():
                                m = result_json_path.stat().st_mtime
                                if m != _last_mtime:
                                    _last_mtime = m
                                    _last_change_ts = now
                                elif now - _last_change_ts >= stall_timeout:
                                    logger.warning(
                                        "Claude Code stalled: result.json unchanged for %ds (elapsed=%ds) — "
                                        "likely a stuck turn (no CLI per-turn timeout); killing and salvaging",
                                        int(now - _last_change_ts), elapsed,
                                    )
                                    raise asyncio.TimeoutError()
                        except asyncio.TimeoutError:
                            raise
                        except Exception:
                            pass
            finally:
                if hb_task is not None and not hb_task.done():
                    hb_task.cancel()

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                logger.warning(
                    "Claude Code exited with code %d. stderr: %s",
                    proc.returncode,
                    stderr[:500],
                )

                combined = (stderr + stdout).lower()

                # Detect token quota / rate limit exhaustion
                if any(kw in combined for kw in [
                    "rate limit", "quota", "credit", "billing",
                    "overloaded", "token limit", "usage limit",
                    "exceeded your current", "insufficient_quota",
                    "out of credits", "plan limit",
                    "hit your limit", "you've hit your",
                    "resets ", "hit the limit",
                ]):
                    # Extract the original error message for display
                    raw_err = (stderr.strip() or stdout.strip())[:500]
                    logger.error("Claude Code token quota exhausted. raw: %s", raw_err)
                    return AnalysisResult(
                        task_id="", issue_id="",
                        problem_type="Claude API Quota Exhausted",
                        problem_type_en="Claude API Quota Exhausted",
                        root_cause=(
                            "Claude Code API quota has been exhausted; analysis could not complete.\n\n"
                            f"Original error: {raw_err}\n\n"
                            "Please check account balance or wait for quota reset, then retry."
                        ),
                        root_cause_en=(
                            "Claude Code API quota has been exhausted; analysis could not complete.\n\n"
                            f"Original error: {raw_err}\n\n"
                            "Please check account balance or wait for quota reset, then retry."
                        ),
                        confidence="low", needs_engineer=False, system_failure=True, agent_type="claude_code",
                    )

                # Detect max turns exhaustion — but still try to parse result.json
                # because Claude may have written it before hitting the limit
                if "max turns" in combined or "reached max" in combined:
                    logger.warning("Claude Code reached max turns limit")

            if on_progress:
                await _maybe_await(on_progress(90, "解析分析结果..."))

            # L2: 检测到主分析结束但 result.json 没落地 → 触发"格式补救轮"
            # 底层逻辑：AI 已经把分析想清楚了（stdout 有 Markdown），只是忘了用 Write 工具落盘；
            # 启一个短任务（max_turns=3, timeout=60s）让它只做格式转换，避免走 Markdown 兜底。
            result_file = workspace / "output" / "result.json"
            if not result_file.exists() and stdout and len(stdout.strip()) > 200:
                logger.warning(
                    "result.json missing after primary run — triggering L2 format fixup (stdout=%d chars)",
                    len(stdout),
                )
                try:
                    fixed = await self._run_format_fixup(workspace, stdout)
                    if fixed:
                        logger.info("L2 format fixup succeeded — result.json now exists")
                    else:
                        logger.warning("L2 format fixup did NOT produce result.json — falling back to Markdown salvage")
                except Exception as e:
                    logger.warning("L2 format fixup exception: %s", e)

            # Always try to parse — even if returncode != 0, Claude may
            # have written result.json or produced useful stdout before failing
            raw_output = stdout
            result = self._parse_claude_output(workspace, raw_output)
            result.agent_type = "claude_code"
            return result

        except asyncio.CancelledError:
            # 外层 pipeline timeout（api/tasks.py:442 asyncio.wait_for）把我们 cancel 了。
            # 必须主动 kill 子进程，否则它在外层 task 已经标 failed 之后还在跑，
            # 吃 token / CPU / API quota，是真正的孤儿进程。
            elapsed = int(_time.monotonic() - _start_ts)
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    logger.warning(
                        "Claude Code subprocess killed on outer cancel (pid=%s, elapsed=%ds)",
                        proc.pid, elapsed,
                    )
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        pass
                except Exception as e:
                    logger.warning("Failed to kill subprocess on outer cancel: %s", e)
            # comm_task 仍 own 着管道，cancel 它避免 "Task was destroyed but pending" 噪音
            if comm_task is not None and not comm_task.done():
                comm_task.cancel()
            raise

        except asyncio.TimeoutError:
            logger.error("Claude Code timed out after %ds", self.config.timeout)

            # Kill the timed-out process and drain whatever stdout/stderr it produced.
            # 注意：管道归 comm_task 所有，必须 await 同一个 comm_task 取缓冲输出，
            # 不能再调 proc.communicate()（会报 already-called / 拿不到数据）。
            stdout_dump = ""
            stderr_dump = ""
            try:
                proc.kill()  # type: ignore[possibly-undefined]
                try:
                    if comm_task is not None:
                        drained_out, drained_err = await asyncio.wait_for(comm_task, timeout=5)
                        stdout_dump = drained_out.decode("utf-8", errors="replace") if drained_out else ""
                        stderr_dump = drained_err.decode("utf-8", errors="replace") if drained_err else ""
                except Exception:
                    pass
            except Exception:
                pass

            # Persist drained output for post-mortem analysis (never lose evidence again)
            try:
                debug_dir = workspace / "output"
                debug_dir.mkdir(parents=True, exist_ok=True)
                if stdout_dump:
                    (debug_dir / "timeout_stdout.txt").write_text(stdout_dump, encoding="utf-8")
                if stderr_dump:
                    (debug_dir / "timeout_stderr.txt").write_text(stderr_dump, encoding="utf-8")
                logger.info(
                    "Timeout dump saved: stdout=%d chars, stderr=%d chars",
                    len(stdout_dump), len(stderr_dump),
                )
            except Exception as e:
                logger.warning("Failed to dump timeout output: %s", e)

            # Salvage path 1: result.json exists → use it (partial result has value)
            result_file = workspace / "output" / "result.json"
            if result_file.exists():
                try:
                    result = self.parse_result(workspace, stdout_dump)
                    # 只 salvage 真·部分结果（有真实根因、低置信）；占位 checkpoint
                    # （PRELIMINARY/分析中）一律不当成品——否则外部 kill 时会把占位标成 done。
                    if result.root_cause and result.root_cause not in ("分析超时", "Analysis Timeout"):
                        if _is_placeholder_result(result.root_cause):
                            logger.warning(
                                "Claude Code timed out and result.json is only a placeholder (type=%s) — NOT salvaging, reporting honest failure",
                                result.problem_type,
                            )
                        else:
                            logger.info(
                                "Claude Code timed out but result.json exists — salvaging partial result (type=%s, confidence=%s)",
                                result.problem_type, result.confidence,
                            )
                            result.agent_type = "claude_code"
                            return result
                except Exception as e:
                    logger.warning("Failed to parse result.json after timeout: %s", e)

            # Salvage path 2: stdout has structured content → try parse_result on it
            # (covers cases where claude printed JSON to stdout but never called Write)
            if stdout_dump and "{" in stdout_dump:
                try:
                    result = self.parse_result(workspace, stdout_dump)
                    if (result.root_cause and result.root_cause not in ("分析超时", "Analysis Timeout")
                            and not _is_placeholder_result(result.root_cause)):
                        logger.info(
                            "Claude Code timed out, salvaging from stdout (type=%s)",
                            result.problem_type,
                        )
                        result.agent_type = "claude_code"
                        return result
                except Exception:
                    pass

            return AnalysisResult(
                task_id="",
                issue_id="",
                problem_type="Analysis Timeout",
                problem_type_en="Analysis Timeout",
                root_cause=f"Claude Code analysis exceeded {self.config.timeout}s timeout (stdout/stderr dumped to output/timeout_*.txt for investigation).",
                root_cause_en=f"Claude Code analysis exceeded {self.config.timeout}s timeout (stdout/stderr dumped to output/timeout_*.txt for investigation).",
                confidence="low",
                needs_engineer=False,
                system_failure=True,
                agent_type="claude_code",
            )
        except FileNotFoundError:
            logger.error("Claude Code CLI not found. Is it installed?")
            return AnalysisResult(
                task_id="",
                issue_id="",
                problem_type="Agent Unavailable",
                problem_type_en="Agent Unavailable",
                root_cause="Claude Code CLI is not installed or not on PATH.",
                root_cause_en="Claude Code CLI is not installed or not on PATH.",
                confidence="low",
                needs_engineer=False,
                system_failure=True,
                agent_type="claude_code",
            )

    def _build_command(self) -> list[str]:
        """Build claude CLI command. Prompt is piped via stdin."""
        cmd = [
            "claude",
            "-p",
            "--output-format", "text",
        ]

        if self.config.model:
            cmd.extend(["--model", self.config.model])

        if self.config.effort:
            cmd.extend(["--effort", self.config.effort])

        if self.config.fallback_model:
            cmd.extend(["--fallback-model", self.config.fallback_model])

        if self.config.betas:
            cmd.append("--betas")
            cmd.extend(self.config.betas)

        if self.config.max_turns:
            cmd.extend(["--max-turns", str(self.config.max_turns)])

        if self.config.allowed_tools:
            cmd.append("--allowedTools")
            cmd.extend(self.config.allowed_tools)

        return cmd

    def _parse_claude_output(self, workspace: Path, raw_output: str) -> AnalysisResult:
        """Parse Claude Code output - try result.json first, then raw text."""
        return self.parse_result(workspace, raw_output)

    # ------------------------------------------------------------------
    # L3: 写 Stop hook，强制模型必须写出 result.json 才能退出
    # ------------------------------------------------------------------
    # 校验脚本：不仅看 result.json 是否存在，还看是不是"分析中"的占位 checkpoint。
    # 底层逻辑：旧 hook 只查存在性 → fb_b47f129711 那份 "Analysis in progress... pending
    # further grep" 占位结果完全合规地通过了退出闸门，被当成品交付。这里升级为完成度校验。
    # 关键约束（避免把模型困死）：
    #   1. 只拦"进行中/占位"标记，不拦诚实的 low-confidence 终版（低置信≠没分析完）。
    #   2. block 次数封顶 2 次（.stop_block_count），超过即放行 → 交给 salvage，不烧光 turn 预算。
    #   3. 任何内部异常一律 fail-open（允许退出），绝不因校验脚本自身问题卡死模型。
    _STOP_CHECK_SCRIPT = r'''import json, os, sys
ws = os.getcwd()
p = os.path.join(ws, "output", "result.json")
counter = os.path.join(ws, ".claude", ".stop_block_count")

def allow():
    sys.exit(0)

def block(reason):
    try:
        n = int(open(counter).read().strip()) if os.path.exists(counter) else 0
    except Exception:
        n = 0
    if n >= 2:            # 已 block 2 次仍未 finalize → 放行，交给 salvage 兜底
        allow()
    try:
        open(counter, "w").write(str(n + 1))
    except Exception:
        pass
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    sys.exit(0)

if not os.path.exists(p):
    block("output/result.json 还没写！请立即用 Write 工具写入符合 schema 的 JSON，写完再尝试 stop。")

try:
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    block("output/result.json 不是合法 JSON，请用 Write 重写为符合 schema 的 JSON 后再 stop。")

rc = " ".join(str(data.get(k, "")) for k in ("root_cause", "root_cause_en", "root_cause_zh")).lower()
reply = " ".join(str(data.get(k, "")) for k in ("user_reply", "user_reply_en", "user_reply_zh")).lower()

inprogress = [
    "analysis in progress", "in progress", "pending further", "pending grep",
    "still gathering", "still investigating", "to be analyzed", "tbd",
    "分析中", "正在分析", "正在调查",
    "待进一步", "仍在调查", "仍需进一步",
]
reply_tpl = [
    "we are currently reviewing", "will provide a detailed response",
    "正在分析您", "稍后将为您提供", "稍后为您",
]

if any(m in rc for m in inprogress) or any(m in reply for m in reply_tpl):
    block("当前 result.json 是分析中的 checkpoint 占位（含 in-progress/稍后回复 字样），不是终版。"
          "请基于已积累证据 finalize：写出明确根因与完整客服回复，去掉占位话术，再 stop。")

if len(rc.strip()) < 40:
    block("root_cause 过短，分析尚未完成。请补足根因（含现象/根因/证据）后再 stop。")

allow()
'''

    @staticmethod
    def _write_stop_hook(workspace: Path) -> None:
        """在 workspace 内写 .claude/settings.json，注入 Stop hook。

        Stop hook 在模型决定 stop 时触发，委托 .claude/check_result.py 校验：
        - result.json 不存在 / 非法 JSON / 进行中占位 / root_cause 过短 → block，强制再来一轮
        - 否则 → exit 0 → 允许 stop
        block 封顶 2 次 + python3 不可用时回退到纯存在性检查，绝不无限循环或卡死模型。
        """
        try:
            settings_dir = workspace / ".claude"
            settings_dir.mkdir(parents=True, exist_ok=True)
            # 落地校验脚本
            (settings_dir / "check_result.py").write_text(
                ClaudeCodeAgent._STOP_CHECK_SCRIPT, encoding="utf-8"
            )
            # hook：优先跑校验脚本；python3 缺失/异常退出码非 0 时回退到旧的存在性检查
            hook_cmd = (
                "python3 \"$(pwd)/.claude/check_result.py\"; rc=$?; "
                "if [ $rc -ne 0 ]; then "
                "if [ -f \"$(pwd)/output/result.json\" ]; then exit 0; "
                "else echo '{\"decision\":\"block\",\"reason\":\"output/result.json 还没写！请立即用 Write 工具写入符合 schema 的 JSON，写完再尝试 stop。\"}'; fi; "
                "fi"
            )
            settings = {
                "hooks": {
                    "Stop": [
                        {
                            "matcher": "",
                            "hooks": [
                                {"type": "command", "command": hook_cmd}
                            ],
                        }
                    ]
                }
            }
            (settings_dir / "settings.json").write_text(
                json.dumps(settings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.debug("Wrote Stop hook to %s/.claude/settings.json", workspace)
        except Exception as e:
            logger.warning("Failed to write stop hook (non-fatal): %s", e)

    # ------------------------------------------------------------------
    # L2: 格式补救轮（廉价子任务，把已有 Markdown 转写成 result.json）
    # ------------------------------------------------------------------
    async def _run_format_fixup(self, workspace: Path, prev_markdown: str) -> bool:
        """启一个短任务让 Claude 把之前的 Markdown 转写成 result.json。

        只在主分析完成但没落盘 result.json 时调用。返回 True 表示 result.json 已生成。
        """
        # 截断输入，避免 prompt 爆掉（保留首尾，丢中间）
        max_md_chars = 20000
        md = prev_markdown
        if len(md) > max_md_chars:
            half = max_md_chars // 2 - 50
            md = md[:half] + "\n...[trimmed]...\n" + md[-half:]

        fixup_prompt = f"""你刚才完成了 Plaud 工单分析，但**忘了用 Write 工具把结果落盘到 `output/result.json`**。

## 你现在的唯一任务

把下面这段 Markdown 转写成符合 schema 的 JSON，**直接用 Write 工具写入 `output/result.json`，然后退出**。

- 不要重新分析、不要 grep、不要读其他文件
- 直接做格式转换，每个字段都从下面的 Markdown 里抽取/凝练
- 写完立即 `cat output/result.json` 验证一次然后退出

## Schema

```json
{{
  "problem_type": "问题分类（中文，从 Markdown 标题或根因段提取）",
  "problem_type_en": "Problem Type (English)",
  "root_cause": "根因（中文，5-10 句，含现象/根因/证据）",
  "root_cause_en": "Root cause (English, equivalent depth)",
  "confidence": "high | medium | low（从 Markdown 中找；找不到默认 medium）",
  "confidence_reason": "为什么是这个置信度",
  "key_evidence": ["最多 5 条关键日志或证据"],
  "user_reply": "完整中文客服回复模板（200-500 字）",
  "user_reply_en": "Full English reply (200-500 words)",
  "needs_engineer": false,
  "fix_suggestion": ""
}}
```

**`needs_engineer` 取值规则**：只有当 Markdown 里明确说"无法定位/证据不足/建议人工排查"时才填 true，否则一律 false。

## 你之前的 Markdown 输出（来源材料）

```
{md}
```
"""

        prompt_file = workspace / "fixup_prompt.md"
        try:
            prompt_file.write_text(fixup_prompt, encoding="utf-8")
        except Exception:
            pass

        # 单独构造命令：只允许 Write/Read/Bash，max-turns=3，超时 60s
        cmd = ["claude", "-p", "--output-format", "text", "--max-turns", "3"]
        if self.config.model:
            cmd.extend(["--model", self.config.model])
        cmd.extend(["--allowedTools", "Write", "Read", "Bash"])

        logger.info("Running L2 fixup: max-turns=3, timeout=60s, prompt=%d chars", len(fixup_prompt))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workspace),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_make_cli_env(),
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(input=fixup_prompt.encode("utf-8")),
                    timeout=60,
                )
            except asyncio.TimeoutError:
                logger.warning("L2 fixup subprocess timed out at 60s; killing")
                try:
                    proc.kill()
                except Exception:
                    pass
                return False

            if proc.returncode != 0:
                logger.warning(
                    "L2 fixup exited code=%d stderr=%s",
                    proc.returncode,
                    stderr_b.decode("utf-8", errors="replace")[:200],
                )
            # 唯一判据：result.json 是否真的落地了
            return (workspace / "output" / "result.json").exists()
        except FileNotFoundError:
            logger.warning("L2 fixup: claude CLI not found")
            return False
        except Exception as e:
            logger.warning("L2 fixup unexpected error: %s", e)
            return False


async def _maybe_await(val):
    """Await if the value is a coroutine."""
    if asyncio.iscoroutine(val):
        await val
