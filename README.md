# 回复去AI化质量层

在每次 LLM 请求时注入质量约束与本轮运行状态，减少 AI 回复的模板腔。

## 安装

1. 在 AstrBot 管理面板 -> 插件市场导入本仓库（或手动 zip 导入插件目录）；
2. 支持 AstrBot `>=4.16,<5`；
3. 测试请在仓库外运行（插件目录是 AstrBot 数据区，`data/` 下会混入 AstrBot 全局产物，已加入 `.gitignore`，勿提交）。

## 版本

- **v0.5.2（第三轮审查修复）**：修复 AstrBot `>=4.23` 上注入块被持久化进会话历史的问题——该版本移除了 part 级 `_no_save`/`mark_as_temp` 机制（4.16 仍在），注入文本会随 user 消息入库并逐轮累积；现在 `request_has_marker` 增加 `req.contexts` 扫描，历史中已有 marker 即不再注入；**动态提示（runtime/voice）改为原位替换**——历史旧块每轮被替换成最新内容，模型始终看到最新避用列表且不累积（4.16 上注入永不入历史，行为不变）；contexts 扫描只认 user 消息（模型复述/手打 marker 不误停）；TextPart 探测改模块级三态缓存（只探测一次，不再每轮 import + 刷日志）；`_load` 键缺失不再误判损坏、损坏备份限 5 份；`@register` 第二参修正为作者；`/humanq status` 统一 ADMIN 权限；`.gitignore` 忽略 `data/`（含敏感配置，防误提交）；metadata 补 `license`、版本去 `v` 前缀；logger import 统一收窄为 `ImportError`；voice opener 差集前缀归一化；单字 opener 过滤；README 数字与措辞修正；测试扩到 61 例。
- **v0.5.1（第二轮复审）**：移除 `_FallbackTextPart`（fallback 对象无法被 provider 消费，会直接让请求失败）——构造失败改返回 None，上层自动回退 system_prompt 注入；voice 样本清洗引用/@ 前缀并反向遍历凑满 60 条；voice opener 与 runtime 避用开头取差集（避免同轮矛盾指令）；`_INJECTED_MARKER_PREFIX` 常量共享；`disabled_sessions` 加载类型校验；空 origin 会话隔离；`#` 拆分补齐；词表移除"让我们"、新增 AI 腔词（"然而"改次数阈值，单次口语使用不提示；"根据我的知识""我的能力范围"覆盖免责声明/客服式回避腔）；稳定规则【像个人】新增"不知道就直说"正向引导；`build_voice_hint` 截断边界；emoji 正则补地区旗帜；`_state_from_dict` 死默认参数删除；status 文案修正；`custom_cliches` 超长词过滤；`append_temp_text_part` 空白拒绝；disabled 匹配函数合并；测试扩到 45 例。
- **v0.5.0**：审查修复 —— metadata 版本/作者同步；`_save` 全路径异常兜底（mkdir/写盘/替换，失败仅告警不阻断主链）；hook 顶层 try/except 防御；import 收窄为 `ImportError` 并显式失败；删除死代码（`hint_part_has_marker` 等）；opener 单字降噪 + 词表补"作为人工智能"；持久化截断跟随 `recent_reply_window`；`custom_cliches` 配置项；voice 样本限量 60 条、文案改"本会话"；TextPart 退化告警；启动 INFO 日志；`tests/` 测试套件落盘。
- **v0.4.0**：新增**声音校准**（`voice_match`）——从会话历史提取说话风格特征（句长、语气词、表情、开头词），注入轻量风格提示，让回复节奏贴合聊天氛围。默认关闭。
- **v0.3.0**：规则升级为 v2 —— 分层结构（铁律/词汇/结构/沟通/风格）+ 自查清单；套路词从 13 个扩到 39 个；新增 5 种结构级正则检测（破折号连发、首先其次最后、不是而是、不仅更是、自问自答）。
- **v0.2.0**：默认 **cache_friendly**——稳定规则 + 运行时提示都走 temp extra，避免改写 `system_prompt` 破坏 prompt cache；可回退 `legacy_system`。并修复 `extra_user_content_parts is None` 时运行时提示静默失败。

## 原理

