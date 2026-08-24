# -*- coding: utf-8 -*-
"""
サフィール踊り子 プレミアムグリーン空席監視ボット（JRサイバーステーション版）
- 「満席→空き」に変わった時だけGmailで通知
- ※個室（4名/6名）はサイト仕様上照会不可
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

# 監視する週末の数（金・土・日をひとまとまりとして直近から）
N_WEEKENDS = 2

# 特定日だけ監視したい場合（例: ["20260904","20260905"]）。空なら上の自動計算
TARGET_DATES = []

# 監視メニュー：(ラベル, 乗車駅コード, 降車駅コード, 検索開始時刻, 対象曜日, 列車名キーワード, 監視座席)
# 不要な行は先頭に # を付けて無効化
WATCHES = [
    ("サフィール踊り子 下り 東京→伊豆急下田", "4000", "9708", ["07", "12"], "土日", "サフィ", ["プレミアムグリーン"]),
    ("サフィール踊り子 上り 伊豆急下田→東京", "9708", "4000", ["12", "16"], "土日", "サフィ", ["プレミアムグリーン"]),
    # グリーン車も見たい場合 → 上2行の ["プレミアムグリーン"] を ["グリーン車", "プレミアムグリーン"] に
    # サンライズのノビノビ座席（普通車扱い）も見たい場合は下の#を外す
    # ("サンライズ出雲 下り 東京→出雲市", "4000", "6540", ["21"], "金土", "サンライズ", ["普通車"]),
    # ("サンライズ瀬戸 下り 東京→高松",   "4000", "7000", ["21"], "金土", "サンライズ", ["普通車"]),
    # ("サンライズ出雲 上り 出雲市→東京", "6540", "4000", ["18"], "日",   "サンライズ", ["普通車"]),
    # ("サンライズ瀬戸 上り 高松→東京",   "7000", "4000", ["20"], "日",   "サンライズ", ["普通車"]),
]

# サイバーステーションの照会時間は6:00〜23:50。深夜帯はスキップ
QUIET_HOURS = {0, 1, 2, 3, 4, 5}

WAIT = 1.0  # リクエスト間隔（秒）

# ============================================================
# ▲▲▲ 設定ここまで ▲▲▲
# ============================================================

BASE = "https://www.jr.cyberstation.ne.jp"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}
STATE_FILE = "state_jr.json"
JST = ZoneInfo("Asia/Tokyo")
WD = "月火水木金土日"


def build_target_dates():
    today = datetime.now(JST).date()
    limit = today + timedelta(days=31)
    env_dates = os.environ.get("JR_DATES", "").strip()
    targets = [x for x in env_dates.split(",") if x] if env_dates else TARGET_DATES
    if targets:
        out = []
        for s in targets:
            d = date(int(s[:4]), int(s[4:6]), int(s[6:8]))
            if today <= d <= limit:
                out.append(d)
        return out, True  # 日付直接指定なら曜日フィルタ無効
    dates = []
    fri = today + timedelta(days=(4 - today.weekday()) % 7)  # 次の金曜(今日が金なら今日)
    for i in range(N_WEEKENDS):
        for off in (0, 1, 2):  # 金土日
            d = fri + timedelta(days=7 * i + off)
            if today <= d <= limit:
                dates.append(d)
    return dates, False


def new_session():
    s = requests.Session()
    s.headers.update(UA)
    s.get(f"{BASE}/", timeout=20)
    time.sleep(WAIT)
    s.get(f"{BASE}/jcs/VacancyInput.do",
          headers={"Referer": f"{BASE}/"}, timeout=20)
    return s


def query_vacancy(s, d, dep_code, arr_code, hh):
    """1回の空席照会。列車リスト [{name, dep, arr, marks:{カテゴリ:記号}}] を返す"""
    data = {
        "lang": "ja", "month": str(d.month), "day": str(d.day),
        "hour": hh, "minute": "00", "train": "5",
        "dep_stnpb": dep_code, "arr_stnpb": arr_code, "script": "1",
    }
    r = s.post(f"{BASE}/jcs/Vacancy.do", data=data,
               headers={"Referer": f"{BASE}/jcs/VacancyInput.do"}, timeout=20)
    soup = BeautifulSoup(r.text, "html.parser")
    tbl = None
    for t in soup.find_all("table"):
        if t.find("span", class_="table_train_name"):
            tbl = t
            break
    if tbl is None:
        return []  # 発売前・時間外・該当なし
    # ヘッダーから列カテゴリを組み立て（colspan考慮）
    cols = []
    thead = tbl.find("thead")
    if thead:
        for th in thead.find_all("th"):
            img = th.find("img")
            if img and img.get("alt") in ("普通車", "グリーン車", "グランクラス", "プレミアムグリーン"):
                cols += [img["alt"]] * int(th.get("colspan", 1))
    trains = []
    for tr in tbl.find_all("tr"):
        name_el = tr.find("span", class_="table_train_name")
        if not name_el:
            continue
        name = re.sub(r"[\s\u3000]+", "", name_el.get_text())
        time_el = tr.find("span", class_="table_vacancy_time")
        times = re.findall(r"\d{1,2}:\d{2}", time_el.get_text()) if time_el else []
        marks = {}
        cells = [td.get_text(strip=True) for td in tr.find_all("td")[1:]]
        for cat, mk in zip(cols, cells):
            cur = marks.get(cat, "-")
            # 禁煙/喫煙2列は良い方を採用（○>△>×>-）
            order = {"○": 3, "△": 2, "×": 1, "-": 0}
            if order.get(mk, 0) > order.get(cur, 0):
                marks[cat] = mk
        trains.append({
            "name": name,
            "dep": times[0] if times else "",
            "arr": times[1] if len(times) > 1 else "",
            "marks": marks,
        })
    return trains


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
        print(f"[{now:%H:%M}] 照会時間外のためスキップ")
        return

    dates, explicit = build_target_dates()
    if not dates:
        print("監視対象日なし")
        return
    print("監視対象日:", ", ".join(f"{d:%m/%d}({WD[d.weekday()]})" for d in dates))

    state = load_state()
    new_openings, snapshot, errors = [], [], []
    s = new_session()

    for d in dates:
        wd = WD[d.weekday()]
        for label, dep_c, arr_c, hours, days, kw, seats in WATCHES:
            if not explicit and wd not in days:
                continue
            seen = set()
            found = 0
            for hh in hours:
                time.sleep(WAIT)
                try:
                    trains = query_vacancy(s, d, dep_c, arr_c, hh)
                except Exception as e:
                    errors.append(f"{d:%m/%d} {label} {hh}時: {e}")
                    continue
                for t in trains:
                    if kw not in t["name"] or t["name"] in seen:
                        continue
                    seen.add(t["name"])
                    found += 1
                    for cat in seats:
                        mk = t["marks"].get(cat, "-")
                        if mk == "-":
                            continue
                        seat_disp = "ノビノビ座席" if (kw == "サンライズ" and cat == "普通車") else cat
                        key = f"{d:%Y%m%d}|{label}|{t['name']}|{cat}"
                        available = mk in ("○", "△")
                        line = (f"{d:%m/%d}({wd}) {t['name']} {t['dep']}発 "
                                f"{label.split(' ')[-1]}  {seat_disp}〔{mk}〕")
                        if available:
                            snapshot.append("○ " + line)
                            if not state.get(key, False):
                                new_openings.append("★ " + line)
                                print("  ★ 空き発生:", line)
                        state[key] = available
            print(f"  済 {d:%m/%d}({wd}) {label}: {found}本確認")

    save_state(state)

    if new_openings:
        subject = f"【サフィール】プレミアムグリーンに空き！（{len(new_openings)}件）"
        body = (
            "サフィール踊り子に新しい空きを検知しました。早い者勝ちです！\n"
            "予約 → えきねっと または駅の窓口・指定席券売機へ\n\n"
            "■ 新たに空いた座席\n" + "\n".join(new_openings) + "\n\n"
            "■ いま空いている監視対象（全体）\n"
            + ("\n".join(snapshot) if snapshot else "（なし）") + "\n\n"
            "記号: ○=空席あり(11席以上) △=残りわずか(1〜10席)\n"
            f"チェック時刻: {now:%Y/%m/%d %H:%M} JST\n"
            "出典: JRサイバーステーション空席案内（個室は照会対象外）"
        )
        send_mail(subject, body)
        print(f"通知メール送信: {len(new_openings)}件")
    else:
        print("新しい空きなし" + (f"（現在空き{len(snapshot)}件は通知済み）" if snapshot else ""))

    if errors:
        print("エラー:", *errors, sep="\n  ")


if __name__ == "__main__":
    main()
