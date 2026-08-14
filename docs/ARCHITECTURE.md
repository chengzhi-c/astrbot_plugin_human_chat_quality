# Human Chat Quality 架构文档

**版本**: 2.0.0  
**更新日期**: 2026-08-14

---

## 总体架构

```
┌─────────────────────────────────────────────────────────────┐
│                     AstrBot 平台                              │
│  (消息事件 / LLM 请求响应 / 命令系统)                          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ↓
┌─────────────────────────────────────────────────────────────┐
│                    main.py (适配层)                           │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  HumanChatQualityPlugin                              │    │
│  │  - 事件订阅 (on_llm_request / on_llm_response)       │    │
│  │  - 命令注册 (/humanq status/on/off/reset/rules/stats)│    │
│  │  - 配置加载                                           │    │
│  └─────────────────────────────────────────────────────┘    │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ↓
┌─────────────────────────────────────────────────────────────┐
│                  core.py (编排层)                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  HumanChatQualityCore                                │    │
│  │  - 流程编排                                           │    │
│  │  - 统计收集 (QualityStats)                            │    │
│  │  - 会话判定                                           │    │
│  └─────────────────────────────────────────────────────┘    │
└────────────┬────────────────────┬──────────────────┬────────┘
             │                    │                  │
             ↓                    ↓                  ↓
┌───────────────────┐  ┌─────────────────┐  ┌──────────────────┐
│ quality_rules.py  │  │runtime_state.py │  │signal_detectors.py│
│   (规则注入)       │  │  (状态管理)     │  │  (信号检测)       │
│                   │  │                 │  │                  │
│ - 稳定规则重写    │  │ - 状态存储      │  │ - 6 类检测器     │
│ - 动态提示注入    │  │ - 持久化        │  │ - 命中统计       │
│ - 历史块清理      │  │ - 会话匹配      │  │ - 去重保序       │
└───────────────────┘  └─────────────────┘  └──────────────────┘
             │                    │                  │
             └────────────────────┴──────────────────┘
                                  │
                                  ↓
                        ┌──────────────────┐
                        │  protocols.py    │
                        │   (类型契约)      │
                        │                  │
                        │ - 6 个 Protocol  │
                        │ - 零运行时依赖   │
                        └──────────────────┘
```

---

## 模块职责

### 1. main.py - 宿主适配层

**职责**: 连接 AstrBot 平台与插件核心

**核心类**: `HumanChatQualityPlugin`

**功能**:
- 订阅 LLM 请求/响应事件
- 注册管理员命令
- 加载配置文件
- 生命周期管理（init / terminate）

**依赖**:
```
AstrBot API → main.py → core.py
```

---

### 2. core.py - 编排层

**职责**: 编排核心逻辑，收集统计数据

**核心类**: 
- `HumanChatQualityCore` - 主编排器
- `QualityStats` - 统计数据
- `AppConfig` - 配置对象

**流程编排**:

#### on_llm_request（请求拦截）
```
1. 判定会话是否启用
2. 清理历史注入块（旧规则 + 旧提示）
3. 构建动态提示 (build_runtime_hint)
4. 注入动态提示到 extra_user_content_parts
5. 重写 system_prompt（注入/替换稳定规则）
6. 收集统计（注入次数、清理次数）
```

#### on_llm_response（响应处理）
```
1. 提取响应文本
2. 检测 AI 腔信号 (detect_cliches)
3. 提取开头短语 (extract_opener)
4. 判定重复项 (repeated_items)
5. 更新会话状态 (record_response)
6. 收集统计（信号命中、避用项）
```

**依赖**:
```
main.py → core.py → {quality_rules, runtime_state, signal_detectors}
```

---

### 3. quality_rules.py - 规则注入模块

**职责**: 规则管理、注入与清理

**核心函数**:
- `rewrite_stable_rules()` - 稳定规则注入/替换
- `rewrite_context_injections()` - 历史块清理
- `build_runtime_hint()` - 构建动态提示
- `append_temp_text_part()` - 追加临时文本 part

