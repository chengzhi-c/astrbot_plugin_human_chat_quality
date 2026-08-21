# Human Chat Quality 架构文档

**文档版本**: 2.2.0
**更新日期**: 2026-08-16

> 本文档记录模块职责与设计意图。**实现细节以代码与测试为准**：本仓库的测试（`tests/`）锁定了所有关键行为，改动行为前先跑测试。配置项以 `_conf_schema.json` 为唯一权威。

---

## 1. 总体架构

```
AstrBot 平台（消息事件 / LLM 请求响应 / 命令系统）
        │
        ▼
main.py            宿主适配层：事件订阅、命令注册、配置加载、生命周期
        │
        ▼
core.py            编排层：会话判定、正式写作让位、流程编排、统计(delta)
        │
   ┌────┴────────────────┬─────────────────┐
   ▼                     ▼                 ▼
quality_rules.py    runtime_state.py   signal_detectors.py
规则注入与所有权      状态存储与持久化      AI 腔信号检测(连续化+铁律)
   │                     │                 │
   └─────────────────────┴─────────────────┘
                        ▼
              constants.py → protocols.py（类型契约，零运行时依赖）
```

**依赖方向**：`main → core → {quality_rules, runtime_state, signal_detectors} → constants/protocols`，单向无环。`constants.py` 为阈值单一源头。核心逻辑（core 及以下）不导入 AstrBot 运行时，`logger` 做了 ImportError 防护，因此可在无宿主环境独立测试。

## 2. 模块职责

| 模块 | 职责 | 关键接口 |
|------|------|----------|
| `main.py` | 连接 AstrBot 平台：订阅 `on_llm_request` / `on_llm_response`、注册 `/humanq` 命令组、加载配置、terminate 落盘 | `HumanChatQualityPlugin` |
| `core.py` | 编排：会话启用判定（配置开关 × 会话开关 × 静态黑名单）、注入流程、响应记录、进程内统计 | `HumanChatQualityCore`、`AppConfig`、`QualityStats` |
| `quality_rules.py` | 稳定规则重写（注入/剥离/幂等）、动态提示构建与历史块清理、临时 part 追加 | `rewrite_stable_rules`、`rewrite_context_injections`、`build_runtime_hint` |

`rewrite_context_injections` 清理历史 contexts 中可核验的本插件块；旧 runtime 不在历史中替换。`extra_user_content_parts` 保留普通 part 与至多一个匹配本轮的 runtime part，结果计数按物理删除块累计。
| `runtime_state.py` | 会话状态（重复开头、避用项）的读写、持久化（原子写、失败重试、损坏容错）、会话匹配 | `RuntimeStateStore`、`unified_origin`、`is_session_disabled` |
| `signal_detectors.py` | 分层检测 AI 腔信号（收尾/自我暴露/开场/自定义/固定/铁律/模糊叠加/密度按 300 字折算），去重保序 | `detect_cliches` + `detect_iron_rule/detect_hedge` |
| `constants.py` | 阈值单一源头（ budgets/MAX_AVOID_ITEM_LEN/阈值 rationale ） | 全部数值常量 |
| `protocols.py` | 宿主对象契约（`ProviderRequest` / `LLMResponse` / `MessageEvent` 等），纯类型标注 | 6 个 Protocol |

## 3. 数据流意图

**请求拦截（on_llm_request）**：

1. 正式写作意图：当前用户请求同时包含动作词与正式产物时让位；session_id 为空则跳过一切
2. `rewrite_context_injections`：清理历史中的本插件注入块（旧稳定规则、旧动态提示），保留用户内容
3. 构建动态提示（`build_runtime_hint` 按完整短语装入，不截半词），注入到 `extra_user_content_parts`（无可用 part 工厂时降级）
4. `rewrite_stable_rules`：剥离旧版本规则块（v1-v6 签名），幂等注入当前 v7 到 `system_prompt`（含铁律+动作一句）
5. 统计注入与清理计数（delta 避免膨胀，hint 英文边界精化）

**响应处理（on_llm_response）**：

1. 提取回复文本（`completion_text` 优先，`result_chain` 兜底）
2. 检测 AI 腔信号、提取开头短语、判定重复项
3. 更新会话状态（`record_response`），内存立即生效并由 debounce 任务合并写盘
4. 统计信号命中与避用项

## 4. 设计决策

### D1. 稳定规则的所有权：marker + 哈希签名

**背景**：规则块注入到用户 system_prompt 和历史中，升级版本后需要剥离旧块；但用户正文可能包含相似文本，粗暴的字符串匹配会误删用户内容。

**决策**：块以整行 marker（`[Human Chat Quality Rules vN]`）声明所有权。剥离旧块时，仅当整块内容通过已发布签名（行数 + sha256）核验才执行；无法核验的块保留不动。

**权衡**：签名表同时承担"确定块边界"的职责，因为旧版 v2 块内部含空行分段，无法按空行安全切分。代价是每次升级规则需要维护签名表，收益是零误删保证。签名表完整性由 `tests/test_quality_rules.py::TestSignatureTableCompleteness` 兜底，漏填会在测试期捕获。

**结论**：签名核验的剥离是唯一安全路径，保留。

### D2. 无法核验的旧块：保留，但不阻断

