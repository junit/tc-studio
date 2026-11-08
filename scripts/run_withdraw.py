#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tencent Cloud IM (即时通信 IM) 单聊消息批量撤回脚本
文档参考: https://cloud.tencent.com/document/product/269/38980
"""

import argparse
import base64
import csv
import hashlib
import hmac
import json
import os
import random
import sys
import time
import zlib
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.namespace import get_env


def gen_user_sig(sdkappid: int, key: str, identifier: str, expire: int = 86400 * 7) -> str:
    """生成腾讯云 IM UserSig (TLSSigAPIv2)"""
    curr_time = int(time.time())
    sig_doc = {
        "TLS.ver": "2.0",
        "TLS.identifier": str(identifier),
        "TLS.sdkappid": int(sdkappid),
        "TLS.expire": int(expire),
        "TLS.time": curr_time,
    }
    base_content = (
        f"TLS.identifier:{identifier}\n"
        f"TLS.sdkappid:{sdkappid}\n"
        f"TLS.time:{curr_time}\n"
        f"TLS.expire:{expire}\n"
    )
    sig = hmac.new(key.encode("utf-8"), base_content.encode("utf-8"), hashlib.sha256).digest()
    sig_doc["TLS.sig"] = base64.b64encode(sig).decode("utf-8")

    compressed = zlib.compress(json.dumps(sig_doc).encode("utf-8"))
    base64_str = base64.b64encode(compressed).decode("utf-8")
    return base64_str.replace("+", "*").replace("/", "-").replace("=", "_")


def post_json(url: str, payload: dict, timeout: int = 15) -> dict:
    """发送 HTTP POST JSON 请求"""
    data_bytes = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data_bytes, headers={"Content-Type": "application/json;charset=utf-8"}, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        return {"ActionStatus": "FAIL", "ErrorCode": e.code, "ErrorInfo": f"HTTP {e.code}: {err_body}"}
    except URLError as e:
        return {"ActionStatus": "FAIL", "ErrorCode": -1, "ErrorInfo": f"URLError: {e.reason}"}
    except Exception as e:
        return {"ActionStatus": "FAIL", "ErrorCode": -2, "ErrorInfo": str(e)}


def withdraw_message(domain: str, sdkappid: int, admin_user: str, usersig: str,
                     from_account: str, to_account: str, msg_key: str) -> dict:
    """调用腾讯云 IM 单聊消息撤回接口"""
    rand = random.randint(0, 4294967295)
    url = f"https://{domain}/v4/openim/admin_msgwithdraw?sdkappid={sdkappid}&identifier={admin_user}&usersig={usersig}&random={rand}&contenttype=json"
    payload = {
        "From_Account": str(from_account),
        "To_Account": str(to_account),
        "MsgKey": str(msg_key)
    }
    return post_json(url, payload)


def load_tasks(task_path: str) -> list:
    """读取待撤回任务列表 (支持 JSON 或 CSV)"""
    if not os.path.exists(task_path):
        raise FileNotFoundError(f"任务文件不存在: {task_path}")

    tasks = []
    if task_path.endswith(".csv"):
        with open(task_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                tasks.append(row)
    else:
        with open(task_path, "r", encoding="utf-8") as f:
            tasks = json.load(f)
    return tasks


def main():
    parser = argparse.ArgumentParser(description="腾讯云 IM 单聊消息批量撤回工具")
    parser.add_argument("--config", default="config.json", help="配置文件路径 (默认: config.json)")
    parser.add_argument("--tasks", default="withdraw_tasks.json", help="任务文件路径 (默认: withdraw_tasks.json)")
    parser.add_argument("--sdkappid", type=int, help="腾讯云 IM SDKAppID")
    parser.add_argument("--secret-key", help="腾讯云 IM 控制台密钥 (用于计算 UserSig)")
    parser.add_argument("--admin-user", default="administrator", help="管理员账号 (默认: administrator)")
    parser.add_argument("--usersig", help="预先生成的有效 UserSig (如提供则无需 secret-key)")
    parser.add_argument("--domain", default="console.tim.qq.com", help="API 域名 (默认: console.tim.qq.com)")
    parser.add_argument("--delay", type=float, default=0.05, help="请求间隔秒数 (默认 0.05s, 即约 20 QPS)")
    parser.add_argument("--execute", action="store_true", help="正式执行网络撤回请求 (若未指定此参数，默认以 --dry-run 预检模式运行)")
    parser.add_argument("--dry-run", action="store_true", help="预检模式，仅打印任务清单不发送真实请求")
    parser.add_argument("--resume", action="store_true", help="断点续传: 自动跳过输出结果中已成功的项")
    parser.add_argument("--limit", type=int, default=0, help="限制本次执行条数 (例如 --limit 1 仅执行 1 条，默认 0 表示全部)")
    parser.add_argument("--output", default="withdraw_results.json", help="输出结果 JSON 路径")
    parser.add_argument("--output-csv", default="withdraw_results.csv", help="输出结果 CSV 路径")

    args = parser.parse_args()

    # 读取配置文件合并参数
    config_path = args.config
    if not os.path.exists(config_path):
        parent_config = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", args.config))
        if os.path.exists(parent_config):
            config_path = parent_config

    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                print(f"[INFO] 已加载配置文件: {config_path}")
        except Exception as e:
            print(f"[WARN] 加载配置文件失败: {e}")

    sdkappid = args.sdkappid or config.get("sdkappid") or int(get_env("SDKAPPID", default="0") or 0)
    secret_key = args.secret_key or config.get("secret_key") or get_env("SECRET_KEY", default="") or ""
    admin_user = args.admin_user or config.get("admin_user", "administrator")
    usersig = args.usersig or config.get("usersig") or get_env("USERSIG", default="") or ""
    domain = args.domain or config.get("domain", "console.tim.qq.com")
    delay = args.delay if args.delay != 0.05 else config.get("delay_sec", 0.05)

    tasks = load_tasks(args.tasks)
    if args.limit and args.limit > 0:
        tasks = tasks[:args.limit]
        print(f"[INFO] 已指定 --limit {args.limit}，仅处理前 {len(tasks)} 条消息")
    total_tasks = len(tasks)
    print(f"[INFO] 共读取到 {total_tasks} 条待撤回消息")

    is_dry_run = not args.execute or args.dry_run
    if is_dry_run:
        print("\n=======================================================")
        print("【DRY-RUN 预检模式】(未传入 --execute，不会发送真实网络请求)")
        print(f"  * SDKAppID: {sdkappid or '(未配置)'}")
        print(f"  * Admin User: {admin_user}")
        print(f"  * 待撤回消息数: {total_tasks}")
        print("-------------------------------------------------------")
        print("前 5 条待撤回消息预览:")
        for i, t in enumerate(tasks[:5], start=1):
            print(f"  [{i}] 发送方: {t.get('from_account')} -> 接收方: {t.get('to_account')} | MsgKey: {t.get('msg_key')}")
            print(f"      内容: {t.get('text_preview', '')}")
        print("-------------------------------------------------------")
        print("若确认执行真实撤回，请在配置好 config.json (sdkappid 与 secret_key) 后执行:")
        print("   python3 run_withdraw.py --execute")
        print("=======================================================\n")
        return

    # 正式执行检查鉴权
    if not sdkappid:
        print("[ERROR] 缺失 SDKAppID，请在 config.json、环境变量或 --sdkappid 参数中指定。")
        sys.exit(1)

    if not usersig:
        if not secret_key or "YOUR_SECRET_KEY" in secret_key:
            print("[ERROR] 缺失 SecretKey 或 UserSig，请在 config.json、环境变量或参数中指定有效密钥。")
            sys.exit(1)
        print("[INFO] 正在根据 SDKAppID 和 SecretKey 生成管理员 UserSig...")
        usersig = gen_user_sig(sdkappid, secret_key, admin_user)

    # 处理断点续传
    existing_results = {}
    if args.resume and os.path.exists(args.output):
        try:
            with open(args.output, "r", encoding="utf-8") as f:
                prev_data = json.load(f)
                for item in prev_data:
                    if item.get("status") == "OK" and item.get("err_code") == 0:
                        existing_results[item.get("msg_key")] = item
            print(f"[INFO] 断点续传已启用，跳过已成功的 {len(existing_results)} 条记录")
        except Exception as e:
            print(f"[WARN] 读取历史执行结果失败: {e}")

    results = []
    success_count = 0
    fail_count = 0
    skip_count = 0

    print(f"\n[INFO] 开始执行批量撤回任务 (总计: {total_tasks} 条, 间隔: {delay}s)...")
    start_time = time.time()

    try:
        for idx, task in enumerate(tasks, start=1):
            from_account = task.get("from_account")
            to_account = task.get("to_account")
            msg_key = task.get("msg_key")

            if msg_key in existing_results:
                skip_count += 1
                results.append(existing_results[msg_key])
                continue

            resp = withdraw_message(domain, sdkappid, admin_user, usersig, from_account, to_account, msg_key)
            action_status = resp.get("ActionStatus", "FAIL")
            err_code = resp.get("ErrorCode", -1)
            err_info = resp.get("ErrorInfo", "")

            is_ok = (action_status == "OK" and err_code == 0)
            if is_ok:
                success_count += 1
                tag = "\033[32m[SUCCESS]\033[0m"
            else:
                fail_count += 1
                tag = "\033[31m[FAILED]\033[0m"

            print(f"[{idx}/{total_tasks}] {tag} MsgKey: {msg_key} | {from_account} -> {to_account} | Code: {err_code} | Info: {err_info}")

            task_result = {
                **task,
                "status": action_status,
                "err_code": err_code,
                "err_info": err_info,
                "timestamp": int(time.time())
            }
            results.append(task_result)

            if delay > 0:
                time.sleep(delay)

    except KeyboardInterrupt:
        print("\n[WARN] 任务被用户中断，正在保存已执行的结果...")
    finally:
        # 保存 JSON 结果
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        # 保存 CSV 结果
        if results:
            fieldnames = list(results[0].keys())
            with open(args.output_csv, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(results)

        elapsed = time.time() - start_time
        print("\n================== 执行结果汇总 ==================")
        print(f"总任务数:   {total_tasks}")
        print(f"本次执行:   {len(results) - skip_count}")
        print(f"撤回成功:   {success_count}")
        print(f"撤回失败:   {fail_count}")
        print(f"跳过历史:   {skip_count}")
        print(f"耗时:       {elapsed:.2f} 秒")
        print(f"结果文件:   {args.output} 和 {args.output_csv}")
        print("==================================================\n")


if __name__ == "__main__":
    main()
