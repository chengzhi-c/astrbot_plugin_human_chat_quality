# astrbot_plugin_human_chat_quality

在每次 LLM 请求时注入质量约束与本轮运行状态，减少 AI 回复的模板腔。

## 原理

- `on_llm_request(priority=-100)`：
  1. 向 `system_prompt` 追加稳定规则（去模板腔、避免过度总结等）。
  2. 向 `extra_user_content_parts` 追加运行时提示（仅避开近期重复开头）。
- `on_llm_response()`：解析本轮回复，更新会话状态。

## 命令

| 命令 | 作用 |
|------|------|
| `/humanq status` | 查看当前状态 |
| `/humanq on` | 管理员：启用当前会话 |
| `/humanq off` | 管理员：关闭当前会话 |
| `/humanq preview` | 管理员：查看将注入的运行时提示 |
| `/humanq rules` | 管理员：查看稳定规则原文 |
| `/humanq reset` | 管理员：清空当前会话状态 |

## 配置

| 配置项 | 说明 |
|--------|------|
| `enabled` | 总开关 |
| `inject_stable_rules` | 注入稳定规则 |
| `inject_runtime_state` | 注入运行时提示 |
| `max_runtime_hint_chars` | 运行时提示最大字符数 |
| `state_retention_days` | 状态保留天数 |
| `recent_reply_window` | 判断重复开头的最近回复窗口 |
| `disabled_sessions` | 禁用列表，支持群号或完整 `unified_msg_origin` |
| `debug_log` | 输出注入与状态调试日志 |

## 数据

状态保存在插件数据目录的 `runtime_state.json` 中，仅保留最近回复开头用于识别重复套话，**不保存完整聊天记录**。实际注入给 LLM 的运行时提示只包含重复开头避用信息。

## 卸载

在 AstrBot WebUI 中禁用本插件，或删除目录：

```powershell
C:\Users\用户名\.astrbot\data\plugins\astrbot_plugin_human_chat_quality
```
