#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
법인무료설립센터(start-inc.org) admin 게시판 신규 신청 감시 → 텔레그램 알림

사용법
  python inquiry_alert.py --probe      # 로그인/목록 파싱이 되는지 점검 (알림 안 보냄)
  python inquiry_alert.py              # 1회 점검 후 종료 (작업 스케줄러용)
  python inquiry_alert.py --loop 300   # 300초마다 반복 실행 (창 띄워놓고 돌릴 때)

설정은 같은 폴더의 config.json 에서 읽는다. (config.example.json 참고)
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "inquiry_alert.log")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) inquiry-alert/1.0"


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    # 1) config.json 이 있으면 그걸 사용 (PC에서 돌릴 때)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    # 2) 없으면 환경변수에서 읽음 (GitHub Actions 등 클라우드에서 돌릴 때)
    env = os.environ
    if env.get("ADMIN_PW") and env.get("TG_TOKEN"):
        chat_ids = [c.strip() for c in env.get("TG_CHAT_IDS", "").split(",") if c.strip()]
        return {
            "login_url": env.get("LOGIN_URL", "https://start-inc.org/admin"),
            "list_url": env.get("LIST_URL", "https://start-inc.org/admin/board/board_list?code=inquiry&language=&search_type=&search=&files=&roundpage=10"),
            "admin_id": env.get("ADMIN_ID", "admin"),
            "admin_pw": env["ADMIN_PW"],
            "telegram_bot_token": env["TG_TOKEN"],
            "telegram_chat_ids": chat_ids,
            "login_id_field": env.get("LOGIN_ID_FIELD", ""),
            "login_pw_field": env.get("LOGIN_PW_FIELD", ""),
        }
    sys.exit("config.json 이 없고 환경변수(ADMIN_PW, TG_TOKEN, TG_CHAT_IDS)도 없습니다.")


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"last_id": None}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 로그인
def login(cfg, probe=False):
    s = requests.Session()
    s.headers["User-Agent"] = UA
    login_url = cfg["login_url"]
    r = s.get(login_url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    form = None
    for f in soup.find_all("form"):
        if f.find("input", {"type": "password"}):
            form = f
            break
    if form is None:
        raise RuntimeError("로그인 폼(password 입력칸)을 찾지 못했습니다. --probe 결과를 확인하세요.")

    action = form.get("action") or login_url
    action = urljoin(r.url, action)
    method = (form.get("method") or "post").lower()

    data = {}
    id_field = pw_field = None
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        typ = (inp.get("type") or "text").lower()
        if typ == "password":
            pw_field = name
        elif typ in ("text", "email") and id_field is None:
            id_field = name
        else:
            data[name] = inp.get("value", "")

    # 설정에서 필드명을 직접 지정했으면 그걸 우선
    id_field = cfg.get("login_id_field") or id_field
    pw_field = cfg.get("login_pw_field") or pw_field
    if not id_field or not pw_field:
        raise RuntimeError(f"로그인 필드 추정 실패 (id={id_field}, pw={pw_field}). config.json 에 login_id_field / login_pw_field 를 지정하세요.")

    data[id_field] = cfg["admin_id"]
    data[pw_field] = cfg["admin_pw"]

    if probe:
        log(f"[probe] 로그인 폼 action={action} method={method}")
        log(f"[probe] 필드: id={id_field}, pw={pw_field}, 기타={ {k: v for k, v in data.items() if k not in (id_field, pw_field)} }")

    if method == "get":
        r2 = s.get(action, params=data, timeout=20)
    else:
        r2 = s.post(action, data=data, timeout=20, headers={"Referer": r.url})
    r2.raise_for_status()

    # 로그인 성공 판정: 응답/목록페이지에 password 입력칸이 더 이상 없으면 성공
    check = s.get(cfg["list_url"], timeout=20)
    if BeautifulSoup(check.text, "html.parser").find("input", {"type": "password"}):
        if probe:
            log("[probe] 로그인 후에도 로그인 폼이 보입니다. 응답 앞부분:")
            log(r2.text[:800])
        raise RuntimeError("로그인 실패. 아이디/비밀번호 또는 로그인 방식(AJAX 등)을 확인하세요.")
    if probe:
        log("[probe] 로그인 성공")
    return s, check


# ---------------------------------------------------------------- 목록 파싱
def parse_list(html, base_url):
    """목록 테이블에서 (번호, 제목, 상세URL, 작성자, 등록일, 상태) 추출"""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        texts = [td.get_text(" ", strip=True) for td in tds]
        # 번호 칸: 순수 숫자인 첫 td
        num = None
        for t in texts[:3]:
            if re.fullmatch(r"\d+", t):
                num = int(t)
                break
        if num is None:
            continue
        a = tr.find("a", href=True)
        link = urljoin(base_url, a["href"]) if a else None
        title = a.get_text(" ", strip=True) if a else ""
        # 목록의 '번호'는 단순 순번이라 글을 지우면 다음 글이 같은 번호를 받는다.
        # 상세 링크의 no=XXXX 가 진짜 고유값이므로 그걸 id 로 쓴다.
        m = re.search(r"[?&]no=(\d+)", a["href"]) if a else None
        if not m:
            continue
        rows.append({
            "id": int(m.group(1)),   # 고유 번호 (no=)
            "seq": num,              # 화면에 보이는 순번
            "title": title,
            "url": link,
            "cols": texts,
        })
    return rows


# ---------------------------------------------------------------- 상세 파싱
def parse_detail(html):
    """th 라벨 → td 값 매핑 (제목, 작성자, 휴대폰, 이메일, 내용, 작성일 ...)"""
    soup = BeautifulSoup(html, "html.parser")
    info = {}
    for th in soup.find_all("th"):
        label = th.get_text(" ", strip=True)
        td = th.find_next_sibling("td")
        if not label or td is None:
            continue
        val = td.get_text("\n", strip=True)
        if label not in info:
            info[label] = val
    return info


# ---------------------------------------------------------------- 텔레그램
def send_telegram(cfg, text):
    token = cfg["telegram_bot_token"]
    chat_ids = cfg["telegram_chat_ids"]
    if isinstance(chat_ids, (str, int)):
        chat_ids = [chat_ids]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for cid in chat_ids:
        r = requests.post(url, data={"chat_id": cid, "text": text, "disable_web_page_preview": True}, timeout=20)
        if r.status_code != 200:
            log(f"텔레그램 전송 실패 chat_id={cid}: {r.text[:200]}")


def build_message(row, detail, cfg):
    name = detail.get("작성자") or (row["cols"][-3] if len(row["cols"]) >= 3 else "")
    phone = detail.get("휴대폰") or detail.get("연락처") or ""
    email = detail.get("이메일") or ""
    date = detail.get("작성일") or ""
    content = detail.get("내용") or ""
    content = content.strip()
    if len(content) > 600:
        content = content[:600] + " …"
    lines = [
        f"📩 신규 법인설립 신청 #{row.get('seq', row['id'])}",
        f"상호: {name}",
        f"연락처: {phone}",
    ]
    if email and email != "ex@ex.com":
        lines.append(f"이메일: {email}")
    if date:
        lines.append(f"접수: {date}")
    lines.append("")
    lines.append(content or "(내용 없음)")
    if row.get("url"):
        lines.append("")
        lines.append(row["url"])
    return "\n".join(lines)


# ---------------------------------------------------------------- 메인 점검
def check_once(cfg, state, probe=False):
    s, list_resp = login(cfg, probe=probe)
    rows = parse_list(list_resp.text, list_resp.url)
    if not rows:
        if probe:
            log("[probe] 목록 테이블에서 행을 하나도 못 읽었습니다. 목록 페이지 HTML 앞부분:")
            log(list_resp.text[:1500])
        raise RuntimeError("목록 파싱 실패 (행 0개). list_url 이 맞는지 확인하세요.")

    rows.sort(key=lambda r: r["id"])
    newest = rows[-1]["id"]

    if probe:
        log(f"[probe] 목록 {len(rows)}개 읽음. 최신 고유번호 no={newest}")
        for r in rows[-3:]:
            log(f"[probe]   #{r.get('seq')} (no={r['id']}) {r['cols']} -> {r['url']}")
        if rows[-1]["url"]:
            d = s.get(rows[-1]["url"], timeout=20)
            det = parse_detail(d.text)
            log(f"[probe] 최신글 상세 필드: {list(det.keys())}")
            log("[probe] 메시지 미리보기 ↓\n" + build_message(rows[-1], det, cfg))
        return

    last = state.get("last_id")
    if last is None or state.get("id_kind") != "no":
        # 첫 실행(또는 예전 순번 방식 state): 현재 최신 고유번호만 기억하고 알림은 안 보냄
        state["last_id"] = newest
        state["id_kind"] = "no"
        save_state(state)
        log(f"초기화: 최신 고유번호 no={newest} 기록. 이후 새 글부터 알림.")
        return

    # 하루 한 번 state.json 에 날짜 기록 (깃허브 저장소가 60일 비활성으로 꺼지는 것 방지)
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("heartbeat") != today:
        state["heartbeat"] = today
        save_state(state)

    new_rows = [r for r in rows if r["id"] > last]
    if not new_rows:
        log(f"새 글 없음 (최신 #{newest})")
        return

    for r in new_rows:
        detail = {}
        if r["url"]:
            try:
                d = s.get(r["url"], timeout=20)
                detail = parse_detail(d.text)
            except Exception as e:  # noqa
                log(f"#{r['id']} 상세 조회 실패: {e}")
        msg = build_message(r, detail, cfg)
        send_telegram(cfg, msg)
        log(f"알림 전송: #{r.get('seq')} (no={r['id']}) {detail.get('작성자', '')}")
        state["last_id"] = r["id"]
        save_state(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="로그인·파싱 점검만 (알림 안 보냄)")
    ap.add_argument("--loop", type=int, metavar="SEC", help="지정 초마다 반복")
    ap.add_argument("--test-telegram", action="store_true", help="텔레그램 테스트 메시지만 전송")
    args = ap.parse_args()

    cfg = load_config()

    if args.test_telegram:
        send_telegram(cfg, "✅ 법인무료설립센터 알림 봇 연결 테스트")
        log("텔레그램 테스트 메시지 전송")
        return

    state = load_state()
    if args.loop:
        log(f"감시 시작 ({args.loop}초 간격)")
        while True:
            try:
                check_once(cfg, state)
            except Exception as e:  # noqa
                log(f"오류: {e}")
            time.sleep(args.loop)
    else:
        try:
            check_once(cfg, state, probe=args.probe)
        except Exception as e:  # noqa
            log(f"오류: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
