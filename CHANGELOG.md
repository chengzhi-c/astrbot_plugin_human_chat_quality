# Changelog

本插件版本变更记录。版本号遵循语义化版本。

## [3.0.0] - 2026-09-01

- 检测器对齐上游 natural-talk：新增 D1 谄媚整句、D3 免责变体、D4 收尾扩充、D5 元话语空预告、B1 翻案变体（与其说/看似实则）、B4 关键在于冒号、C6 空泛气氛总结；评测集扩充，保持 0 误报 / 0 漏报。
- 新增危害排序：动态提示预算装不下时，可靠性损害信号（谄媚/免责）优先装入。
- 规则升级至 v9（RULES_VERSION 8→9）。
- 退役 v1–v7 旧规则块剥离签名表与旧版截断提示兼容代码（ARCHITECTURE.md D7 终态）。从 1.x 升级的用户：状态里的旧块会在约 14 天保留期后自然消失，或执行 `/humanq reset` 立即清理。
- `RuntimeStateStore.record_response` 的检测参数改为必传，删除内部重复检测路径。
- README 重构（快速开始上移、词表收敛为概览）；CHANGELOG 极简化（历史版本折叠，完整历史见 git tag v2.4.0）。
- 测试重组：test_core_flow 拆分为 config / flow / stats / text-extraction 四个文件。

## [2.x]（历史）

- 2.4.0 规则 v8，评测集 143 例，跨平台工具链修复
- 2.3.0 debounce 写盘，/humanq status 真实原因，冻结 detector 评测门禁
- 2.2.0 规则 v7，constants.py 阈值单源，delta 统计
- 2.1.0 修复：旧规则块不再阻断注入；发布包缺失模块修复
- 2.0.0 /humanq stats 命令，状态文件紧凑格式（体积 -60%）

## [1.x 及更早]

首个稳定版 1.0.0（2026-08-12）。0.x 完整历史见 git log / tag v2.4.0 的 CHANGELOG。