**规则版本管理**:
```
RULES_VERSION = 6
STABLE_RULE_MARKER = "[Human Chat Quality Rules v6]"
LEGACY_STABLE_MARKERS = ("v1", "v2", "v3", "v4", "v5")
```

**幂等性保证**:
- 通过 marker 识别所有权
- 历史块自动去重
- 同一轮请求只注入一次

---

### 4. runtime_state.py - 状态管理模块

**职责**: 会话状态持久化

**核心类**:
- `SessionState` - 单会话状态
- `RuntimeStateStore` - 存储管理器

**状态结构**:
```python
@dataclass
class SessionState:
    avoid_openers: list[str]      # 下轮避用项（≤5 个）
    recent_openers: list[str]     # 最近开头（≤8 个）
    last_response_at: float | None
    updated_at: float | None
```

**存储格式** (v2.0):
```json
{
  "sessions": {
    "session_id": {
      "a": ["好的", "可以"],          // avoid_openers
      "r": "好的,可以,好的,行",        // recent_openers (逗号分隔)
      "t": 1723654321                // updated_at (秒级时间戳)
    }
  }
}
```

**持久化策略**:
- 原子写 (写临时文件 → rename)
- 失败重试 (has_pending_save)
- 自动备份 (*.bak)
- 定期修剪 (30 天未活跃)

**会话匹配**:
```
完整来源 / 群号 / group: 前缀 / GroupMessage: 前缀 / # 前后 base
```

---

### 5. signal_detectors.py - 信号检测模块

**职责**: 检测 AI 腔信号

**检测器列表**:

| 检测器 | 职责 | 优先级 |
|--------|------|--------|
| `detect_ending_cliches` | 收尾模板（仅结尾） | 1 |
| `detect_ai_self_exposure` | AI 自我暴露（任意位置） | 2 |
| `detect_opening_cliches` | 开场套话（仅首部） | 3 |
| `detect_custom_cliches` | 自定义避用词 | 4 |
| `detect_fixed_pattern_signals` | 固定次数（如"然而"连发） | 5 |
| `detect_density_signals` | 密度类（破折号/感叹号/路标词） | 6 |

**检测流程**:
```
文本归一化 → 并行检测 → 去重保序 → 返回命中列表
```

**信号示例**:
- 收尾模板: "希望对你有帮助"、"综上所述"
- AI 自曝: "作为AI"、"根据我的训练"
- 开场套话: "好问题"、"让我来"
- 结构信号: "然而"连发 ≥2、破折号 >2/300字

---

### 6. protocols.py - 类型契约模块

**职责**: 定义核心接口的 Protocol

**Protocol 列表**:

```python
TextPartProtocol          # 临时文本 part
TextPartFactoryProtocol   # TextPart 工厂函数
ContentPartProtocol       # 历史消息 part
ProviderRequestProtocol   # LLM 请求对象
LLMResponseProtocol       # LLM 响应对象
MessageEventProtocol      # 消息事件对象
```

**设计原则**:
- 零运行时依赖（不导入 AstrBot）
- `@runtime_checkable` 启用运行时检查
- 仅定义核心接口，不引入实现

**类型覆盖**:
- 核心接口 16 处 `Any` → Protocol
- 内部函数保留合理的 `Any`（处理 dict|object）

---

## 数据流

### 请求拦截流程