**背景**：v3 曾尝试发布但未形成可核验物；用户也可能编辑旧块。这类块边界未知，无法安全剥离。

**决策**：无法核验的块（ambiguous）一律保留。2.1.0 起，保留不再阻断当前规则注入（此前会永久停摆）；被编辑过的当前版本块按 marker 识别为"已注入"，尊重用户定制且不重复注入。

**结论**：保留是信息约束下的最优解；注入恢复由"当前版本块存在即视为已注入"保证幂等，不会累积。

### D3. 状态文件 v2 紧凑格式

**背景**：状态文件随会话数线性增长，字段名冗长。

**决策**：v2 格式用单字母键（`a`=avoid_openers、`r`=recent_openers、`t`=时间戳）与逗号分隔，体积较 v1 减约 60%；加载兼容 v1，写入只用 v2。

**权衡**：牺牲人工可读性换取 IO 与体积；格式已随 2.0.0 发布，不回退。

**容错**：顶层 JSON、根结构或文件 IO 无法恢复时，备份现场并重置状态；单个会话条目或其 v2 紧凑字段无效时，只为本次加载备份一次并跳过坏条，其余会话继续加载。这样保留有效提示历史，同时让损坏现场可追溯。

### D4. 信号检测：高置信度优先（2.2.0 扩展至铁律与模糊叠加）

**背景**：误报会让正常回复被反复"提醒"，用户会关掉插件。

**决策**：只抓高置信度信号。末尾模板仅结尾命中、开场套话仅首部命中、AI 自我暴露任意位置精确命中；密度类（破折号/感叹号/路标词）按 300 字基准折算阈值（ceil(len/300)），更长回复放宽；新增 Tier3 铁律（不是…而是/与其…不如/很久…久到，4 条）与 hedge（可能或许叠加），均经引号豁免降低误伤。词库从 `dist/lexicon.json` 高置信子集扩展（AI 3→8、收尾 +2、路标 6→10），仍保持 tuple 精确匹配无 Trie。

**权衡**：保留小型 tuple 词表和明确配对扫描，不引入 Trie 或自动改写；效果以独立语料评测为准，不在文档中预填未经验证的命中率。

### D5. 统计不持久化

**决策**：统计（`QualityStats`）仅进程内，不落盘，避免隐私风险（不记录命中上下文），仅作实时观测。状态文件也只存开头短语（≤8 字符）与避用词，不存完整聊天记录。

### D6. 依赖面收敛（2.2.0 引入 constants.py 单源治理）

**决策**：核心逻辑零第三方运行时依赖，仅标准库 + 宿主 API。阈值集中至 `constants.py`（MAX_AVOID_ITEM_LEN 等带 rationale），`quality_rules/runtime_state/signal_detectors` 仅 `from .constants import`，消除 3 处硬编码重复与命名混淆（`MAX_OPEN_LEN` 保留别名兼容）。测试用标准库 unittest，发布门禁用 ruff（check + format）。不引入 pytest/mypy/coverage。

### D9. 正式写作让位与状态反馈

**背景**：固定规则只在正式写作请求中让位；未生效的多档场景分类会增加状态组合，却没有对应运行时行为。

**决策**：正式写作由“动作意图 + 正式产物”组合判断；`/humanq status` 区分全局配置关闭、当前会话 `/humanq off`、静态禁用、动态配置关闭与宿主 TextPart 不可用；无效自定义词只报告数量与原因类别，不记录原始词。

**结论**：状态页反映真实能力，且不新增命令或用户配置。

### D7. 历史截断提示兼容代码的退场计划（2.2.0 更新至 v1-v6）

**背景**：`_LEGACY_RUNTIME_PREFIX` 与 `_is_legacy_truncated_runtime`（quality_rules.py 约 20 行）是为已发布版本的截断提示做的兜底，每次 `_runtime_kind` 调用都会进入该函数判断。

**决策**：保留至 v3.0，届时随 `LEGACY_STABLE_MARKERS` 中 v1–v6 的退场一并移除。移除时机由 `tests/test_quality_rules.py` 的夹具数量监控（`REAL_LEGACY_RULES` 条目减少即标志旧版用户已迁移）。2.2.0 已新增 v6 签名 `(28, 2ff44...)`。

### D8. hinted_sessions 不持久化

**背景**：`core.py` `self._hinted_sessions: set[str]` 是进程内集合。若进程在"注入提醒"后、"处理响应"前崩溃，该标志丢失，`runtime_hint_missed` 统计会漏计。

**决策**：不实现持久化。`hinted_sessions` 为进程内集合，不持久化。崩溃导致的漏计仅影响 `runtime_hint_missed` 统计精度，不影响提醒注入功能。持久化的成本（schema 扩展 + 跨重启语义）高于收益（秒级崩溃窗口的统计精度）。

## 5. 测试与发布

- 测试分两层：`core`（无宿主，`python -S scripts/run_tests.py core`）与 `host`（需 AstrBot，契约测试）。
- 发布门禁 `python scripts/build_release.py`：全部测试 + compileall + ruff + 清单校验 + metadata/CHANGELOG 版本一致性 + zip 路径安全检查，任一失败不产出归档。
- CI（`.github/workflows/ci.yml`）三层：无宿主 core、最低宿主 4.23.3 全门禁、最新 4.x host。
