# -*- coding: utf-8 -*-
"""
サンライズ出雲 個室空席ウォッチャー
- 非公式ツール「Sunrise Checker」(https://sunrise-checker.com/) が公開している
  空席表を1時間に1回だけ読み、狙いの個室が「×→○/△」に変わったらGmail通知
- データ出典は同サイト。予約は e5489 / 駅の窓口で
"""

import json
import os
import re
import smtplib
import time
from datetime import date, datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ============================================================
# ▼▼▼ 設定（ここだけ書き換えればOK） ▼▼▼
# ============================================================

N_WEEKENDS = 2          # 直近いくつの週末を見るか
TARGET_DATES = []       # 特定日指定（例: ["20260918","20260921"]）。空なら自動

# 監視する方面と曜日（週末旅行の動線がデフォルト）
TABLES = [
    ("サンライズ出雲 下り 東京→出雲市", "table_down_izumo.html", "金土"),
    ("サンライズ出雲 上り 出雲市→東京", "table_up_izumo.html",   "日"),
    # 瀬戸も見たい場合は下の#を外す
    # ("サンライズ瀬戸 下り 東京→高松", "table_down_seto.html", "金土"),
    # ("サンライズ瀬戸 上り 高松→東京", "table_up_seto.html",   "日"),
]

# 監視する個室タイプ
TARGET_ROOMS = ["シングルデラックス", "シングルツイン", "シングル", "サンライズツイン"]
# 「ソロ」「ノビノビ座席」も見たければ上のリストに追加

QUIET_HOURS = {0, 1, 2, 3, 4, 5}   # 元データが更新されない深夜帯はお休み
WAIT = 2.0

# ============================================================
# ▲▲▲ 設定ここまで ▲▲▲
# ============================================================

BASE = "https://sunrise-checker.com"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
STATE_FILE = "state_sunrise.json"
JST = ZoneInfo("Asia/Tokyo")
WD = "月火水木金土日"


def build_target_dates():
    today = datetime.now(JST).date()
    env_dates = os.environ.get("SUNRISE_DATES", "").strip()
    targets = [x for x in env_dates.split(",") if x] if env_dates else TARGET_DATES
    if targets:
        # 日付を直接指定した場合は曜日フィルタを無効化
        return [date(int(s[:4]), int(s[4:6]), int(s[6:8])) for s in targets], True
    dates = []
    fri = today + timedelta(days=(4 - today.weekday()) % 7)
    for i in range(N_WEEKENDS):
        for off in (0, 1, 2):  # 金土日
            d = fri + timedelta(days=7 * i + off)
            if d >= today:
                dates.append(d)
    return dates, False


def parse_table(html):
    """空席表 → {date: {部屋タイプ: (ベスト記号, 喫煙のみか)}} と最終更新時刻文字列"""
    soup = BeautifulSoup(html, "html.parser")
    updated = ""
    m = re.search(r"最終更新日時[:：]\s*([\d年月日:\s]+)", soup.get_text())
    if m:
        updated = m.group(1).strip()
    tbl = soup.find("table")
    rows = tbl.find_all("tr")
    # 1行目: 設備名(colspan)、2行目: 禁煙/喫煙
    cats, cat_ths = [], rows[0].find_all("th")[1:]
    for th in cat_ths:
        cats += [th.get_text(strip=True)] * int(th.get("colspan", 1))
    subs = [th.get_text(strip=True) for th in rows[1].find_all("th")[1:]]
    today = datetime.now(JST).date()
    order = {"○": 3, "△": 2, "×": 1, "-": 0, "−": 0}
    result = {}
    for tr in rows[2:]:
        tds = tr.find_all("td")
        if not tds:
            continue
        dm = re.match(r"(\d{1,2})/(\d{1,2})", tds[0].get_text(strip=True))
        if not dm:
            continue
        mo, dy = int(dm.group(1)), int(dm.group(2))
        d = date(today.year, mo, dy)
        if d < today - timedelta(days=180):
            d = date(today.year + 1, mo, dy)
        marks = {}
        for cat, sub, td in zip(cats, subs, [t.get_text(strip=True) for t in tds[1:]]):
            best, smoke_only = marks.get(cat, ("-", False))
            mk = td if td in order else "-"
            if order[mk] > order[best]:
                marks[cat] = (mk, sub == "喫煙")
            elif order[mk] == order[best] and sub == "禁煙":
                marks[cat] = (mk, False)
        result[d] = marks
    return result, updated


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def send_mail(subject, body):
    user = os.environ.get("GMAIL_ADDRESS")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not pw:
        print("=== メール設定なし（テストモード）: 以下を送信する想定 ===")
        print("件名:", subject)
        print(body)
        return
    to = (os.environ.get("MAIL_TO") or "").strip() or user
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as sv:
        sv.login(user, pw)
        sv.sendmail(user, [to], msg.as_string())


