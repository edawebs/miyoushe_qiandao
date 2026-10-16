#!/usr/bin/env python3
"""
跨仓库健康巡检（"第五层 · 跨仓库版"）

为什么需要它
------------
2026-10-15 / 10-16，贴吧仓库（tiebaSignIn）连续两天零运行。根因不在代码：
该仓库是从别人仓库 Fork 来的，GitHub 会周期性清理 Fork 仓库上的工作流
以节省算力。被清理后 ——
  · 定时任务不跑
  · 手动/API 触发被拒（422 Actions has been disabled）
  · **任何内置看门狗也跑不起来**，所以全程静默、零告警
这就是"静默失效"：仓库自己监控自己，一旦整体停摆就彻底没人报信。

本脚本住在**非 Fork 的米游社仓库**里，从外部核验其余仓库，专门兜住这类场景。

核验方式
--------
1) 贴吧（权威信号）
   读取仓库内被工作流提交的状态文件 cache/signin-status.json，
   比对其 date 字段是否等于北京时间今天。该文件只有真正签完才会更新，
   因此"日期过期"即等价于"今天没签成功"——无论什么原因。

2) 米游社 / 森空岛（辅助信号）
   查询其今日是否有成功的 workflow run（公开 API，无需鉴权）。
   取不到（限流、网络抖动）只记录为提示、**不告警**，避免误报。

使用
----
  python cross_check.py              # 正常巡检，仅异常时发邮件并以非零码退出
  python cross_check.py --self-test  # 自检：强制发一封测试告警，验证邮件通道

告警邮件沿用本项目既有 SMTP 约定（SMTP_HOST/PORT/USER/PASSWORD/TO）。
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

import requests

CST = timezone(timedelta(hours=8))
OWNER = "edawebs"
TIMEOUT = 20

# 贴吧状态文件（工作流每次签完都会提交）
TIEBA_STATUS_URL = (
    f"https://raw.githubusercontent.com/{OWNER}/tiebaSignIn/master/"
    "cache/signin-status.json"
)

# 借助运行记录核验的仓库：(仓库名, 显示名)
RUN_TARGETS = [
    ("miyoushe_qiandao", "米游社"),
    ("skland-checkin", "森空岛"),
]


def now_cst() -> datetime:
    """当前北京时间。判断"今天"必须用北京时间，与各仓库脚本保持一致。"""
    return datetime.now(CST)


def to_cst_date(iso: str) -> str:
    """把 GitHub 的 UTC 时间串转成北京日期。"""
    dt = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(CST).strftime("%Y-%m-%d")


def check_tieba(today: str):
    """返回问题描述；正常则返回 None。"""
    url = f"{TIEBA_STATUS_URL}?t={int(now_cst().timestamp())}"
    try:
        r = requests.get(url, timeout=TIMEOUT)
    except requests.RequestException as e:
        return f"贴吧：状态文件读取失败（{type(e).__name__}）"
    if r.status_code != 200:
        return f"贴吧：状态文件读取失败（HTTP {r.status_code}）"
    try:
        data = r.json()
    except ValueError:
        return "贴吧：状态文件解析失败（内容不是 JSON）"

    date = (data.get("date") or "").strip()
    if date != today:
        counters = ""
        if data.get("total") is not None:
            counters = (f"，上次成绩 成功{data.get('success')}/失败{data.get('fail')}"
                        f"/共{data.get('total')}")
        return f"贴吧：签到状态停留在 {date or '未知'}，不是今天 {today}{counters}"
    return None


def check_runs(repo: str, disp: str, today: str):
    """返回 (问题, 提示)，二者至少一个为 None。"""
    url = f"https://api.github.com/repos/{OWNER}/{repo}/actions/runs?per_page=40"
    try:
        r = requests.get(url, timeout=TIMEOUT,
                         headers={"Accept": "application/vnd.github+json"})
    except requests.RequestException as e:
        return None, f"{disp}：今日运行情况无法核验（{type(e).__name__}）"
    if r.status_code != 200:
        return None, f"{disp}：今日运行情况无法核验（HTTP {r.status_code}，可能接口限流）"

    runs = r.json().get("workflow_runs", [])
    today_runs = [x for x in runs if to_cst_date(x.get("created_at", "")) == today]
    if not today_runs:
        return f"{disp}：今天（{today}）没有任何运行记录", None
    if not any(x.get("conclusion") == "success" for x in today_runs):
        return f"{disp}：今天有 {len(today_runs)} 次运行，但无一成功", None
    return None, None


def send_mail(subject: str, body: str) -> bool:
    host = os.environ.get("SMTP_HOST", "smtp.qq.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    to_list = [x.strip() for x in (os.environ.get("SMTP_TO", "") or user).split(",") if x.strip()]

    if not (user and password and to_list):
        print("SMTP 配置不完整，跳过邮件发送")
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to_list)
    try:
        with smtplib.SMTP_SSL(host, port, timeout=15) as server:
            server.login(user, password)
            server.sendmail(user, to_list, msg.as_string())
        print("告警邮件已发送 →", ", ".join(to_list))
        return True
    except Exception as e:  # noqa: BLE001
        print("邮件发送失败：", e)
        return False


def build_body(issues, notes, stamp: str, today: str) -> str:
    lines = [
        "【签到系统 · 跨仓库巡检告警】",
        f"检查时间：{stamp}（北京时间）",
        "",
        "发现问题：",
    ]
    lines += [f"  ✗ {i}" for i in issues]
    if notes:
        lines += ["", "补充提示（不影响判定）："]
        lines += [f"  · {n}" for n in notes]
    lines += [
        "",
        "常见原因与处置：",
        "  1. 仓库是 Fork —— GitHub 会清理 Fork 上的工作流，表现为突然零运行。",
        "     处置：打开该仓库的 Actions 页，点绿色按钮",
        "     「I understand my workflows, go ahead and re-enable them」。",
        "  2. 定时任务被平台丢弃或延迟数小时 —— 通常下一周期自愈，连续两天以上才需处理。",
        "  3. 凭据失效 —— 检查该仓库 Secrets（贴吧 BDUSS/PTOKEN；米游社/森空岛 Cookie）。",
        "",
        f"贴吧仓库：https://github.com/{OWNER}/tiebaSignIn/actions",
        f"米游社仓库：https://github.com/{OWNER}/miyoushe_qiandao/actions",
        f"森空岛仓库：https://github.com/{OWNER}/skland-checkin/actions",
        "",
        "本节告警由米游社仓库的 cross_check.py 发出（跨仓库巡检，每日 21:30）。",
    ]
    return "\n".join(lines)


def main() -> int:
    today = now_cst().strftime("%Y-%m-%d")
    stamp = now_cst().strftime("%Y-%m-%d %H:%M:%S")
    self_test = "--self-test" in sys.argv

    issues, notes = [], []

    if self_test:
        issues.append("（这是自检测试产生的假告警，可忽略；收到即代表告警通道正常）")
    else:
        # 1) 贴吧：权威信号
        msg = check_tieba(today)
        if msg:
            issues.append(msg)
        # 2) 米游社 / 森空岛：辅助信号（取不到不算故障）
        for repo, disp in RUN_TARGETS:
            issue, note = check_runs(repo, disp, today)
            if issue:
                issues.append(issue)
            if note:
                notes.append(note)

    if not issues:
        print(f"[{stamp}] 巡检正常：贴吧状态文件已更新至 {today}，各仓库今日均有成功运行。")
        for n in notes:
            print("  提示：" + n)
        return 0

    body = build_body(issues, notes, stamp, today)
    print(body)
    prefix = "🧪 [自检]" if self_test else "🚨 [跨仓库巡检]"
    sent = send_mail(f"{prefix} 签到异常 {today}", body)
    if self_test:
        # 自检的唯一目的就是验证邮件通道，发不出去必须报错，否则自检形同虚设
        return 0 if sent else 1
    # 异常时非零退出，让 run 在 Actions 页面标红，多一重可见性
    return 1


if __name__ == "__main__":
    sys.exit(main())
