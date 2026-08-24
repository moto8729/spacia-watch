# -*- coding: utf-8 -*-
"""
スペーシアX 特別座席 空席監視ボット（トブチケ！対応版）
- コックピットラウンジ / ボックスシート / コンパートメント / コックピットスイート を監視
- 空きが「新たに発生」したときだけGmailで通知
- GitHub Actions で1時間ごとに自動実行する想定
"""

import json
import os
import re
import smtplib
import sys
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

# 監視する週末の数（直近から数えて）。2なら「次の土日＋その次の土日」
N_WEEKENDS = 2

# 特定の日だけ監視したい場合はここに書く（例: ["20260905", "20260906"]）
# 空リスト [] のままなら上のN_WEEKENDSで自動計算
TARGET_DATES = []

# 監視する区間（不要な行は先頭に # を付けて無効化）
ROUTES = [
    ("浅草", "1102", "東武日光", "3215"),      # 下り 日光方面
    ("浅草", "1102", "鬼怒川温泉", "4208"),    # 下り 鬼怒川方面
    ("東武日光", "3215", "浅草", "1102"),      # 上り 日光発
    ("鬼怒川温泉", "4208", "浅草", "1102"),    # 上り 鬼怒川発
]

# 監視する座席種別（不要なら行頭に # ）
TARGET_SEATS = {
    "3": "コックピットラウンジ",
    "4": "ボックスシート",
    "5": "コンパートメント",
    "6": "コックピットスイート",
}

# 空席照会するときの人数（実際に乗る人数にすると精度が上がります）
ADULT_NUM = "1"
CHILD_NUM = "0"

# 深夜はサイトメンテナンスが多いのでスキップする時間帯（JST、この時台は実行しない）
QUIET_HOURS = {0, 1, 2, 3, 4, 5}

# リクエスト間の待ち時間（秒）。サーバーへの礼儀なので短くしすぎない
WAIT = 0.6

# ============================================================
# ▲▲▲ 設定ここまで ▲▲▲
# ============================================================

BASE = "https://tobuchike.jp"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}
SEAT_NAMES = {
    "1": "スタンダードシート",
    "2": "プレミアムシート",
    "3": "コックピットラウンジ",
    "4": "ボックスシート",
    "5": "コンパートメント",
    "6": "コックピットスイート",
}
STATE_FILE = "state.json"
JST = ZoneInfo("Asia/Tokyo")


# ------------------------------------------------------------
# 日付まわり
# ------------------------------------------------------------
def build_target_dates():
    """監視対象の日付リスト（発売済み=1ヶ月以内のみ）を返す"""
    today = datetime.now(JST).date()
    limit = today + timedelta(days=31)  # トブチケは乗車1ヶ月前から発売
    env_dates = os.environ.get("SPACIA_DATES", "").strip()
    targets = [x for x in env_dates.split(",") if x] if env_dates else TARGET_DATES
    if targets:
        out = []
        for s in targets:
            d = date(int(s[:4]), int(s[4:6]), int(s[6:8]))
            if today <= d <= limit:
                out.append(d)
        return out
    # 直近N週末の土日
    dates = []
    d = today
    weekends = 0
    while weekends < N_WEEKENDS:
        # 次の土曜へ
        sat = d + timedelta(days=(5 - d.weekday()) % 7)
        sun = sat + timedelta(days=1)
        for x in (sat, sun):
            if today <= x <= limit and x not in dates:
                dates.append(x)
        weekends += 1
        d = sat + timedelta(days=7)
    return dates


# ------------------------------------------------------------
# トブチケ！アクセス層
# ------------------------------------------------------------
class RouteError(Exception):
    pass


def get_tab(html):
    el = BeautifulSoup(html, "html.parser").find("input", {"name": "tab_id"})
    return el["value"] if el else None