def main():
    now = datetime.now(JST)
    if now.hour in QUIET_HOURS and os.environ.get("FORCE_RUN") != "1":
        print(f"[{now:%H:%M}] 深夜帯のためスキップ")
        return

    dates, explicit = build_target_dates()
    print("監視対象日:", ", ".join(f"{d:%m/%d}({WD[d.weekday()]})" for d in dates))

    state = load_state()
    new_openings, snapshot, errors = [], [], []
    updated_at = ""
    s = requests.Session()
    s.headers.update(UA)

    for label, path, days in TABLES:
        try:
            r = s.get(f"{BASE}/{path}", timeout=20)
            r.encoding = "utf-8"
            table, updated = parse_table(r.text)
            updated_at = updated or updated_at
        except Exception as e:
            errors.append(f"{label}: {e}")
            print(f"  ! {label}: {e}")
            continue
        hit = 0
        for d in dates:
            if (not explicit and WD[d.weekday()] not in days) or d not in table:
                continue
            hit += 1
            for room in TARGET_ROOMS:
                mk, smoke_only = table[d].get(room, ("-", False))
                if mk == "-":
                    continue
                key = f"{d:%Y%m%d}|{label}|{room}"
                available = mk in ("○", "△")
                suffix = "（喫煙のみ）" if smoke_only else ""
                line = f"{d:%m/%d}({WD[d.weekday()]}) {label}  {room}〔{mk}〕{suffix}"
                if available:
                    snapshot.append("○ " + line)
                    if not state.get(key, False):
                        new_openings.append("★ " + line)
                        print("  ★ 空き発生:", line)
                state[key] = available
        print(f"  済 {label}: {hit}日分確認（データ更新 {updated}）")
        time.sleep(WAIT)

    save_state(state)

    if new_openings:
        subject = f"【サンライズ個室】空きが出ました！（{len(new_openings)}件）"
        body = (
            "サンライズの個室に新しい空きを検知しました。早い者勝ちです！\n"
            "予約 → e5489（JR西日本）または駅の窓口・指定席券売機へ\n\n"
            "■ 新たに空いた個室\n" + "\n".join(new_openings) + "\n\n"
            "■ いま空いている監視対象（全体）\n"
            + ("\n".join(snapshot) if snapshot else "（なし）") + "\n\n"
            "記号: ○=空席あり △=残りわずか\n"
            f"データ時点: {updated_at}\n"
            f"チェック時刻: {now:%Y/%m/%d %H:%M} JST\n"
            "出典: Sunrise Checker（非公式） https://sunrise-checker.com/"
        )
        send_mail(subject, body)
        print(f"通知メール送信: {len(new_openings)}件")
    else:
        print("新しい空きなし" + (f"（現在空き{len(snapshot)}件は通知済み）" if snapshot else ""))

    if errors:
        print("エラー:", *errors, sep="\n  ")


if __name__ == "__main__":
    main()
