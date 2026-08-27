#!/usr/bin/env python3
"""
米游社多游戏自动签到 — 防漏签增强版
========================================
三层防护: 08:00 主力签到 → 10:00 补签 → 12:00 看门狗
支持: 崩坏3 / 原神 / 崩坏：星穹铁道 / 绝区零 / 未定事件簿
通知: QQ邮箱 SMTP / Server酱 / PushPlus / 企业微信

用法:
  python main.py                  # 默认 full 模式
  python main.py --mode full      # 主力签到 (签到+通知)
  python main.py --mode check     # 看门狗检查 (只查询+告警)
  python main.py --mode retry     # 补签 (同 full, API 幂等)
"""

import argparse
import hashlib
import json
import logging
import os
import random
import smtplib
import sys
import time
import uuid
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

import requests

# ============== 路径 ==============
SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
LOG_FILE = SCRIPT_DIR / "checkin.log"
STATE_FILE = SCRIPT_DIR / ".checkin_state.json"

# ============== API ==============
API_HOST = "https://api-takumi.mihoyo.com"
ZZZ_API_HOST = "https://act-nap-api.mihoyo.com"
LUNA_INFO = "/event/luna/info"
LUNA_SIGN = "/event/luna/sign"
LUNA_HOME = "/event/luna/home"
ZZZ_INFO = "/event/luna/zzz/info"
ZZZ_SIGN = "/event/luna/zzz/sign"
ZZZ_HOME = "/event/luna/zzz/home"

# DS 签名 (2026.08)
DS_SALT_WEB = "gW20AtTxpc0V5GR3SmsytCLhVBgXtW6I"
APP_VERSION = "2.85.1"
CLIENT_TYPE = "5"

# ============== 游戏 ==============
GAMES = {
    "崩坏3": {
        "game_biz": "bh3_cn", "act_id": "e202306201626331",
        "zzz_api": False, "signgame": "bh3",
    },
    "原神": {
        "game_biz": "hk4e_cn", "act_id": "e202311201442471",
        "zzz_api": False, "signgame": "hk4e",
    },
    "崩坏：星穹铁道": {
        "game_biz": "hkrpg_cn", "act_id": "e202304121516551",
        "zzz_api": False, "signgame": "hkrpg",
    },
    "绝区零": {
        "game_biz": "nap_cn", "act_id": "e202406242138391",
        "zzz_api": True, "signgame": "zzz",
    },
    "未定事件簿": {
        "game_biz": "nxx_cn", "act_id": "e202202251749321",
        "zzz_api": False, "signgame": "nxx",
    },
}

# ============== 日志 ==============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ============== DS 签名 ==============

def generate_ds(body: Optional[dict] = None, salt: str = DS_SALT_WEB) -> str:
    t = int(time.time())
    r = random.randint(100001, 200000)
    check = f"salt={salt}&t={t}&r={r}"
    if body is not None:
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        body_md5 = hashlib.md5(body_str.encode()).hexdigest()
        check += f"&b={body_md5}"
    return f"{hashlib.md5(check.encode()).hexdigest()},{t},{r}"


# ============== HTTP ==============

def build_headers(cookie: str, device_id: str, body: Optional[dict] = None,
                  signgame: Optional[str] = None) -> dict:
    h = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            f"Mozilla/5.0 (Linux; Android 12; Unspecified Device) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Version/4.0 Chrome/103.0.5060.129 Mobile Safari/537.36 "
            f"miHoYoBBS/{APP_VERSION}"
        ),
        "Referer": "https://act.mihoyo.com/",
        "Origin": "https://act.mihoyo.com",
        "Cookie": cookie,
        "x-rpc-device_id": device_id,
        "x-rpc-client_type": CLIENT_TYPE,
        "x-rpc-app_version": APP_VERSION,
        "x-rpc-channel": "miyousheluodi",
        "X-Requested-With": "com.mihoyo.hyperion",
        "DS": generate_ds(body),
    }
    if signgame:
        h["X-Rpc-Signgame"] = signgame
    return h


def api_get(url: str, params: dict, cookie: str, device_id: str,
            signgame: Optional[str] = None) -> dict:
    h = build_headers(cookie, device_id, signgame=signgame)
    try:
        resp = requests.get(url, params=params, headers=h, timeout=15)
        return resp.json()
    except Exception as e:
        return {"retcode": -1, "message": str(e)}