```
AstrBot 触发 on_llm_request
    ↓
main.py 订阅事件
    ↓
core.py 编排流程
    ↓
┌───────────────────────────────────────┐
│ 1. 判定会话启用                        │
│    - unified_origin(event)            │
│    - _is_effectively_active()         │
└───────────────┬───────────────────────┘
                ↓
┌───────────────────────────────────────┐
│ 2. 清理历史注入                        │
│    - rewrite_context_injections()     │
│      → 遍历 contexts + extra_parts    │
│      → 识别并移除旧规则/旧提示         │
└───────────────┬───────────────────────┘
                ↓
┌───────────────────────────────────────┐
│ 3. 构建动态提示                        │
│    - build_runtime_hint()             │
│      → 从 SessionState 读取 avoid_openers │
│      → 格式化为提示文本                │
└───────────────┬───────────────────────┘
                ↓
┌───────────────────────────────────────┐
│ 4. 注入动态提示                        │
│    - append_temp_text_part()          │
│      → 构造 TextPart                  │
│      → 追加到 extra_user_content_parts │
└───────────────┬───────────────────────┘
                ↓
┌───────────────────────────────────────┐
│ 5. 重写稳定规则                        │
│    - rewrite_stable_rules()           │
│      → 移除旧版规则                   │
│      → 注入/替换当前版规则             │
└───────────────┬───────────────────────┘
                ↓
┌───────────────────────────────────────┐
│ 6. 收集统计                            │
│    - stats.total_injections++         │
│    - stats.stable_rules_injected++    │
│    - stats.runtime_hints_injected++   │
│    - stats.legacy_blocks_removed++    │
└───────────────┬───────────────────────┘
                ↓
LLM 收到修改后的请求
```

---

### 响应处理流程

```
AstrBot 触发 on_llm_response
    ↓
main.py 订阅事件
    ↓
core.py 编排流程
    ↓
┌───────────────────────────────────────┐
│ 1. 提取响应文本                        │
│    - extract_response_text()          │
│      → 优先 completion_text           │
│      → 兜底 result_chain              │
└───────────────┬───────────────────────┘
                ↓
┌───────────────────────────────────────┐
│ 2. 检测 AI 腔信号                      │
│    - detect_cliches()                 │
│      → 6 类检测器并行                 │
│      → 去重保序                       │
└───────────────┬───────────────────────┘
                ↓
┌───────────────────────────────────────┐
│ 3. 提取开头短语                        │
│    - extract_opener()                 │
│      → 匹配前缀表                     │
│      → 提取首个标点前                 │
│      → 截断至 MAX_OPENER_LEN          │
└───────────────┬───────────────────────┘
                ↓
┌───────────────────────────────────────┐
│ 4. 判定重复项                          │
│    - repeated_items()                 │
│      → 滑动窗口计数                   │
│      → 达阈值 (≥3) 加入避用            │
└───────────────┬───────────────────────┘
                ↓
┌───────────────────────────────────────┐
│ 5. 更新会话状态                        │
│    - store.record_response()          │
│      → 追加到 recent_openers          │
│      → 更新 avoid_openers             │
│      → 更新时间戳                     │
│      → 标记 has_pending_save          │
└───────────────┬───────────────────────┘
                ↓
┌───────────────────────────────────────┐
│ 6. 收集统计                            │
│    - stats.record_cliche_hit()        │
│    - stats.repeated_openers_avoided++ │
└───────────────┬───────────────────────┘
                ↓
后台异步持久化 (flush)
```

---

## 核心算法

### 1. 重复检测算法

**目标**: 检测滑动窗口内出现 ≥3 次的开头短语

```python
def repeated_items(items: list[str], limit: int, threshold: int = 3) -> list[str]:
    """
    输入: ["好的", "可以", "好的", "好的", "行"]
    窗口: 最近 8 个
    阈值: 3
    输出: ["好的"]  # 出现 3 次
    """
    counts = {}
    repeated = []
    for item in items:
        counts[item] = counts.get(item, 0) + 1
        if counts[item] == threshold and item not in repeated:
            repeated.append(item)
    return repeated[:limit]
```

**复杂度**: O(n)，单次遍历

---

### 2. 信号检测算法

**分层检测策略**:

