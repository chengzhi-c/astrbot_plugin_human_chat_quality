"""Centralized constants — single source of truth for thresholds.

All numeric limits derive from natural-talk upstream budgets with rationale.
Changing a threshold here propagates to config, detection, and hint building.
"""

from __future__ import annotations

import re

# 状态与提示预算
MAX_AVOID_ITEMS: int = 5  # 避用清单上限（重复开头+套路词合计），上游 budgets 5 项封顶
MAX_AVOID_ITEM_LEN: int = 20  # 单条避用词最大长度，超长截断成半截即失效，入库过滤口径
MAX_OPENER_LEN: int = 8  # 开头截断长度（前缀 ≤8 与普通开头同口径）
# 配置默认值与边界（_conf_schema.json 内镜像一份，JSON 无法引用 Python 常量，以 tests/test_config.py 锚定一致）
DEFAULT_STATE_RETENTION_DAYS: int = 14
MIN_STATE_RETENTION_DAYS: int = 1
MAX_STATE_RETENTION_DAYS: int = 365
DEFAULT_RECENT_REPLY_WINDOW: int = 8
MIN_RECENT_REPLY_WINDOW: int = 3
MAX_RECENT_REPLY_WINDOW: int = 50
MIN_RUNTIME_HINT_CHARS: int = 80  # 运行时提示最小字符数（完整短语装入，不截半）
MAX_RUNTIME_HINT_CHARS: int = 157  # 理论容量：53 前缀 + 5×20 + 4分隔 = 157

# 检测口径
OPENER_REPEAT_THRESHOLD: int = 3  # 同一开头在窗口内达3次才视为重复（降低误报）
CONSECUTIVE_THRESHOLD: int = 2  # 然而连发等固定模式阈值
DENSITY_BASE: int = 300  # 密度折算基准：每300字一档，长文按比例放宽（上游 engines/detector scale=max(1,len/300)）
DAY_SECONDS: int = 86400
STATE_SAVE_DEBOUNCE_SECONDS: float = 0.2  # 普通回复合并写盘；命令与退出仍直接 flush
PENDING_HINT_MAX_PER_SESSION: int = 32  # 无宿主 request id 时 FIFO 对齐的每会话上限
PENDING_HINT_TTL_SECONDS: float = 300.0  # 超时响应不再归因到旧请求提示
YIELD_STICKY_TTL_SECONDS: float = 300.0  # 正式写作/创作让位的进程内续写窗口
STICKY_FOLLOWUP_MAX_LEN: int = 20  # 粘性续写口令最大长度（core._is_sticky_followup）
PENDING_SESSION_CAP: int = 256  # 进程内 pending 队列会话数上限（core._evict_pending_if_needed）

# 切分正则（供 signal_detectors.detect_opening_cliches 与 runtime_state.extract_opener 共用）
OPENER_DELIM = re.compile(r"[，,。.!！?？\n\r]")

# opener 前缀（命中即返回，长度均 ≤MAX_OPENER_LEN）
OPENER_PREFIXES: tuple[str, ...] = (
    "我会",
    "好的",
    "可以",
    "没问题",
    "没事",
    "别急",
    "明白",
    "行吧",
    "好嘞",
    "确实",
    "当然",
    "对的",
    "没错",
    "哈哈",
)

# 预算档位对应 upstream budgets，暂不暴露为用户配置，保持最轻量（需档位时再引入）