- `on_llm_request(priority=-100)`：
  1. **稳定规则**：默认注入 `extra_user_content_parts`（temp）；`legacy_system` 时幂等追加到 `system_prompt`。
  2. **运行时提示**：始终注入 `extra_user_content_parts`（temp），缺失 list 时创建。
  3. **声音校准**（`voice_match` 开启时）：从 `req.contexts` 提取用户消息分析风格特征，注入轻量风格提示。
- `on_llm_response()`：解析本轮回复，更新会话状态。

规则在人设之后注入（`priority=-100`），只做减法去 AI 腔，不改变人设的性格、称呼、情绪和口头禅。

## 缓存相关

| 配置 | 默认 | 说明 |
|------|------|------|
| `prompt_injection_mode` | `cache_friendly` | `cache_friendly` / `legacy_system` |
| `inject_stable_rules` | true | 是否注入稳定规则 |
| `inject_runtime_state` | true | 是否注入运行时避用提示 |

说明：稳定规则内容固定，放进 system 理论上可缓存；但与其它插件改写 system 叠加时仍易触发 `sp_changed`。默认 temp extra 的目标是 **不污染 system 前缀**。

**AstrBot 版本差异（v0.5.2 起）**：`>=4.16,<4.23` 存在 part 级 `_no_save` 机制，注入块不落历史，每轮正常注入；`>=4.23` 该机制被框架移除，注入块会随 user 消息进入会话历史——插件检测到历史中已有注入标记后停止重复追加：稳定规则（静态）保留历史一份；**runtime/voice 动态提示每轮原位替换历史旧块**（改写仅作用于本轮请求副本，不写回存储），模型始终看到最新避用列表，token 成本不随轮数增长。

## 注入的规则

### 稳定规则

见 `/humanq rules`。标记：`[Human Chat Quality Rules v2]`。分层结构：铁律（4 条硬约束）、词汇层（7 类替换）、结构层（4 条）、沟通层（3 条）、风格层（5 条，含"不知道就直说"）、自查清单。

### 运行时提示

命中重复开头 / AI 套路词时注入避用列表。标记：`[Human Chat Quality Runtime]`。

## 命令

| 命令 | 作用 |
|------|------|
| `/humanq status` | 管理员：查看当前状态（含注入模式） |
| `/humanq on` | 管理员：启用当前会话 |
| `/humanq off` | 管理员：关闭当前会话 |
| `/humanq preview` | 管理员：查看将注入的运行时提示 |
| `/humanq rules` | 管理员：查看稳定规则原文 |
| `/humanq reset` | 管理员：清空当前会话状态 |

## 配置

| 配置项 | 说明 |
|--------|------|
| `enabled` | 总开关 |
| `prompt_injection_mode` | 注入模式 |
| `inject_stable_rules` | 注入稳定规则 |
| `inject_runtime_state` | 注入运行时提示 |
| `voice_match` | 声音校准（默认关闭，开启后从会话历史匹配当前会话说话风格） |
| `voice_max_chars` | 声音校准提示最大字符数 |
| `max_runtime_hint_chars` | 运行时提示最大字符数 |
| `state_retention_days` | 状态保留天数 |
| `recent_reply_window` | 判断重复开头的最近回复窗口 |
| `custom_cliches` | 自定义套路词（每行一个，命中即加入避用提示） |
| `disabled_sessions` | 禁用列表 |
| `debug_log` | 调试日志 |

## 测试

```
python -m unittest discover -s tests -v
```

覆盖：套路词/结构信号检测（红绿灯）、opener 提取、声音校准、状态存取/损坏恢复/写盘失败兜底/并发写盘、群号/禁用匹配纯函数、main.py 注入链路 mock 集成（幂等/禁用/空 origin/voice/配置边界/contexts 历史幂等与原位替换）。共 61 例（其中 1 例需真实 AstrBot 环境，无宿主时自动跳过）。

> 注：修改配置（如 `recent_reply_window`、`custom_cliches`）后需在 AstrBot 管理面板**重载插件**生效；测试请勿在插件目录内直接运行（`data/` 为 AstrBot 运行时目录）。

## 数据

状态保存在插件数据目录的 `runtime_state.json` 中，仅保留最近回复开头和命中的套路词，**不保存完整聊天记录**。

## 卸载

在 AstrBot WebUI 中禁用本插件，或删除插件目录。
