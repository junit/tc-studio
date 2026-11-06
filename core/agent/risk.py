"""Shared high-risk API classification and confirmation helpers.

API callers must classify a resolved registry entry rather than trusting the
alias supplied by a client or model.  The registry is intentionally imported
inside the resolver to keep this module independent from registry discovery
startup.
"""

from __future__ import annotations

from typing import Any, Iterable

from .constants import HIGH_RISK_API_KEYWORDS


# Commands whose effects are destructive or difficult to reverse.  Keep this
# set explicit; configurable keyword matching is retained for existing local
# policy customisation, but aliases must always resolve to one of these values.
HIGH_RISK_CANONICAL_ACTIONS = frozenset(
    {
        "destroy_group",
        "account_delete",
        "clean_roam_msg",
        "delete_group_member",
        "delete_all_msg",
        "delete_group_msg_by_sender",
        "delete_c2c_msg_ramble",
        "c2c_delete_ramble",
        "destroy_topic",
        "destroy_group_topic",
        "change_group_owner",
        "modify_admin",
        "droplivestream",
        "forbidlivestream",
        "deleteliverecordrule",
        "deletelivetranscoderule",
        "setnospeaking",
        "push_all_member",
        # IM destructive/admin operations that do not share the older
        # keyword names above.
        "account_kick",
        "c2c_msg_withdraw",
        "follow_follow_delete",
        "group_forbid_send_msg",
        "group_msg_recall",
        "group_open_http_svc_ban_group_member",
        "group_open_http_svc_clear_group_attr",
        "group_open_http_svc_clear_group_msg",
        "group_open_http_svc_delete_group_attr",
        "group_open_http_svc_delete_group_counter",
        "group_open_http_svc_delete_permission_group_member",
        "group_open_http_svc_delete_topic_permission_group",
        "official_destroy",
        "official_msg_recall",
        "recent_contact_delete",
        "robot_delete",
        "sns_black_list_delete",
        "sns_friend_delete",
        "sns_friend_delete_all",
        "sns_group_delete",
        "timpush_clear_all_tags",
        "timpush_remove_tag",
        "tim_102309",
        "tim_103967",
        "tim_111053",
        "tim_94304",
        # Live destructive actions.  The catalog contains many resource
        # deletion variants, so the marker rule below is intentionally also
        # applied to future/unknown actions.
        "close_source_stream",
        "drop_live_stream",
        "forbid_live_domain",
        "forbid_live_stream",
    }
)

# A newly added cloud action must not become an approval bypass merely because
# the local registry has not learned it yet.  These markers are restricted to
# destructive verbs and are evaluated only against the canonical action/path
# identities, never arbitrary payload values.
HIGH_RISK_NAME_MARKERS = (
    "delete",
    "destroy",
    "clear",
    "clean",
    "drop",
    "forbid",
    "recall",
    "withdraw",
    "kick",
    "ban",
    "remove",
    "close",
    "setnospeaking",
    "modify_admin",
    "change_group_owner",
    "push_all_member",
)


def resolve_api(target_name: str) -> Any | None:
    from ..registry import registry

    return registry.get_api(target_name)


def api_identities(target_name: str, api_meta: Any | None = None) -> list[str]:
    """Return all stable names for an API, including the submitted alias."""
    values: list[str] = [str(target_name or "")]
    if api_meta is not None:
        # A bare service name must not be an approval identity: accepting
        # ``approved_high_risk_paths=["live"]`` would blanket-approve every
        # destructive action in the live service.  The service is retained
        # only as part of the canonical ``service/command`` paths below.
        for attr in (
            "api_id",
            "command",
            "action",
            "name",
            "full_command",
        ):
            value = getattr(api_meta, attr, "")
            if value:
                values.append(str(value))
        service = str(getattr(api_meta, "service_name", "") or "")
        command = str(getattr(api_meta, "command", "") or "")
        if service and command:
            values.extend((f"{service}/{command}", f"v4/{service}/{command}"))
    return list(dict.fromkeys(value.strip().lower() for value in values if value.strip()))


def is_high_risk_api(
    target_name: str,
    api_meta: Any | None = None,
    keywords: Iterable[str] | None = None,
) -> bool:
    """Classify an API after alias resolution, with configurable keywords."""
    identities = api_identities(target_name, api_meta)
    command = str(
        getattr(api_meta, "command", "") or getattr(api_meta, "action", "") or ""
    ).strip().lower()
    api_id = str(getattr(api_meta, "api_id", "") or "").strip().lower()
    if command in HIGH_RISK_CANONICAL_ACTIONS or api_id in HIGH_RISK_CANONICAL_ACTIONS:
        return True
    configured = tuple(str(item).strip().lower() for item in (keywords or HIGH_RISK_API_KEYWORDS) if str(item).strip())
    blob = " ".join(identities)
    if any(marker in blob for marker in HIGH_RISK_NAME_MARKERS):
        return True
    return any(keyword in blob for keyword in configured)


def matching_confirmation_paths(target_name: str, api_meta: Any | None = None) -> set[str]:
    """Return canonical and submitted paths accepted by explicit confirmation."""
    return set(api_identities(target_name, api_meta))