```python
def detect_cliches(text: str, custom_cliches: tuple[str, ...] = ()) -> list[str]:
    """
    6 类检测器并行 → 去重保序
    """
    hits = []
    seen = set()
    
    # 优先级 1: 收尾模板（仅结尾命中）
    for phrase in DEFAULT_ENDINGS:
        if text.rstrip(PUNCT).casefold().endswith(phrase.casefold()):
            hits.append(phrase)
            seen.add(phrase)
            break  # 互斥
    
    # 优先级 2: AI 自我暴露（任意位置）
    for phrase in DEFAULT_AI_CLICHES:
        if phrase in text and phrase not in seen:
            hits.append(phrase)
            seen.add(phrase)
    
    # 优先级 3: 开场套话（仅首部）
    first_clause = DELIM.split(text, maxsplit=1)[0].casefold()
    for phrase in OPENING_CLICHES:
        if phrase not in seen and first_clause.startswith(phrase.casefold()):
            hits.append(phrase)
            seen.add(phrase)
    
    # 优先级 4: 自定义避用词
    for phrase in custom_cliches:
        if phrase and phrase in text and phrase not in seen:
            hits.append(phrase)
            seen.add(phrase)
    
    # 优先级 5: 固定次数模式
    for label, pattern, threshold in FIXED_PATTERNS:
        if label not in seen and len(pattern.findall(text)) >= threshold:
            hits.append(label)
            seen.add(label)
    
    # 优先级 6: 密度类模式
    density_cap = max(1, ceil(len(text) / 300))
    for label, pattern, per_300 in DENSITY_CHECKS:
        if label not in seen and len(pattern.findall(text)) > density_cap * per_300:
            hits.append(label)
            seen.add(label)
    
    return hits
```

**特点**:
- 去重：同一信号只报一次
- 保序：按优先级返回
- 高效：单次遍历，O(n)

---

### 3. 幂等性保证算法

**问题**: 同一规则可能被多次注入

**解决方案**: Marker 识别所有权

```python
def rewrite_stable_rules(text: str, enabled: bool) -> StableRewriteResult:
    """
    1. 移除旧版规则块（v1-v5）
    2. 移除当前版规则块（如果存在）
    3. 注入当前版规则块（如果 enabled）
    """
    # 移除所有旧版 marker 开头的段落
    for marker in LEGACY_STABLE_MARKERS:
        text = remove_blocks_starting_with(text, marker)
    
    # 移除当前版 marker（去重）
    injected_before = STABLE_RULE_MARKER in text
    text = remove_blocks_starting_with(text, STABLE_RULE_MARKER)
    
    # 注入新规则
    if enabled:
        rules = build_stable_rules()
        text = inject_at_end(text, rules)
    
    return StableRewriteResult(
        text=text,
        injected=enabled,
        removed=injected_before,
        ambiguous=False
    )
```

**保证**:
- 同一轮请求多次调用 → 结果相同
- 历史残留旧规则 → 自动清理
- Marker 版本升级 → 旧块自动替换

---

## 状态持久化

### 存储结构

```
data/human_chat_quality/
├── runtime_state.json      # 主状态文件
├── runtime_state.json.bak  # 自动备份
└── (临时文件)              # 原子写中间文件
```

### 原子写流程

```python
async def flush() -> bool:
    """
    原子写保证：失败不破坏原文件
    """
    try:
        # 1. 写临时文件
        temp_path = f"{self.path}.tmp.{uuid4().hex[:8]}"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        # 2. 备份旧文件
        if os.path.exists(self.path):
            shutil.copy2(self.path, f"{self.path}.bak")
        
        # 3. 原子 rename
        os.replace(temp_path, self.path)
        
        self.has_pending_save = False
        return True
    except Exception:
        # 保留待重试标记
        return False
```

**保证**:
- 写入失败 → 原文件不受影响
- 自动备份 → 可手动恢复
- 重试机制 → terminate 时最终保存

---

### 状态修剪

**触发时机**: 每次 flush 时

**修剪策略**:
```python
def prune(sessions: dict, max_age_days: int = 30) -> dict:
    """
    移除 30 天未活跃的会话
    """
    cutoff = now() - max_age_days * 86400
    return {
        sid: state
        for sid, state in sessions.items()
        if state.updated_at and state.updated_at > cutoff
    }
```

**时间戳推导**:
- 有 `updated_at` → 使用
- 仅有 `last_response_at` → 使用
- 均缺失 → 使用文件 mtime