def api_post(url: str, body: dict, cookie: str, device_id: str,
             signgame: Optional[str] = None) -> dict:
    h = build_headers(cookie, device_id, body=body, signgame=signgame)
    h["Content-Type"] = "application/json"
    try:
        resp = requests.post(url, json=body, headers=h, timeout=15)
        return resp.json()
    except Exception as e:
        return {"retcode": -1, "message": str(e)}


def get_game_roles(cookie: str, device_id: str, game_biz: str) -> list[dict]:
    data = api_get(
        f"{API_HOST}/binding/api/getUserGameRolesByCookie",
        {"game_biz": game_biz}, cookie, device_id,
    )
    if data.get("retcode") != 0:
        logger.warning(f"获取角色失败 [{game_biz}]: {data.get('message')}")
        return []
    return data.get("data", {}).get("list", [])


def get_checkin_info(cookie: str, device_id: str, game: dict,
                     region: str, uid: str) -> dict:
    host = ZZZ_API_HOST if game["zzz_api"] else API_HOST
    path = ZZZ_INFO if game["zzz_api"] else LUNA_INFO
    return api_get(
        f"{host}{path}",
        {"act_id": game["act_id"], "lang": "zh-cn", "region": region, "uid": uid},
        cookie, device_id, signgame=game["signgame"],
    )


def do_sign(cookie: str, device_id: str, game: dict,
            region: str, uid: str) -> dict:
    host = ZZZ_API_HOST if game["zzz_api"] else API_HOST
    path = ZZZ_SIGN if game["zzz_api"] else LUNA_SIGN
    return api_post(
        f"{host}{path}",
        {"act_id": game["act_id"], "lang": "zh-cn", "region": region, "uid": uid},
        cookie, device_id, signgame=game["signgame"],
    )


def get_rewards(cookie: str, device_id: str, game: dict) -> list:
    host = ZZZ_API_HOST if game["zzz_api"] else API_HOST
    path = ZZZ_HOME if game["zzz_api"] else LUNA_HOME
    data = api_get(
        f"{host}{path}", {"act_id": game["act_id"], "lang": "zh-cn"},
        cookie, device_id, signgame=game["signgame"],
    )
    if data.get("retcode") == 0:
        return data.get("data", {}).get("awards", [])
    return []


# ============== 状态追踪 ==============

