"""降级链（P2-25 / AC-4）：L2.A → L1-C → L1-A → L0。

🔴 铁律 5：任何降级**不得向 L0 写错误状态**——降级只改变回答的来源标记，
绝不触碰 kp_state / answer_log / 每日任务（L0 调度闭环与网络无关）。

- L2.A：MiniMax-M3 在线生成（P2-24）
- L1-C：云端响应缓存命中（P2-23）
- L1-A：本地规则/预生成内容检索（离线规则表）
- L0  ：离线默认回答（永远可用，中性文案）
"""

from __future__ import annotations

from dataclasses import dataclass

from fdl_core.l2.cache import L1Cache, cache_key
from fdl_core.l2.minimax import ConsentError, L2UnavailableError, MiniMaxClient

# L1-A 本地预生成内容（离线规则检索；正式内容库阶段三由 ING 沉淀）
LOCAL_RULES: dict[str, str] = {}


@dataclass
class ChainResult:
    """降级链结果：answer + 来源层级（可视化/审计用）。"""

    answer: str
    source: str  # L2.A / L1-C / L1-A / L0
    elapsed_sec: float = 0.0


def run_chain(
    system_prompt: str,
    user_input: str,
    *,
    client: MiniMaxClient | None = None,
    cache: L1Cache | None = None,
    version_stamp: str = "v0",
) -> ChainResult:
    """依次尝试 L2.A → L1-C → L1-A → L0；任何一级失败/未命中自动降级。"""
    key = cache_key(system_prompt, user_input)

    # 1) L2.A 在线生成
    if client is not None:
        try:
            resp = client.ask(system_prompt, user_input)
            if cache is not None:  # 生成结果回填缓存
                cache.put(key, {"answer": resp.answer})
            return ChainResult(resp.answer, "L2.A", resp.elapsed_sec)
        except (ConsentError, L2UnavailableError):
            pass  # 静默降级（不向 L0 写任何状态）

    # 2) L1-C 缓存
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            return ChainResult(hit.get("answer", ""), "L1-C")

    # 3) L1-A 本地规则
    if user_input in LOCAL_RULES:
        return ChainResult(LOCAL_RULES[user_input], "L1-A")

    # 4) L0 离线默认（永远成功；🔴 不写任何库）
    return ChainResult("这个问题先记下来，我们一起想办法。", "L0")