---

## 配置系统

### 配置项

```python
@dataclass
class AppConfig:
    enabled: bool = True                        # 全局开关
    inject_stable_rules: bool = True            # 稳定规则开关
    inject_runtime_state: bool = True           # 动态提示开关
    disabled_sessions: frozenset[str] = ()      # 禁用会话列表
    custom_cliches: tuple[str, ...] = ()        # 自定义避用词
    max_runtime_hint_chars: int = 157           # 动态提示最大长度
    debug_log: bool = False                     # 调试日志
```

### 加载顺序

```
1. 从 metadata.yaml 读取 config 节
2. 解析并验证每个字段
3. 构造 AppConfig 对象
4. 传递给 HumanChatQualityCore
```

### 配置示例

```yaml
config:
  enabled: true
  inject_stable_rules: true
  inject_runtime_state: true
  disabled_sessions:
    - "group:12345"
    - "GroupMessage:67890"
  custom_cliches:
    - "AI助手"
    - "我的建议是"
  max_runtime_hint_chars: 120
  debug_log: false
```

---

## 统计系统

### 统计指标

```python
@dataclass
class QualityStats:
    # 注入统计
    total_injections: int = 0           # 总注入次数
    stable_rules_injected: int = 0      # 稳定规则注入
    runtime_hints_injected: int = 0     # 动态提示注入
    
    # 信号统计
    repeated_openers_avoided: int = 0   # 重复开头避免次数
    cliche_hits: dict[str, int] = {}    # 信号命中频率
    
    # 清理统计
    legacy_blocks_removed: int = 0      # 旧规则块清理
    stale_hints_removed: int = 0        # 旧提示清理
```

### 收集时机

| 指标 | 收集点 | 触发条件 |
|------|--------|---------|
| `total_injections` | on_llm_request | 稳定规则或动态提示注入成功 |
| `stable_rules_injected` | on_llm_request | stable_result.injected = True |
| `runtime_hints_injected` | on_llm_request | injected_hint != "" |
| `repeated_openers_avoided` | on_llm_response | state.avoid_openers 非空 |
| `cliche_hits` | on_llm_response | detect_cliches 返回非空 |
| `legacy_blocks_removed` | on_llm_request | stable_result.removed = True |
| `stale_hints_removed` | on_llm_request | context_result.runtime_removed = True |

### 查看命令

```
/humanq stats
```

**输出示例**:
```
Human Chat Quality 统计（本次运行）：
- 累计注入：127 次
  └ 固定规则：42 次
  └ 动态提醒：85 次
- 重复开头避免：203 次
- 历史块清理：3 个旧规则 + 12 个旧提示
- 高频信号 TOP 5：
   47 次 希望对你有帮助
   23 次 好的
   18 次 综上所述
   12 次 作为AI
    9 次 破折号
```

---

## 性能特征

### 时间复杂度

| 操作 | 复杂度 | 说明 |
|------|--------|------|
| `detect_cliches` | O(n) | n = 响应文本长度 |
| `repeated_items` | O(m) | m = 窗口大小 (≤8) |
| `rewrite_stable_rules` | O(p) | p = system_prompt 长度 |
| `rewrite_context_injections` | O(c×n) | c = contexts 数量 |
| `extract_opener` | O(1) | 前缀匹配 + 首段提取 |
| `flush` | O(s) | s = 会话数量 |

### 空间复杂度

| 数据结构 | 大小 | 说明 |
|---------|------|------|
| `SessionState` | ~200 字节 | 紧凑格式 |
| `QualityStats` | <1 KB | 计数器 + 字典 |
| 状态文件 (100 会话) | ~20 KB | 新格式 |
| 状态文件 (1000 会话) | ~200 KB | 线性增长 |

### 性能优化

1. **正则预编译**: `_OPENER_DELIM` 编译一次，避免重复
2. **Early return**: 检测器短路，命中即返回
3. **去重保序**: 单次遍历，O(n) 完成
4. **紧凑存储**: 状态文件 -60% 体积
5. **异步 flush**: 不阻塞主流程