def new_session():
    """トップ→特急列車→ゲスト購入 と進み、検索可能なセッションを作る"""
    s = requests.Session()
    s.headers.update(UA)
    s.get(f"{BASE}/shop/default.aspx", timeout=30)
    time.sleep(WAIT)
    r = s.post(f"{BASE}/shop/default.aspx", data={"linetype": "1", "next": "1"}, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")
    tok = soup.find("input", {"name": "crsirefo_hidden"})
    if not tok:
        raise RouteError("ゲスト入口の取得に失敗")
    time.sleep(WAIT)
    r2 = s.post(
        f"{BASE}/shop/ticket/guest/trainsearch.aspx?linetype=1",
        data={"order": "購入へ進む", "crsirefo_hidden": tok["value"]},
        timeout=30,
    )
    tab = get_tab(r2.text)
    if not tab:
        raise RouteError("検索フォームの取得に失敗")
    return s, tab


def do_search(s, tab, ymd, dep_code, arr_code, hh):
    data = {
        "tab_id": tab, "linetype": "1", "order_id": "",
        "ride_dt": ymd, "calendar": "",
        "brdngTm_tm": hh, "brdngTm_fld": "1",
        "search_method": "station",
        "depart_route": "0", "depart_station": dep_code, "depart_station_all": dep_code,
        "arrival_route": "0", "arrival_station": arr_code, "arrival_station_all": arr_code,
        "adult_num": ADULT_NUM, "child_num": CHILD_NUM,
        "search.x": "10", "search.y": "10",
    }
    r = s.post(f"{BASE}/shop/ticket/guest/trainsearch.aspx", data=data, timeout=30)
    return r.text, r.url


def do_research(s, tab, hh):
    """結果ページの「時間を変更して再検索」"""
    r = s.post(
        f"{BASE}/shop/ticket/guest/trainlist.aspx",
        data={"tab_id": tab, "brdngTm_tm": hh, "brdngTm_fld": "1", "tm_search.x": "x"},
        timeout=30,
    )
    return r.text, r.url


def parse_trains(html):
    """結果ページから (列車名, 発時刻, trnNo or None=満席) を抽出"""
    soup = BeautifulSoup(html, "html.parser")
    trains = []
    for item in soup.select(".block-train-search--item"):
        name_el = item.select_one(".block-train-search--name")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if "号" not in name:
            continue
        times = [t.get_text(strip=True) for t in
                 item.select(".block-train-search--arrival-departure-condition-time")]
        if not times:
            continue
        inp = item.find("input", {"name": "trnNo"})
        full = inp is None and "満" in item.get_text()
        trains.append({
            "name": name,
            "dep": times[0],
            "arr": times[1] if len(times) > 1 else "",
            "trnNo": inp["value"] if inp else None,
            "full": full,
        })
    return trains


def drill_seat_types(s, tab_list, trn_no):
    """列車を選択→座席位置選択画面から空席のある座席種別を取得。
    戻り値: (available_set, 復帰後のtrainlist用tab_id)"""
    r = s.post(
        f"{BASE}/shop/ticket/guest/trainlist.aspx",
        data={"trnNo": trn_no, "tab_id": tab_list, "choice.x": "x"},
        timeout=30,
    )
    if "seattype" not in r.url:
        raise RouteError(f"座席選択画面に進めない (trnNo={trn_no})")
    disabled = set(re.findall(
        r'TicketSeatTypeSpaciaX_(\d)"\)\.prop\("disabled", true\)', r.text))
    exists = set(re.findall(r'id="TicketSeatTypeSpaciaX_(\d)"', r.text))
    avail = exists - disabled
    tab_seat = get_tab(r.text)
    time.sleep(WAIT)
    # 前の画面に戻る（次の列車を選べる状態へ）
    r2 = s.post(
        f"{BASE}/shop/ticket/guest/seattype.aspx",
        data={"tab_id": tab_seat, "return.x": "x"},
        timeout=30,
    )
    if "trainlist" not in r2.url:
        raise RouteError("列車一覧へ戻れない")
    return avail, get_tab(r2.text)


def check_route(ymd, dep_name, dep_code, arr_name, arr_code):
    """1区間・1日分のスペーシアX全列車の座席種別空席状況を返す
    戻り値: {列車名: {"dep":.., "arr":.., "avail": set() or None(満席)}}"""
    s, tab = new_session()
    time.sleep(WAIT)
    html, url = do_search(s, tab, ymd, dep_code, arr_code, "04")
    if "trainlist" not in url:
        # 列車なし・発売前などは静かにスキップ
        return {}
    results = {}
    seen = set()
    windows = ["10", "14", "17", "20"]  # 最初の検索は04時、その後この窓で再検索
    while True:
        trains = parse_trains(html)
        tab = get_tab(html)
        for t in trains:
            if t["name"] in seen:
                continue
            seen.add(t["name"])
            if "スペーシアX" not in t["name"]:
                continue
            if t["full"]:
                results[t["name"]] = {"dep": t["dep"], "arr": t["arr"], "avail": set()}
                continue
            time.sleep(WAIT)
            avail, tab = drill_seat_types(s, tab, t["trnNo"])
            results[t["name"]] = {"dep": t["dep"], "arr": t["arr"], "avail": avail}
        if not windows:
            break
        time.sleep(WAIT)
        html, url = do_research(s, tab, windows.pop(0))
        if "trainlist" not in url:
            break
    return results


# ------------------------------------------------------------
# 状態管理・通知
# ------------------------------------------------------------
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


def wd_ja(d):
    return "月火水木金土日"[d.weekday()]


def main():
    now = datetime.now(JST)
    if now.hour in QUIET_HOURS and os.environ.get("FORCE_RUN") != "1":
        print(f"[{now:%H:%M}] 深夜帯のためスキップ")
        return

    dates = build_target_dates()
    if not dates:
        print("監視対象日なし")
        return
    print("監視対象日:", ", ".join(f"{d:%m/%d}({wd_ja(d)})" for d in dates))

    state = load_state()
    new_openings = []   # 新たに空いたもの
    snapshot = []       # 現在の空き一覧（メール用）
    errors = []

    for d in dates:
        ymd = f"{d:%Y%m%d}"
        for dep_name, dep_code, arr_name, arr_code in ROUTES:
            label = f"{d:%m/%d}({wd_ja(d)}) {dep_name}→{arr_name}"
            try:
                res = check_route(ymd, dep_name, dep_code, arr_name, arr_code)
            except Exception as e:
                errors.append(f"{label}: {e}")
                print(f"  ! {label}: {e}")
                time.sleep(WAIT)
                continue
            for tr_name, info in sorted(res.items(), key=lambda x: x[1]["dep"]):
                for code, seat_name in TARGET_SEATS.items():
                    key = f"{ymd}|{dep_name}→{arr_name}|{tr_name}|{seat_name}"
                    available = code in info["avail"]
                    was = state.get(key, False)
                    line = (f"{d:%m/%d}({wd_ja(d)}) {tr_name} "
                            f"{info['dep']}発 {dep_name}→{arr_name}  {seat_name}")
                    if available:
                        snapshot.append("○ " + line)
                        if not was:
                            new_openings.append("★ " + line)
                            print("  ★ 空き発生:", line)
                    state[key] = available
            print(f"  済 {label}: スペーシアX {len(res)}本確認")
            time.sleep(WAIT)

    save_state(state)

    if new_openings:
        subject = f"【スペーシアX】特別座席に空きが出ました！（{len(new_openings)}件）"
        body = (
            "スペーシアXの特別座席に新しい空きを検知しました。\n"
            "早い者勝ちです。今すぐトブチケ！で確保を！\n"
            f"→ {BASE}/shop/\n\n"
            "■ 新たに空いた座席\n" + "\n".join(new_openings) + "\n\n"
            "■ いま空いている特別座席（全体）\n"
            + ("\n".join(snapshot) if snapshot else "（なし）") + "\n\n"
            f"チェック時刻: {now:%Y/%m/%d %H:%M} JST\n"
            "※空席は常に変動します。予約画面で最新状況をご確認ください。"
        )
        send_mail(subject, body)
        print(f"通知メール送信: {len(new_openings)}件")
    else:
        print("新しい空きなし" + (f"（現在空き{len(snapshot)}件は通知済み）" if snapshot else ""))

    if errors:
        print("エラー:", *errors, sep="\n  ")


if __name__ == "__main__":
    main()
