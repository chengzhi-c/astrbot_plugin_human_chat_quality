# Changelog

本插件所有版本变更记录。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [0.5.2] - 2026-08-05

### 修复

- **AstrBot `>=4.23` 注入块持久化累积**：该版本框架移除了 part 级 `_no_save`/`mark_as_temp` 机制（4.16 仍在），注入文本会随 user 消息入库并逐轮累积。现在 `request_has_marker` 增加 `req.contexts` 扫描，历史中已有 marker 即不再注入（4.16 上注入永不入历史，行为不变）。
- **动态提示（runtime/voice）在 4.23+ 冻结**：改为原位替换——历史旧块每轮被替换成最新内容，模型始终看到最新避用列表且不累积（改写仅作用于本轮请求副本，不写回存储）。
- **`_load` 键缺失误判损坏**：旧版状态文件缺 `disabled_sessions` 键会被当作损坏并清空全部状态；现在缺失视为空列表，仅真类型错误走损坏分支；损坏备份限 5 份防磁盘堆积。
- **contexts 扫描误伤面**：只检查 `role == "user"` 消息，模型复述/手打 marker 到 assistant/system 消息不再误停注入。

### 变更

- TextPart 探测改模块级三态缓存（只探测一次，不再每轮 import + 刷日志）。
- `@register` 第二参修正为作者 `chengzhi-c`；`/humanq status` 统一 ADMIN 权限。
- `.gitignore` 忽略 `data/`（AstrBot 运行时全局产物，含敏感配置，防误提交）。
- metadata 补 `license: GPL-3.0`、版本去 `v` 前缀；logger import 统一收窄为 `ImportError`。
- voice opener 差集前缀归一化（"好的"与"好的，我来"变体视为重叠）；单字符消息不构成"常以 X 开头"特征。
- 移除死标记 `_is_temp`；`_no_save` setattr 增加防御。
- 测试扩到 61 例（含 contexts 历史幂等与原位替换、pydantic setattr 真实形态、损坏恢复分支）。

## [0.5.1] - 2026-08-05

### 修复

- **移除 `_FallbackTextPart`**：fallback 对象无法被 provider 消费（缺 `model_dump_for_context`、`type` 字段），一旦启用会让整个请求失败。构造失败改返回 `None`，上层自动回退 system_prompt 注入。
- voice 样本清洗引用（`(昵称): 内容`）与 `@` 前缀，反向遍历凑满 60 条即停。
- voice opener 与 runtime 避用开头取差集，避免同轮注入矛盾指令。
- `disabled_sessions` 加载类型校验；空 origin 会话隔离（不再挤进 `unknown` 会话）。
- `disabled_match_candidates_from_session` 补齐 `#` 拆分（与 event 路径一致）。
- `build_voice_hint` 极小 `max_chars` 截断边界；emoji 正则补地区旗帜区间。
- `_state_from_dict` 死默认参数删除；status 文案"本轮"改"自启动以来"。
- `custom_cliches` 超长词（>20 字）不注入；`append_temp_text_part` 拒绝纯空白文本。
- 重复的 disabled 匹配函数合并。

### 变更

- 词表移除高频误报"让我们"；新增 AI 腔词（"然而"改次数阈值，单次口语使用不提示；"根据我的知识""我的能力范围"覆盖免责声明/客服式回避腔）。
- 稳定规则【像个人】新增"不知道就直说"正向引导；【自查】清单同步。
- marker 前缀改为共享常量（`INJECTED_MARKER_PREFIX`）。
- 测试扩到 45 例（含 main.py 注入链路 mock 集成、voice 差集、并发写盘）。

## [0.5.0] - 2026-08-05

### 修复

- metadata 版本/作者/描述同步（v0.5.0、chengzhi-c）。
- `_save` 全路径异常兜底（mkdir/写盘/替换，失败仅告警不阻断主链）。
- hook 顶层 try/except 防御（on_llm_request/on_llm_response 包防御，不缩进内部）。
- import 兜底收窄为 `ImportError`；广谱 except 补日志。
- TextPart 退化告警；词表补"作为人工智能"。
- 提取器截断硬编码 20 → 跟随 `recent_reply_window`；voice 样本限量 60 条、文案"群聊"→"会话"。
- 删除死代码（`hint_part_has_marker`、重复 `injected_hint` 赋值）。
- 启动 INFO 日志（版本/恢复会话数/词表数）。

### 新增

- `tests/` 测试套件落盘（20 例，红绿灯 + 状态存取/损坏恢复/写盘失败兜底 + main.py 纯函数）。
- `custom_cliches` 配置项（内置词表 + 自定义词合并去重）。

## [0.4.0] - 2026-08-05

### 新增

- **声音校准（`voice_match`）**：从会话历史提取说话风格特征（句长、语气词、表情、开头词），注入轻量风格提示，让回复节奏贴合聊天氛围。默认关闭；样本 ≥5 条才注入，只取最近 60 条。
- 词表扩充与 5 种结构级正则检测（与 0.3.0 同步推进）。

## [0.3.0] - 2026-08-05

### 变更

- 规则升级为 v2：分层结构（铁律 4 条 / 词汇 6 类 / 结构 4 条 / 沟通 3 条 / 风格 4 条 / 自查清单）。
- 套路词从 13 个扩到 39 个。
- 新增 5 种结构级正则检测：破折号连发、首先其次最后、不是而是、不仅更是、自问自答。

## [0.2.0] - 2026-07-14

### 变更

- 默认 **cache_friendly**：稳定规则 + 运行时提示都走 temp extra，避免改写 `system_prompt` 破坏 prompt cache；可回退 `legacy_system`。

### 修复

- `extra_user_content_parts is None` 时运行时提示静默失败。