class CheckinState:
    """签到状态记录，用于跨工作流传递"""

    def __init__(self):
        self.today = date.today().isoformat()
        self.results: list[dict] = []
        self.overall_success = False
        self.mode = "full"
        self.timestamp = ""

    def add_result(self, game: str, nickname: str, uid: str,
                   action: str, detail: str = ""):
        self.results.append({
            "game": game,
            "nickname": nickname,
            "uid": uid,
            "action": action,  # signed / already_signed / failed / skipped
            "detail": detail,
        })

    def any_signed(self) -> bool:
        return any(r["action"] in ("signed", "already_signed")
                   for r in self.results)

    def all_ok(self) -> bool:
        if not self.results:
            return False
        return all(r["action"] in ("signed", "already_signed", "skipped")
                   for r in self.results)

    def save(self):
        self.timestamp = datetime.now().isoformat()
        STATE_FILE.write_text(
            json.dumps(self.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls) -> "CheckinState":
        if not STATE_FILE.exists():
            return cls()
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            s = cls()
            s.__dict__.update(data)
            return s
        except Exception:
            return cls()


# ============== 邮件通知 ==============

def send_email(smtp_config: dict, subject: str, body: str):
    """通过 QQ邮箱 SMTP 发送通知"""
    host = smtp_config.get("host", "smtp.qq.com")
    port = smtp_config.get("port", 465)
    user = smtp_config.get("user", "")
    password = smtp_config.get("password", "")
    to_addr = smtp_config.get("to", user)

    if not user or not password:
        logger.warning("SMTP 配置不完整，跳过邮件发送")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.attach(MIMEText(body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL(host, port, timeout=10) as server:
            server.login(user, password)
            server.sendmail(user, to_addr, msg.as_string())
        logger.info(f"邮件已发送 → {to_addr}")
        return True
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


def send_push(messages: list[str], push_config: dict, title_prefix: str = ""):
    """推送通知: Server酱 / PushPlus / 企业微信"""
    if not push_config:
        return

    msg_text = "\n".join(messages)
    today = datetime.now().strftime("%Y-%m-%d")
    title = f"{title_prefix}米游社签到 {today}"

    # Server酱
    key = push_config.get("server_chan_key", "")
    if key:
        try:
            requests.post(
                f"https://sctapi.ftqq.com/{key}.send",
                data={"title": title, "desp": msg_text}, timeout=10,
            )
        except Exception:
            pass

    # PushPlus
    token = push_config.get("pushplus_token", "")
    if token:
        try:
            requests.post(
                "https://www.pushplus.plus/send",
                json={
                    "token": token, "title": title,
                    "content": msg_text.replace("\n", "<br>"),
                    "template": "html",
                }, timeout=10,
            )
        except Exception:
            pass

    # 企业微信
    wecom_key = push_config.get("wecom_bot_key", "")
    if wecom_key:
        try:
            requests.post(
                f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={wecom_key}",
                json={"msgtype": "text", "text": {"content": f"{title}\n{msg_text}"}},
                timeout=10,
            )
        except Exception:
            pass


# ============== 构建通知内容 ==============

def build_email_body(state: CheckinState, mode: str) -> str:
    """构建 HTML 邮件正文"""
    mode_labels = {
        "full": "🟢 主力签到 (08:00)",
        "retry": "🟡 补签 (10:00)",
        "check": "🔴 看门狗告警 (12:00)",
        "backup": "🛟 兜底补签 (14:00)",
    }
    label = mode_labels.get(mode, mode)

    rows = ""
    for r in state.results:
        if r["action"] == "signed":
            icon, color = "🎉", "#1D9E75"
        elif r["action"] == "already_signed":
            icon, color = "✅", "#378ADD"
        elif r["action"] == "skipped":
            icon, color = "⏭️", "#999"
        else:
            icon, color = "❌", "#E53E3E"
        rows += f"""<tr>
            <td style="padding:6px 12px;border-bottom:1px solid #eee">{icon}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #eee">{r['game']}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;color:#888;font-size:13px">{r['nickname']}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;color:{color}">{r['detail']}</td>
        </tr>"""

    overall = "✅ 全部成功" if state.all_ok() else "⚠️ 存在失败"
    overall_color = "#1D9E75" if state.all_ok() else "#E53E3E"

    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:500px;margin:0 auto;padding:16px">
<div style="background:#f0f4ff;padding:16px;border-radius:12px 12px 0 0">
    <h2 style="margin:0;color:#333">{label}</h2>
    <p style="margin:4px 0 0;color:#888;font-size:13px">{state.timestamp}</p>
</div>
<div style="padding:8px 0">
    <table style="width:100%;border-collapse:collapse">{rows}</table>
</div>
<div style="background:#f9f9f9;padding:12px;border-radius:0 0 12px 12px;text-align:center">
    <span style="font-weight:bold;color:{overall_color};font-size:16px">{overall}</span>
</div>
<p style="color:#aaa;font-size:11px;text-align:center;margin-top:16px">
    MiyouCheckin · 防漏签系统 · {state.today}
</p>
</body></html>"""


# ============== 核心签到逻辑 ==============

def process_game(game_name: str, game: dict, cookie: str, device_id: str,
                 mode: str, state: CheckinState) -> str:
    """处理单个游戏的签到"""
    roles = get_game_roles(cookie, device_id, game["game_biz"])
    if not roles:
        state.add_result(game_name, "-", "-", "skipped", "未绑定角色")
        return f"  ⚠️ {game_name}: 未绑定角色"

    role = roles[0]
    region = role.get("region", "")
    uid = role.get("game_uid", "")
    nickname = role.get("nickname", uid)

    # 查询状态
    info = get_checkin_info(cookie, device_id, game, region, uid)
    retcode = info.get("retcode", -1)
    data = info.get("data", {})
    is_sign = data.get("is_sign", False)
    total_days = data.get("total_sign_day", 0)

    if retcode != 0 and mode != "check":
        state.add_result(game_name, nickname, uid, "failed",
                         f"[{retcode}] {info.get('message', '查询失败')}")
        return f"  ❌ {game_name}({nickname}): [{retcode}] 查询失败"

    if retcode != 0 and mode == "check":
        state.add_result(game_name, nickname, uid, "failed",
                         f"[{retcode}] 查询失败")
        return f"  ❌ {game_name}({nickname}): 看门狗检测到查询异常"

    if is_sign:
        state.add_result(game_name, nickname, uid, "already_signed",
                         f"已签 {total_days} 天")
        return f"  ✅ {game_name}({nickname}): 已签 {total_days} 天"

    # check 模式只查询不签到
    if mode == "check":
        state.add_result(game_name, nickname, uid, "failed",
                         f"未签到! (已签{total_days}天)")
        return f"  🔴 {game_name}({nickname}): 全天未签到! 需手动处理"

    # 执行签到 (full/retry 模式)
    for attempt in range(1, 4):  # 最多重试 3 次
        result = do_sign(cookie, device_id, game, region, uid)
        sign_retcode = result.get("retcode", -1)
        sign_data = result.get("data", {})

        if sign_retcode == 0 and sign_data.get("success") == 0:
            new_total = total_days + 1
            award_name = ""
            awards = get_rewards(cookie, device_id, game)
            if awards and new_total <= len(awards):
                award = awards[new_total - 1]
                award_name = f" — {award.get('cnt', '')}x{award.get('name', '')}"
            state.add_result(game_name, nickname, uid, "signed",
                             f"签到成功! 累计{new_total}天{award_name}")
            return f"  🎉 {game_name}({nickname}): 签到成功! 累计{new_total}天{award_name}"

        if sign_retcode == -5003:
            state.add_result(game_name, nickname, uid, "already_signed",
                             f"已签 {total_days} 天")
            return f"  ✅ {game_name}({nickname}): 已签 {total_days} 天"

        if sign_data.get("is_risk") or sign_retcode == -3001:
            if attempt < 3:
                time.sleep(3)
                continue
            state.add_result(game_name, nickname, uid, "failed", "需手动验证")
            return f"  ⚠️ {game_name}({nickname}): 需手动验证"

        if attempt < 3:
            logger.warning(f"  {game_name} 第{attempt}次失败，{2*attempt}秒后重试...")
            time.sleep(2 * attempt)
            continue

        state.add_result(game_name, nickname, uid, "failed",
                         f"[{sign_retcode}] {result.get('message', '失败')}")
        return f"  ❌ {game_name}({nickname}): [{sign_retcode}] {result.get('message', '失败')}"

    state.add_result(game_name, nickname, uid, "failed", "多次重试失败")
    return f"  ❌ {game_name}({nickname}): 多次重试失败"


# ============== 账号处理 ==============

def checkin_account(account: dict, mode: str, state: CheckinState) -> list[str]:
    cookie = account.get("cookie", "")
    device_id = account.get("device_id", str(uuid.uuid4()).upper())
    account_name = account.get("name", "默认账号")
    games_whitelist = account.get("games", None)

    if not cookie:
        return [f"❌ [{account_name}] Cookie 为空"]

    messages = [f"📱 [{account_name}]"]
    for game_name, game in GAMES.items():
        if games_whitelist and game_name not in games_whitelist:
            continue
        msg = process_game(game_name, game, cookie, device_id, mode, state)
        messages.append(msg)
        time.sleep(random.uniform(0.5, 1.5))

    return messages


# ============== 配置 ==============

def load_config() -> dict:
    # 优先从环境变量读取（GitHub Actions）
    env_config = os.environ.get("MIYOUSHE_CONFIG")
    if env_config:
        return json.loads(env_config)

    if not CONFIG_FILE.exists():
        logger.error(f"配置文件不存在: {CONFIG_FILE}")
        logger.info("请从 config.example.json 复制并填入你的 Cookie")
        sys.exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_smtp_config(config: dict) -> dict:
    """合并 config.json 和环境变量中的 SMTP 配置"""
    smtp = config.get("smtp", {})
    return {
        "host": os.environ.get("SMTP_HOST", smtp.get("host", "smtp.qq.com")),
        "port": int(os.environ.get("SMTP_PORT", smtp.get("port", 465))),
        "user": os.environ.get("SMTP_USER", smtp.get("user", "")),
        "password": os.environ.get("SMTP_PASSWORD", smtp.get("password", "")),
        "to": os.environ.get("SMTP_TO", smtp.get("to", "")),
    }


# ============== 主流程 ==============

def main():
    parser = argparse.ArgumentParser(description="米游社多游戏自动签到")
    parser.add_argument(
        "--mode", choices=["full", "retry", "check", "backup"], default="full",
        help="full=主力签到 | retry=补签 | check=看门狗检查 | backup=兜底补签(14:00)",
    )
    parser.add_argument(
        "--no-email", action="store_true", help="禁用邮件通知"
    )
    args = parser.parse_args()

    mode = args.mode
    now = datetime.now()
    state = CheckinState()
    state.mode = mode
    state.today = now.strftime("%Y-%m-%d")

    logger.info("=" * 50)
    mode_labels = {"full": "主力签到", "retry": "补签", "check": "看门狗检查", "backup": "兜底补签"}
    logger.info(f"米游社签到 [{mode_labels.get(mode, mode)}] {now.strftime('%Y-%m-%d %H:%M')}")
    logger.info("=" * 50)

    config = load_config()
    accounts = config.get("accounts", [])
    if not accounts:
        logger.error("未配置账号")
        sys.exit(1)

    all_messages = []
    for i, account in enumerate(accounts):
        if not account.get("enabled", True):
            continue
        msgs = checkin_account(account, mode, state)
        all_messages.extend(msgs)
        if i < len(accounts) - 1:
            time.sleep(2)

    state.save()

    # 输出
    logger.info("-" * 45)
    for msg in all_messages:
        logger.info(msg)
    logger.info("-" * 45)

    # ===== 通知 =====
    push_config = config.get("push", {})

    # check 模式特殊处理
    if mode == "check":
        unsigned = [r for r in state.results if r["action"] == "failed"]
        if unsigned:
            # 有未签到的游戏 → 紧急告警
            alert_msgs = [f"🚨 看门狗告警: {len(unsigned)} 个游戏全天未签到!"]
            alert_msgs.extend(all_messages)
            all_messages = alert_msgs
            # 强制发邮件告警
            if not args.no_email:
                smtp_config = get_smtp_config(config)
                body = build_email_body(state, "check")
                send_email(smtp_config, f"🚨 [紧急] 米游社签到告警 {state.today}", body)
            send_push(all_messages, push_config, "🚨 [紧急] ")
        else:
            logger.info("看门狗: 所有游戏签到正常 ✓")

    elif mode in ("full", "retry"):
        if not args.no_email:
            smtp_config = get_smtp_config(config)
            if smtp_config.get("user") and smtp_config.get("password"):
                subject = f"{'✅' if state.all_ok() else '⚠️'} 米游社签到 {state.today} [{mode_labels.get(mode, mode)}]"
                body = build_email_body(state, mode)
                send_email(smtp_config, subject, body)

        if push_config:
            send_push(all_messages, push_config)

    elif mode == "backup":
        # 兜底模式（14:00）：早上 schedule 漏跑时补签；全部已签则静默不打扰
        has_new = any(r["action"] == "signed" for r in state.results)
        has_fail = any(r["action"] == "failed" for r in state.results)
        if has_fail:
            if not args.no_email:
                smtp_config = get_smtp_config(config)
                if smtp_config.get("user") and smtp_config.get("password"):
                    body = build_email_body(state, "backup")
                    send_email(smtp_config, f"⚠️ 米游社兜底检查异常 {state.today}", body)
            send_push(all_messages, push_config, "⚠️ ")
        elif has_new:
            if not args.no_email:
                smtp_config = get_smtp_config(config)
                if smtp_config.get("user") and smtp_config.get("password"):
                    body = build_email_body(state, "backup")
                    send_email(smtp_config, f"🛟 米游社兜底补签完成 {state.today}", body)
            send_push(all_messages, push_config, "🛟 ")
        else:
            logger.info("兜底检查: 今日已全部签到，静默")

    logger.info("签到完成!")

    # 看门狗模式: 如果有失败则 exit 1 (让 GitHub Actions 感知失败)
    if mode == "check":
        unsigned = [r for r in state.results if r["action"] == "failed"]
        if unsigned:
            sys.exit(1)


if __name__ == "__main__":
    main()
