# -*- coding: utf-8 -*-
"""
批量调用测试 (Runner) 预设场景模版与智能 QPS 风控治理规则
"""

import os
from typing import List, Dict, Any, Optional

from core.runtime_paths import runtime_docs_dir, runtime_storage_dir

STORAGE_DIR = str(runtime_storage_dir())
DOCS_DIR = str(runtime_docs_dir())

# 1. 核心高频运维场景预设清单
BATCH_PRESETS = [
    {
        "id": "preset_c2c_withdraw",
        "service_type": "im",
        "title": "批量撤回违规单聊消息",
        "tag": "高频应急",
        "tag_color": "rose",
        "api_id": "c2c_msg_withdraw",
        "command": "v4/openim/admin_msgwithdraw",
        "description": "线上出现涉黄涉诈、违禁言论或误发消息时，指定会话双方及 MsgKey 批量极速撤回",
        "filename": "c2c_withdraw_template.csv",
        "columns": ["from_account", "to_account", "msg_key", "text_preview"],
        "recommended_delay": 0.05,
        "sample_rows": [
            {
                "from_account": "user_demo_01",
                "to_account": "user_demo_02",
                "msg_key": "3190_12345678_1700000001",
                "text_preview": "测试违规引流广告内容样本 A"
            },
            {
                "from_account": "user_demo_01",
                "to_account": "user_demo_02",
                "msg_key": "3190_12345678_1700000002",
                "text_preview": "测试误发营销消息内容样本 B"
            },
            {
                "from_account": "user_demo_03",
                "to_account": "user_demo_04",
                "msg_key": "3190_12345678_1700000003",
                "text_preview": "违规敏感测试内容样本 C"
            }
        ],
        "has_today_data": False,
    },
    {
        "id": "preset_group_msg_recall",
        "service_type": "im",
        "title": "批量撤回群聊违规消息",
        "tag": "群组应急",
        "tag_color": "rose",
        "api_id": "group_msg_recall",
        "command": "v4/group_open_http_svc/group_msg_recall",
        "description": "群内遭遇黑产广告刷屏或违规发言时，指定 GroupId 与 MsgSeq 批量撤回消息",
        "filename": "group_msg_recall_template.csv",
        "columns": ["group_id", "msg_seq", "text_preview"],
        "recommended_delay": 0.05,
        "sample_rows": [
            {
                "group_id": "@TGS#2J4SDFK3",
                "msg_seq": "10023",
                "text_preview": "违规引流广告刷屏A"
            },
            {
                "group_id": "@TGS#2J4SDFK3",
                "msg_seq": "10024",
                "text_preview": "违规引流广告刷屏B"
            },
            {
                "group_id": "@TGS#2J4SDFK3",
                "msg_seq": "10025",
                "text_preview": "违规恶意刷屏内容C"
            }
        ],
        "has_today_data": False,
    },
    {
        "id": "preset_nospeaking",
        "service_type": "im",
        "title": "批量全局禁言 / 解禁用户",
        "tag": "安全治理",
        "tag_color": "amber",
        "api_id": "op_set_no_speaking",
        "command": "v4/openconfigsvr/setnospeaking",
        "description": "对黑灰产或违规营销号批量设置单聊/群聊禁言秒数（86400 为 1 天，0 为立即解禁）",
        "filename": "nospeaking_template.csv",
        "columns": ["set_account", "c2cmsg_nospeaking_time", "groupmsg_nospeaking_time", "reason"],
        "recommended_delay": 0.05,
        "sample_rows": [
            {
                "set_account": "spammer_user_01",
                "c2cmsg_nospeaking_time": "86400",
                "groupmsg_nospeaking_time": "86400",
                "reason": "发布欺诈引流信息禁言1天"
            },
            {
                "set_account": "spammer_user_02",
                "c2cmsg_nospeaking_time": "604800",
                "groupmsg_nospeaking_time": "604800",
                "reason": "高频广告刷屏禁言7天"
            },
            {
                "set_account": "repaired_user_03",
                "c2cmsg_nospeaking_time": "0",
                "groupmsg_nospeaking_time": "0",
                "reason": "申诉审核通过解禁恢复"
            }
        ],
        "has_today_data": False,
    },
    {
        "id": "preset_delete_member",
        "service_type": "im",
        "title": "批量踢出群成员 (清理死粉/违规)",
        "tag": "社群治理",
        "tag_color": "blue",
        "api_id": "group_delete_member",
        "command": "v4/group_open_http_svc/delete_group_member",
        "description": "批量移出群内违规账号或僵尸粉，支持设置是否静默踢人（Silence=1 为静默）",
        "filename": "group_delete_member_template.csv",
        "columns": ["group_id", "member_to_del_account", "reason", "silence"],
        "recommended_delay": 0.1,
        "sample_rows": [
            {
                "group_id": "@TGS#1SAMPLE_GROUP",
                "member_to_del_account": "spammer_991",
                "reason": "群内恶意引流广告",
                "silence": "1"
            },
            {
                "group_id": "@TGS#1SAMPLE_GROUP",
                "member_to_del_account": "spammer_992",
                "reason": "违规发送敏感文件",
                "silence": "1"
            }
        ],
        "has_today_data": False,
    },
    {
        "id": "preset_portrait_set",
        "service_type": "im",
        "title": "批量同步用户资料 (昵称/头像)",
        "tag": "账号同步",
        "tag_color": "emerald",
        "api_id": "profile_portrait_set",
        "command": "v4/profile/portrait_set",
        "description": "系统数据割接或新用户导入时，批量设定用户的昵称、头像 URL 与签名",
        "filename": "portrait_set_template.csv",
        "columns": ["from_account", "nick", "face_url", "self_signature"],
        "recommended_delay": 0.05,
        "sample_rows": [
            {
                "from_account": "vip_user_001",
                "nick": "张经理 (企业专属顾问)",
                "face_url": "https://img.sample.com/avatar1.png",
                "self_signature": "随时为您提供咨询"
            },
            {
                "from_account": "vip_user_002",
                "nick": "李工程师 (高级技术专家)",
                "face_url": "https://img.sample.com/avatar2.png",
                "self_signature": "专注IM技术赋能"
            }
        ],
        "has_today_data": False,
    },
    {
        "id": "preset_destroy_group",
        "service_type": "im",
        "title": "批量解散过期/违规群聊",
        "tag": "业务清理",
        "tag_color": "slate",
        "api_id": "group_destroy",
        "command": "v4/group_open_http_svc/destroy_group",
        "description": "业务下线、临时活动到期或内测完毕的群聊，指定群 ID 批量销毁注销",
        "filename": "destroy_group_template.csv",
        "columns": ["group_id", "remark"],
        "recommended_delay": 0.1,
        "sample_rows": [
            {
                "group_id": "@TGS#1EXPIRED_GROUP_01",
                "remark": "2026春季促销临时群销毁"
            },
            {
                "group_id": "@TGS#1EXPIRED_GROUP_02",
                "remark": "沙箱内测群清理"
            }
        ],
        "has_today_data": False,
    },
    {
        "id": "preset_live_drop_stream",
        "service_type": "live",
        "title": "云直播：批量断开违规直播推流",
        "tag": "直播应急",
        "tag_color": "rose",
        "api_id": "live_DropLiveStream",
        "command": "DropLiveStream",
        "description": "监控发现主播推流违规或音视频异常时，指定 StreamName 与推流域名批量断流",
        "filename": "live_drop_stream_template.csv",
        "columns": ["domain_name", "app_name", "stream_name", "remark"],
        "recommended_delay": 0.05,
        "sample_rows": [
            {
                "domain_name": "push.yourdomain.com",
                "app_name": "live",
                "stream_name": "room_1001",
                "remark": "涉嫌违规内容紧急断流"
            },
            {
                "domain_name": "push.yourdomain.com",
                "app_name": "live",
                "stream_name": "room_1002",
                "remark": "推流码率严重超标异常"
            }
        ],
        "has_today_data": False,
    },
    {
        "id": "preset_live_check_state",
        "service_type": "live",
        "title": "云直播：批量对账查询流在线状态",
        "tag": "直播监控",
        "tag_color": "amber",
        "api_id": "live_DescribeLiveStreamState",
        "command": "DescribeLiveStreamState",
        "description": "大促或开播活动期间，批量查询各直播间流在线/断流状态 (active/inactive/forbid)",
        "filename": "live_check_state_template.csv",
        "columns": ["domain_name", "app_name", "stream_name"],
        "recommended_delay": 0.05,
        "sample_rows": [
            {
                "domain_name": "play.yourdomain.com",
                "app_name": "live",
                "stream_name": "room_1001"
            },
            {
                "domain_name": "play.yourdomain.com",
                "app_name": "live",
                "stream_name": "room_1002"
            },
            {
                "domain_name": "play.yourdomain.com",
                "app_name": "live",
                "stream_name": "room_1003"
            }
        ],
        "has_today_data": False,
    }
]


