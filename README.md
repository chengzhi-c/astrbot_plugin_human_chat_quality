# 回复去AI化质量层

在每次 LLM 请求时注入质量约束与本轮运行状态，减少 AI 回复的模板腔。

## 版本

- **v0.2.0**：默认 **cache_friendly**——稳定规则 + 运行时提示都走 temp extra，避免改写 `system_prompt` 破坏 prompt cache；可回退 `legacy_system`。并修复 `extra_user_content_parts is None` 时运行时提示静默失败。

## 原理

- `on_llm_request(priority=-100)`：
  1. **稳定规则**：默认注入 `extra_user_content_parts`（temp）；`legacy_system` 时幂等追加到 `system_prompt`。
  2. **运行时提示**：始终注入 `extra_user_content_parts`（temp），缺失 list 时创建。
- `on_llm_response()`：解析本轮回复，更新会话状态。

规则在人设之后注入（`priority=-100`），只做减法去 AI 腔，不改变人设的性格、称呼、情绪和口头禅。

## 缓存相关

| 配置 | 默认 | 说明 |
|------|------|------|
| `prompt_injection_mode` | `cache_friendly` | `cache_friendly` / `legacy_system` |
| `inject_stable_rules` | true | 是否注入稳定规则 |
| `inject_runtime_state` | true | 是否注入运行时避用提示 |

说明：稳定规则内容固定，放进 system 理论上可缓存；但与其它插件改写 system 叠加时仍易触发 `sp_changed`。默认 temp extra 的目标是 **不污染 system 前缀**。

## 注入的规则

### 稳定规则

见 `/humanq rules`。标记：`[Human Chat Quality Rules v1]`。

### 运行时提示

命中重复开头 / AI 套路词时注入避用列表。标记：`[Human Chat Quality Runtime]`。

## 命令

| 命令 | 作用 |
|------|------|
| `/humanq status` | 查看当前状态（含注入模式） |
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
| `max_runtime_hint_chars` | 运行时提示最大字符数 |
| `state_retention_days` | 状态保留天数 |
| `recent_reply_window` | 判断重复开头的最近回复窗口 |
| `disabled_sessions` | 禁用列表 |
| `debug_log` | 调试日志 |

## 数据

状态保存在插件数据目录的 `runtime_state.json` 中，仅保留最近回复开头和命中的套路词，**不保存完整聊天记录**。

## 卸载

在 AstrBot WebUI 中禁用本插件，或删除插件目录。