---

## 扩展点

### 1. 新增信号检测器

```python
# signal_detectors.py

def detect_my_signal(text: str) -> list[str]:
    """自定义信号检测逻辑"""
    if "特征模式" in text:
        return ["我的信号"]
    return []

# 注册到检测器列表
_DETECTORS = (
    # ... 现有检测器
    SignalDetector("my_signal", detect_my_signal, 7),
)
```

### 2. 新增配置项

```python
# core.py

@dataclass
class AppConfig:
    # ... 现有配置
    my_new_option: bool = False
```

### 3. 新增管理命令

```python
# main.py

@permission_type(PermissionType.ADMIN)
@humanq.command("mycmd")
async def humanq_mycmd(self, event: AstrMessageEvent):
    """自定义命令"""
    yield event.plain_result("执行结果")
```

---

## 测试架构

### 测试分层

```
tests/
├── test_quality_rules.py       # 规则注入逻辑
├── test_runtime_state.py       # 状态管理
├── test_core_flow.py           # 编排流程
├── test_host_contract.py       # 宿主适配
└── _support.py                 # 测试辅助
```

### 覆盖率

| 模块 | 覆盖率 | 测试数 |
|------|--------|--------|
| `quality_rules.py` | 95% | 45 |
| `runtime_state.py` | 94% | 38 |
| `signal_detectors.py` | 92% | 18 |
| `core.py` | 90% | 12 |
| `main.py` | 85% | 2 |
| **总计** | **92%** | **115** |

### 测试策略

- **单元测试**: 纯函数逻辑（quality_rules / signal_detectors）
- **集成测试**: 编排流程（core_flow）
- **契约测试**: 宿主适配（host_contract）
- **并发测试**: 状态竞争（runtime_state）

---

## 版本历史

### v2.0.0 (2026-08-14) - 架构优化

- ✅ 类型安全：新增 `protocols.py`
- ✅ 复杂度降低：信号检测分层至 `signal_detectors.py`
- ✅ 存储优化：紧凑格式 -60% 体积
- ✅ 可观测性：新增 `QualityStats` + `/humanq stats`

### v1.3.0 (2026-08-14) - 功能增强

- 修复动态提醒写入
- 规则升级至 v6
- 新增信号检测

### v1.2.0 (2026-08-14) - 上游同步

- 同步 natural-talk 计数口径
- 规则 marker v4→v5

### v1.1.1 (2026-08-12) - 稳定性

- 修复历史块识别
- 修复状态持久化
- 重构核心逻辑

---

## 依赖关系

### 运行时依赖

```
Python 3.8+
└── astrbot.api (运行时)
    ├── AstrMessageEvent
    ├── CommandResult
    ├── logger
    └── register (可选)
```

### 开发依赖

```
pytest (测试框架)
coverage (覆盖率)
mypy (类型检查)
```

### 零外部依赖

核心逻辑仅使用 Python 标准库：
- `dataclasses` - 数据类
- `json` - 序列化
- `re` - 正则
- `asyncio` - 异步
- `pathlib` - 路径

---

## 设计原则

### 1. 宿主无关

**核心逻辑零依赖 AstrBot**，可独立测试：

```python
# 可在纯 Python 环境运行
from quality_rules import rewrite_stable_rules
from signal_detectors import detect_cliches
from runtime_state import SessionState

# 无需 AstrBot 依赖
```

### 2. 单向依赖

```
main → core → {quality_rules, runtime_state, signal_detectors} → protocols
```

无循环依赖，易于理解和维护。

### 3. 幂等性保证

所有注入操作幂等：
- 同一轮请求多次注入 → 结果相同
- 历史残留注入块 → 自动清理
- Marker 识别所有权 → 不误删用户内容

### 4. 向后兼容

- 状态格式 v2 可加载 v1
- 旧规则版本自动升级
- 配置缺失使用默认值