# 2. 智能 QPS 与风控规则库
API_BATCH_RULES = {
    # 消息发送
    "c2c_send_msg": {
        "native_alternative": {
            "api_id": "c2c_batch_send_msg",
            "name": "批量发单聊消息",
            "command": "v4/openim/batchsendmsg"
        },
        "tip": "💡 官方已提供原生「批量发单聊消息 (batchsendmsg)」接口，单次请求可直发 500 人，速度提升数十倍且更不易触发频控！",
        "risk_level": "NORMAL",
        "min_delay": 0.05,
    },
    # 账号导入
    "account_import": {
        "native_alternative": {
            "api_id": "multiaccount_import",
            "name": "导入多个账号",
            "command": "v4/im_open_login_svc/multiaccount_import"
        },
        "tip": "💡 大批量新用户入库时，官方推荐使用「导入多个账号 (multiaccount_import)」，单次调用最多可导入 100 人！",
        "risk_level": "NORMAL",
        "min_delay": 0.02,
    },
    # 全员推送 (高危)
    "all_member_push": {
        "risk_level": "HIGH_RISK",
        "tip": "⚠️ 全员广播属于强管控高危接口，官方每日调用配额极低。批量循环极易导致当日配额耗尽或触发控制台权限风控封停！强烈建议开启「预览模式 (Dry-Run)」演练！",
        "min_delay": 1.0,
        "recommended_delay": 2.0,
    },
    "tim_111048": {
        "risk_level": "HIGH_RISK",
        "tip": "⚠️ 全员/标签广播属于强管控高危接口，官方每日调用配额极低。批量循环极易导致当日配额耗尽或触发控制台权限风控封停！强烈建议开启「预览模式 (Dry-Run)」演练！",
        "min_delay": 1.0,
        "recommended_delay": 2.0,
    },
    # AI 翻译 (低频扣费)
    "im_open_translate_http_svc_batch_trans_text": {
        "risk_level": "AI_EXPENSIVE",
        "tip": "💰 批量文本翻译为按字符计费服务，官方最高调用频率严格限制为 5次/秒。系统已自动限制执行间隔 ≥200ms，避免频控失败与意外扣费！",
        "min_delay": 0.2,
        "recommended_delay": 0.3,
    },
    # 单例配置
    "get_ip_list": {
        "risk_level": "SINGLETON",
        "tip": "ℹ️ 该接口为全局单例配置，返回整个 SDKAppID 对应的腾讯云服务器 IP 列表，针对不同数据行批量调用无业务意义。",
        "min_delay": 0.2,
    },
    "get_app_info": {
        "risk_level": "SINGLETON",
        "tip": "ℹ️ 该接口返回整个应用的全局运行统计信息，批量调用无实质业务意义。",
        "min_delay": 0.2,
    }
}


