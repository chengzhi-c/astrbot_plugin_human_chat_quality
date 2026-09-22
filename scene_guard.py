"""会话场景守卫：判定请求是否属于正式写作 / 文艺创作 / 粘性续写。

只做纯文本判定，不持有状态；粘性 TTL 状态与让位编排归 core。
词表对齐上游 natural-talk 生成模式的核心规则（正式文稿与 fiction 让位）。
"""

from __future__ import annotations

import re

from .constants import STICKY_FOLLOWUP_MAX_LEN
from .protocols import MessageEventProtocol


def event_text(event: MessageEventProtocol | None) -> str:
    """宿主对象形状随版本变化，逐属性探测是有意的。"""
    if event is not None:
        for attr in ("get_message_str", "message_str", "message", "text"):
            try:
                v = getattr(event, attr, None)
                if callable(v):
                    v = v()
                if isinstance(v, str) and v.strip():
                    return v.strip()
            except Exception:
                continue
    return ""


_FORMAL_ACTIONS = re.compile(r"写|撰写|起草|拟定|拟(?!定)|润色|改写|改成|改这篇|修改|生成|翻译|输出")
_FORMAL_ARTIFACTS = re.compile(
    r"论文|摘要|公文|演讲稿|营销文案|法律(?:文书|声明)|合同|会议纪要|(?:正式)?道歉声明|正式声明|新闻稿|采购申请|正式通知|变更通知|服务通知|研究计划|求职邮件|周报|日报|公告|汇报"
)
# 技术系统排除：产物词与系统词可跨少量名词性修饰（"通知推送的代码"、"周报汇总的服务"）。
# 修饰位不得含传达类动词（说明/告知/介绍…）——"写个公告说明服务下线"是正式公告，不是技术件。
_TECH_SYSTEM_SUFFIXES = re.compile(
    r"(?:合同|论文|公文|会议纪要|通知|公告|周报|日报|汇报)"
    r"(?:(?!(?:说明|告知|介绍|描述|解释|讲清|说明白))[^，。；！？\n]){0,6}"
    r"(?:系统|平台|模块|表结构|数据库|接口|代码|算法|架构|逻辑|组件|服务|队列|脚本|函数|查重)"
)
# 通知类的起草动词：量词全覆盖（写个/写份/写一封/拟个/拟一份/拟一篇…）
_NOTICE_DRAFT = re.compile(r"拟定|起草|撰写|(?:写|拟)(?:个|一份|份|一封|一篇|篇)")
# 私域场景豁免：私下叮嘱与群内知会不是正式文体
_PRIVATE_NOTICE = re.compile(r"朋友|同学|家人|今晚|聚餐")
_PRIVATE_ARTIFACTS = re.compile(r"通知|公告")
_CREATIVE_ACTIONS = re.compile(r"写|创作|续写|扮演|roleplay", re.IGNORECASE)
_CREATIVE_GENRES = re.compile(r"小说|故事|同人|角色卡|剧本|角色扮演|roleplay", re.IGNORECASE)


def is_formal_writing_request(event: MessageEventProtocol | None) -> bool:
    text = event_text(event)
    if not text:
        return False
    # 技术系统与私域场景优先让行，再判正式文体，否则会接管技术问答与私下叮嘱
    if _TECH_SYSTEM_SUFFIXES.search(text):
        return False
    if _PRIVATE_NOTICE.search(text) and _PRIVATE_ARTIFACTS.search(text):
        return False
    if _NOTICE_DRAFT.search(text) and "通知" in text:
        return True
    return bool(_FORMAL_ACTIONS.search(text) and _FORMAL_ARTIFACTS.search(text))


_USER_STORY = re.compile(r"用户故事|user story", re.IGNORECASE)


def is_creative_writing_request(event: MessageEventProtocol | None) -> bool:
    text = event_text(event)
    if not text:
        return False
    # 「用户故事 / user story」是产品任务，不是体裁；屏蔽后再看还有没有真体裁。
    text = _USER_STORY.sub(" ", text)
    return bool(_CREATIVE_ACTIONS.search(text) and _CREATIVE_GENRES.search(text))


_STICKY_EXACT = re.compile(
    r"^(继续|然后|接着|再改一下|按这个写|同上|continue|go on)[。.!！?？]*$",
    re.IGNORECASE,
)
_STICKY_WRITE = re.compile(r"^(?:继续|接着|再).*(?:写|改|润色|拟)")


def is_sticky_followup(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > STICKY_FOLLOWUP_MAX_LEN:
        return False
    return bool(_STICKY_EXACT.fullmatch(stripped) or _STICKY_WRITE.search(stripped))