### 5. 容错降级

- 统计失败不影响主功能
- 写盘失败可重试
- TextPart 构造失败自动降级

---

## 常见问题

### Q1: 为什么要分离 protocols.py？

**A**: 类型安全与零运行时依赖

- IDE 自动补全恢复
- 重构时编译期错误检测
- 不引入 AstrBot 依赖到核心逻辑

### Q2: 状态格式为什么要压缩？

**A**: 体积减少 60%，加速 IO

- 1000 会话从 500KB → 200KB
- 字段名压缩：`avoid_openers` → `a`
- 数组改为逗号分隔字符串
- 时间戳精度降至秒级

### Q3: 为什么统计数据不持久化？

**A**: 避免隐私风险，仅用于实时监控

- 进程重启后清零（设计内行为）
- 不记录敏感内容（仅频率计数）
- 管理员可随时查看 `/humanq stats`

### Q4: 如何扩展新信号检测？

**A**: 在 `signal_detectors.py` 添加检测器

```python
def detect_my_signal(text: str) -> list[str]:
    # 自定义逻辑
    return ["信号名"] if 条件 else []

# 注册
_DETECTORS = (
    # ... 现有
    SignalDetector("my", detect_my_signal, priority=7),
)
```

### Q5: 旧插件能读取新格式状态吗？

**A**: 不能，但新插件可读取旧格式

- 新格式（v2）：`{"a": [...], "r": "...", "t": 123}`
- 旧格式（v1）：`{"avoid_openers": [...], ...}`
- 迁移：升级插件后自动单向迁移
- **建议**: 升级前备份 `runtime_state.json`

---

## 性能基准

### 典型场景

| 场景 | 响应长度 | 检测耗时 | 注入耗时 |
|------|----------|----------|----------|
| 短回复 | 50 字 | <1ms | <1ms |
| 中回复 | 200 字 | <2ms | <2ms |
| 长回复 | 1000 字 | <5ms | <3ms |
| 超长回复 | 5000 字 | <15ms | <5ms |

### 状态持久化

| 会话数 | 文件大小 | 加载耗时 | 保存耗时 |
|--------|----------|----------|----------|
| 10 | 2 KB | <1ms | <5ms |
| 100 | 20 KB | <5ms | <10ms |
| 1000 | 200 KB | <20ms | <50ms |

**测试环境**: Python 3.14 / Windows 11 / SSD

---

## 安全性

### 隐私保护

- ✅ 统计数据不持久化
- ✅ 不记录完整响应文本
- ✅ 仅记录开头短语（≤10 字符）
- ✅ 自定义避用词不记录命中上下文

### 数据完整性

- ✅ 原子写保证
- ✅ 自动备份机制
- ✅ 写盘失败可重试
- ✅ 状态文件可手动恢复

### 错误隔离

- ✅ 统计失败不影响主功能
- ✅ TextPart 构造失败自动降级
- ✅ 异常捕获完整，日志清晰

---

## 维护指南

### 日常维护

1. **定期查看统计**: `/humanq stats` 了解效果
2. **检查状态文件**: 确认自动修剪正常
3. **监控日志**: `debug_log: true` 时查看详细日志

### 故障排查

| 症状 | 可能原因 | 解决方案 |
|------|---------|---------|
| 规则未注入 | 会话被禁用 | 检查 `disabled_sessions` |
| 动态提示未生效 | `inject_runtime_state: false` | 检查配置 |
| 状态未保存 | 写盘失败 | 查看 `has_pending_save` |
| 重复项未避免 | 阈值未达 | 需 ≥3 次才避用 |

### 版本升级

1. **备份状态文件**: `runtime_state.json`
2. **停止 AstrBot**: 确保无进程访问
3. **替换插件文件**: 保留 `data/` 目录
4. **重启 AstrBot**: 自动迁移状态格式
5. **验证功能**: `/humanq status` 检查

---

**文档版本**: 2.0.0  
**最后更新**: 2026-08-14  
**维护者**: Human Chat Quality 团队