def get_api_batch_guidance(api_id: str) -> Dict[str, Any]:
    """
    根据 API ID 计算智能批量调用建议、QPS 限速规则与风控提示
    """
    rule = API_BATCH_RULES.get(api_id, {})

    # 尝试从官方持久化文档中读取 rate_limit 字段
    rate_limit_str = ""
    min_delay = rule.get("min_delay", 0.02)
    recommended_delay = rule.get("recommended_delay", 0.05)
    risk_level = rule.get("risk_level", "NORMAL")
    tip = rule.get("tip")

    # 读取运行期 docs 缓存；未覆盖时由 doc_syncer 使用随包 fallback。
    try:
        from .registry import registry
        api_obj = registry.get_api(api_id)
        if api_obj:
            from .agent.risk import is_high_risk_api
            if is_high_risk_api(api_id, api_obj):
                risk_level = "HIGH_RISK"
                min_delay = max(min_delay, 1.0)
                recommended_delay = max(recommended_delay, 1.0)
                if not tip:
                    tip = "⚠️ 该接口会删除、撤回、禁用或修改云端资源，批量执行前必须核对目标范围并显式确认。"
        if api_obj and api_obj.doc_id:
            from .doc_syncer import get_doc_by_id

            doc_data = get_doc_by_id(api_obj.doc_id)
            if doc_data:
                rate_limit_str = doc_data.get("rate_limit", "")

                if "5次/秒" in rate_limit_str or "5 次/秒" in rate_limit_str:
                    min_delay = max(min_delay, 0.2)
                    recommended_delay = max(recommended_delay, 0.3)
                    if not tip:
                        tip = f"官方最高调用频率限制为 5 次/秒，建议执行间隔 ≥200ms。"
                elif "20次/秒" in rate_limit_str or "20 次/秒" in rate_limit_str:
                    min_delay = max(min_delay, 0.05)
                    recommended_delay = max(recommended_delay, 0.1)
                elif "100次/秒" in rate_limit_str or "100 次/秒" in rate_limit_str:
                    min_delay = max(min_delay, 0.02)
                    recommended_delay = max(recommended_delay, 0.05)
    except Exception as e:
        print(f"[WARN] Failed to load rate limit for {api_id}: {e}")

    return {
        "api_id": api_id,
        "rate_limit": rate_limit_str,
        "risk_level": risk_level,
        "min_delay": min_delay,
        "recommended_delay": recommended_delay,
        "native_alternative": rule.get("native_alternative"),
        "tip": tip
    }
