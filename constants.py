"""Centralized constants — single source of truth for thresholds.

All numeric limits derive from natural-talk upstream budgets with rationale.
Changing a threshold here propagates to config, detection, and hint building.
"""

from __future__ import annotations

# 状态与提示预算
MAX_AVOID_ITEMS: int = 5  # 避用清单上限（重复开头+套路词合计），上游 budgets 5 项封顶
MAX_AVOID_ITEM_LEN: int = 20  # 单条避用词最大长度，超长截断成半截即失效，入库过滤口径
# 兼容旧名：历史代码/测试引用 MAX_OPEN_LEN，保留别名
MAX_OPEN_LEN: int = MAX_AVOID_ITEM_LEN
MAX_OPENER_LEN: int = 8  # 开头截断长度（前缀 ≤8 与普通开头同口径）
MIN_RUNTIME_HINT_CHARS: int = 80  # 运行时提示最小字符数（完整短语装入，不截半）
MAX_RUNTIME_HINT_CHARS: int = 157  # 理论容量：53 前缀 + 5×20 + 4分隔 = 157

# 检测口径
OPENER_REPEAT_THRESHOLD: int = 3  # 同一开头在窗口内达3次才视为重复（降低误报）
CONSECUTIVE_THRESHOLD: int = 2  # 然而连发等固定模式阈值
DENSITY_BASE: int = 300  # 密度折算基准：每300字一档，长文按比例放宽（上游 engines/detector scale=max(1,len/300)）
DAY_SECONDS: int = 86400

# 版本与预算档位（对应 upstream budgets，当前未暴露为配置，保持极致轻量）
# PROMPT_LEVELS 保留为内部常量，暂不开放为用户配置，避免过度抽象
PROMPT_LEVELS: tuple[str, ...] = ("auto", "L0", "L1", "L2")  # noqa: F401 内部预留
