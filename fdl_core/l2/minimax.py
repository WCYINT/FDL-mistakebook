"""L2.A MiniMax-M3 接入（P2-24 / G2）。

🔴 数据条款 opt-out 守门（代码层强制，G2 合规）：
- 任何 LLM 调用前检查 `data_opt_out=True`（Frank 学习数据不用于训练）；
- 未确认 → `ConsentError`，**绝不发起请求**。

网络状态机（5 态）：OFFLINE → CONNECTING → READY → RATE_LIMITED → ERROR；
退避重试：429/timeout 指数退避（1s/2s/4s，最多 3 次）；
成本仪表盘：每次调用 token 计数追加写入 `logs/l2a_cost.jsonl`；
模型返回按记忆（2026-08-30 实测）剥离 `<think>...</think>` 思考段。

API key 来源：环境变量 `MINIMAX_API_KEY` 优先，其次 `config/secrets.yaml`
（该文件已被 .gitignore 排除，绝不入库）。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import yaml

from fdl_core.srs.time_layer import fmt_ts, now_utc

# audit-ok: L2 在线层（NFR-1 仅约束 L0，降级链 AC-4 单测验证离线路径）
API_URL = "https://api.minimax.chat/v1/text/chatcompletion_v2"  # audit-ok
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# 网络状态机 5 态
OFFLINE, CONNECTING, READY, RATE_LIMITED, ERROR = (
    "OFFLINE",
    "CONNECTING",
    "READY",
    "RATE_LIMITED",
    "ERROR",
)
_BACKOFF = (1, 2, 4)  # 指数退避秒数


class ConsentError(PermissionError):
    """🔴 数据条款未确认/未 opt-out——拒绝调用（G2 守门）。"""


class L2UnavailableError(ConnectionError):
    """L2 网络不可达（触发降级链）。"""


@dataclass
class L2Response:
    """一次 L2 调用的结果。"""

    answer: str
    state: str
    prompt_tokens: int
    completion_tokens: int
    elapsed_sec: float
    source: str = "L2.A"


def load_api_key(secrets_path: Path | str | None = None) -> str:
    """读 API key：env 优先，其次 config/secrets.yaml。"""
    import os

    key = os.environ.get("MINIMAX_API_KEY", "")
    if key:
        return key
    p = (
        Path(secrets_path)
        if secrets_path
        else (Path(__file__).resolve().parent.parent.parent / "config" / "secrets.yaml")
    )
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return str(data.get("minimax_api_key", ""))
    return ""


class MiniMaxClient:
    """MiniMax-M3 客户端（守门 + 状态机 + 退避 + 成本记账）。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        data_opt_out: bool = True,
        model: str = "MiniMax-M3",
        temperature: float | None = None,
        secrets_path: Path | str | None = None,
        cost_log: Path | str | None = None,
    ):
        self.api_key = api_key if api_key is not None else load_api_key(secrets_path)
        # 🔴 G2 守门：opt-out 必须显式为 True（Frank 数据不用于训练）
        self.data_opt_out = bool(data_opt_out)
        self.model = model
        # 2026-09-12 King 拍板：默认低温。实测同一证据两次调用置信度 0.75 vs 0.92，
        # 波动会破坏"高置信自动写入"闸门的可复现性（同一题可能这次过闸下次不过）。
        # 低温显著收敛采样随机性。可用环境变量 FDL_LLM_TEMPERATURE 覆盖，调参不改代码。
        if temperature is None:
            try:
                temperature = float(os.environ.get("FDL_LLM_TEMPERATURE", "0.1"))
            except (TypeError, ValueError):
                temperature = 0.1
        self.temperature = max(0.0, min(2.0, float(temperature)))
        self.state = OFFLINE
        self.cost_log = (
            Path(cost_log)
            if cost_log
            else (Path(__file__).resolve().parent.parent.parent / "logs" / "l2a_cost.jsonl")
        )

    # ── 状态机 ────────────────────────────────────────────
    def _set_state(self, state: str) -> str:
        self.state = state
        return state

    def _record_cost(self, prompt_tokens: int, completion_tokens: int, elapsed: float) -> None:
        """成本仪表盘数据点（JSONL 追加，阶段四 VIS 消费）。"""
        self.cost_log.parent.mkdir(parents=True, exist_ok=True)
        with self.cost_log.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": fmt_ts(now_utc()),
                        "model": self.model,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "elapsed_sec": round(elapsed, 2),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    # ── 调用 ─────────────────────────────────────────────
    def ask(self, system_prompt: str, user_input: str, *, timeout: int = 120) -> L2Response:
        """发起一次补全；守门 → 重试 → 剥离思考段 → 记账。"""
        # 🔴 守门 1：数据条款 opt-out（代码层强制，先于任何网络行为）
        if not self.data_opt_out:
            raise ConsentError("数据条款未确认 opt-out：拒绝调用（G2 合规守门）")
        if not self.api_key:
            self._set_state(OFFLINE)
            raise L2UnavailableError("缺少 MINIMAX_API_KEY")

        self._set_state(CONNECTING)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            # 🔴 守门 2：请求头显式声明数据不用于训练
            "X-Data-Usage": "opt-out-no-training",
        }
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input},
            ],
            "temperature": self.temperature,  # 低温（默认 0.1）：置信度可复现
            # 🔴 守门 3：请求体携带 opt-out 声明
            "data_handling": "no_training",
        }

        last_err: Exception | None = None
        for wait in (0, *_BACKOFF):
            if wait:
                self._set_state(RATE_LIMITED)
                time.sleep(wait)
            try:
                t0 = time.perf_counter()
                resp = requests.post(API_URL, headers=headers, json=body, timeout=timeout)
                elapsed = time.perf_counter() - t0
                if resp.status_code == 429:
                    last_err = L2UnavailableError("429 限流")
                    continue
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage", {}) or {}
                # 防御（2026-09-13）：API 可能返回 "choices": null（配额耗尽/风控/
                # 内部错误时响应体仍是 200 JSON 但无内容）——原实现
                # data.get("choices", [{}])[0] 遇到显式 null 直接 TypeError。
                # 实测样例：base_resp.status_code=2067「Token Plan 用量上限」。
                choices = data.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    base = data.get("base_resp") or {}
                    last_err = L2UnavailableError(
                        f"响应无 choices（{base.get('status_msg') or str(data)[:120]}）"
                    )
                    self._set_state(ERROR)
                    continue
                answer = _strip_think((choices[0].get("message") or {}).get("content", "") or "")
                self._set_state(READY)
                self._record_cost(
                    usage.get("total_tokens", 0) - usage.get("completion_tokens", 0),
                    usage.get("completion_tokens", 0),
                    elapsed,
                )
                return L2Response(
                    answer=answer,
                    state=READY,
                    prompt_tokens=usage.get("total_tokens", 0) - usage.get("completion_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    elapsed_sec=elapsed,
                )
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = e
                self._set_state(ERROR)
                continue
            except requests.HTTPError as e:
                last_err = e
                self._set_state(ERROR)
                if resp.status_code in (429, 500, 503):
                    continue
                break

        raise L2UnavailableError(f"L2.A 调用失败（重试 {len(_BACKOFF)} 次后）：{last_err}")


def _strip_think(text: str) -> str:
    """剥离 MiniMax-M3 thinking 段（2026-08-30 实测默认开启）。"""
    return THINK_RE.sub("", text).strip()
