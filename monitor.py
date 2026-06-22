"""Jungfrau Lodge room availability monitor.

Always queries each night within the [arrival, departure) range separately.
This shows per-night availability rather than only fully-bookable multi-night
stays (which are often empty).

Usage:
    # Check each night between 2026-07-01 and 2026-07-07 (6 nights)
    python monitor.py --arrival 2026-07-01 --departure 2026-07-07

    # With state file + notify on changes
    python monitor.py --arrival 2026-07-01 --departure 2026-07-07 \\
        --state-file state.json --notify

    # Push current status summary regardless of changes (testing)
    python monitor.py --arrival 2026-07-01 --departure 2026-07-07 \\
        --state-file state.json --always-notify
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

BOOKING_URL = "https://www.jungfraulodge.ch/en/booking/"
ROOM_SELECTION_URL = "https://www.jungfraulodge.ch/en/booking/booking-step:room_selection1/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


@dataclass
class Room:
    room_id: str
    name: str
    occupation: str
    available: bool
    price_chf: float | None
    closed_dates: list[str]


@dataclass
class Subscriber:
    id: str
    email: str
    arrival: str
    departure: str
    adults: int = 2
    rooms: int = 1
    rooms_filter: list[str] = field(default_factory=list)  # match by name keyword, case-insensitive
    unsubscribe_token: str = ""
    created_at: str = ""


def load_subscribers(path: Path) -> list[Subscriber]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[Subscriber] = []
    for raw in data.get("subscribers", []):
        # Tolerate extra fields and missing optional ones.
        out.append(Subscriber(
            id=str(raw["id"]),
            email=str(raw["email"]),
            arrival=str(raw["arrival"]),
            departure=str(raw["departure"]),
            adults=int(raw.get("adults", 2)),
            rooms=int(raw.get("rooms", 1)),
            rooms_filter=list(raw.get("rooms_filter") or []),
            unsubscribe_token=str(raw.get("unsubscribe_token", "")),
            created_at=str(raw.get("created_at", "")),
        ))
    return out


def matches_room_filter(room: Room, rooms_filter: list[str]) -> bool:
    if not rooms_filter:
        return True
    name = room.name.lower()
    return any(kw.lower() in name for kw in rooms_filter)


def filter_diff_by_rooms(diff: dict, rooms_filter: list[str]) -> dict:
    if not rooms_filter:
        return diff
    return {
        "newly_available": [r for r in diff["newly_available"] if matches_room_filter(r, rooms_filter)],
        "now_unavailable": list(diff["now_unavailable"]),  # name-only; can't filter precisely without room obj
        "price_drops": [
            (r, old, new) for r, old, new in diff["price_drops"]
            if matches_room_filter(r, rooms_filter)
        ],
    }


def fetch_html(arrival: str, departure: str, adults: int, rooms: int) -> str:
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    session.get(BOOKING_URL, timeout=20).raise_for_status()
    form = {
        "begin_date": arrival,
        "end_date": departure,
        "rooms_count": str(rooms),
        "room_adult_count[1]": str(adults),
        "room_children_count[1]": "0",
        "specoffer": "",
        "room_name_filter": "",
        "room_category_filter": "",
        "rate_filter": "",
        "coupon": "",
        "show_not_available": "on",
        "process_action": "search",
    }
    resp = session.post(BOOKING_URL, data=form, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    return resp.text


_PRICE_RE = re.compile(r"From\s+([\d.,]+)\s*CHF", re.IGNORECASE)
_CLOSED_RE = re.compile(r"(\d{4}-\d{2}-\d{2}):\s*Closed", re.IGNORECASE)


def _first_text(el) -> str:
    if el is None:
        return ""
    for child in el.children:
        if isinstance(child, str):
            text = child.strip()
            if text:
                return text
    return el.get_text(strip=True)


def _parse_price(text: str) -> float | None:
    m = _PRICE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_rooms(html: str) -> list[Room]:
    soup = BeautifulSoup(html, "html.parser")
    rooms: list[Room] = []

    for tile in soup.select("div.room_tile"):
        classes = tile.get("class", [])
        available = "not_available" not in classes

        name = _first_text(tile.select_one(".room_name"))
        occupation = _first_text(tile.select_one(".room_occupation"))

        comment_div = tile.select_one("div[id^='room_comment_']")
        room_id = comment_div["id"].replace("room_comment_", "") if comment_div else "?"
        closed_dates = (
            _CLOSED_RE.findall(comment_div.decode_contents()) if comment_div else []
        )

        price = None
        footer = tile.select_one(".room_footer")
        if footer:
            price = _parse_price(footer.get_text(" ", strip=True))

        rooms.append(
            Room(
                room_id=room_id,
                name=name,
                occupation=occupation,
                available=available,
                price_chf=price,
                closed_dates=closed_dates,
            )
        )
    return rooms


def load_state(path: Path, key: str) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data.get(key, {})


def save_state(path: Path, key: str, snapshot: dict) -> None:
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data[key] = snapshot
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def diff_availability(prev: dict, current_rooms: list[Room]) -> dict:
    """Return what changed since previous snapshot.

    Returns dict with:
      - newly_available: list[Room]  -> rooms now available that weren't last time
      - now_unavailable: list[str]   -> room names that disappeared
      - price_drops:     list[tuple] -> (room, old_price, new_price)
    """
    prev_rooms = {r["room_id"]: r for r in prev.get("rooms", [])}
    newly_available: list[Room] = []
    now_unavailable: list[str] = []
    price_drops: list[tuple[Room, float, float]] = []

    current_by_id = {r.room_id: r for r in current_rooms}

    for room in current_rooms:
        prev_room = prev_rooms.get(room.room_id)
        was_available = prev_room["available"] if prev_room else False
        if room.available and not was_available:
            newly_available.append(room)
        elif room.available and prev_room and prev_room.get("price_chf"):
            if room.price_chf and room.price_chf < prev_room["price_chf"]:
                price_drops.append((room, prev_room["price_chf"], room.price_chf))

    for rid, prev_room in prev_rooms.items():
        if prev_room.get("available") and rid not in current_by_id:
            now_unavailable.append(prev_room["name"])
        elif prev_room.get("available") and not current_by_id[rid].available:
            now_unavailable.append(prev_room["name"])

    return {
        "newly_available": newly_available,
        "now_unavailable": now_unavailable,
        "price_drops": price_drops,
    }


def push_serverjiang(send_key: str, title: str, desp: str) -> bool:
    url = f"https://sctapi.ftqq.com/{send_key}.send"
    try:
        r = requests.post(url, data={"title": title, "desp": desp}, timeout=15)
        r.raise_for_status()
        result = r.json()
        ok = result.get("code") == 0
        if not ok:
            print(f"[warn] Server酱 returned: {result}", file=sys.stderr)
        return ok
    except Exception as e:
        print(f"[warn] Server酱 push failed: {e}", file=sys.stderr)
        return False


def _md_to_html(md: str) -> str:
    """Minimal markdown -> HTML, just for email rendering."""
    lines = md.split("\n")
    out = []
    for line in lines:
        if line.startswith("### "):
            out.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("#### "):
            out.append(f"<h4>{line[5:]}</h4>")
        elif line.startswith("- "):
            out.append(f"<li>{line[2:]}</li>")
        elif line.strip() == "":
            out.append("<br>")
        else:
            out.append(f"<p>{line}</p>")
    html = "\n".join(out)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"_(.+?)_", r"<em>\1</em>", html)
    html = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', html)
    return f"<html><body>{html}</body></html>"


def push_email(
    smtp_user: str,
    smtp_password: str,
    recipients: list[str],
    subject: str,
    body_md: str,
    smtp_host: str = "smtp.gmail.com",
    smtp_port: int = 465,
) -> bool:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body_md, "plain", "utf-8"))
    msg.attach(MIMEText(_md_to_html(body_md), "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20) as s:
            s.login(smtp_user, smtp_password)
            s.sendmail(smtp_user, recipients, msg.as_string())
        return True
    except Exception as e:
        print(f"[warn] Email push failed: {e}", file=sys.stderr)
        return False


def push_all(title: str, desp: str) -> list[str]:
    """Push to all configured channels. Returns list of channels that succeeded."""
    succeeded: list[str] = []

    send_key = os.environ.get("SERVERJIANG_SENDKEY")
    if send_key:
        if push_serverjiang(send_key, title, desp):
            succeeded.append("serverjiang")

    gmail_user = os.environ.get("GMAIL_USER")
    gmail_pw = os.environ.get("GMAIL_APP_PASSWORD")
    gmail_to_raw = os.environ.get("GMAIL_TO") or gmail_user
    if gmail_user and gmail_pw and gmail_to_raw:
        recipients = [x.strip() for x in gmail_to_raw.split(",") if x.strip()]
        if push_email(gmail_user, gmail_pw, recipients, title, desp):
            succeeded.append("email")

    if not send_key and not (gmail_user and gmail_pw):
        print(
            "[warn] No notification channel configured "
            "(set SERVERJIANG_SENDKEY and/or GMAIL_USER+GMAIL_APP_PASSWORD)",
            file=sys.stderr,
        )
    return succeeded


def iter_nights(arrival: str, departure: str) -> list[tuple[str, str]]:
    """Split a [arrival, departure) range into consecutive 1-night queries.

    iter_nights("2026-07-01", "2026-07-04") ->
        [("2026-07-01","2026-07-02"), ("2026-07-02","2026-07-03"), ("2026-07-03","2026-07-04")]
    """
    start = date.fromisoformat(arrival)
    end = date.fromisoformat(departure)
    if end <= start:
        raise ValueError(f"departure must be after arrival, got {arrival} -> {departure}")
    out: list[tuple[str, str]] = []
    cur = start
    while cur < end:
        nxt = cur + timedelta(days=1)
        out.append((cur.isoformat(), nxt.isoformat()))
        cur = nxt
    return out


@dataclass
class NightResult:
    arrival: str
    departure: str
    rooms: list[Room]
    diff: dict | None  # newly_available / now_unavailable / price_drops


def has_changes(night_results: list[NightResult]) -> bool:
    for nr in night_results:
        if nr.diff and (nr.diff["newly_available"] or nr.diff["price_drops"]):
            return True
    return False


def format_notification_nightly(
    arrival: str, departure: str, night_results: list[NightResult]
) -> tuple[str, str]:
    title = f"🏨 Jungfrau Lodge 有新房! {arrival} → {departure}"
    lines = [f"### 日期范围: {arrival} → {departure} (逐夜查询)", ""]

    new_lines: list[str] = []
    drop_lines: list[str] = []
    for nr in night_results:
        if not nr.diff:
            continue
        for r in nr.diff["newly_available"]:
            price = f" — **{r.price_chf:.2f} CHF**" if r.price_chf else ""
            new_lines.append(f"- {nr.arrival} → {nr.departure}: {r.name}{price}")
        for r, old, new in nr.diff["price_drops"]:
            drop_lines.append(
                f"- {nr.arrival} → {nr.departure}: {r.name}: {old:.2f} → **{new:.2f} CHF**"
            )

    if new_lines:
        lines += ["## ✨ 新可用房型(按夜)", ""] + new_lines + [""]
    if drop_lines:
        lines += ["## 💰 降价房型(按夜)", ""] + drop_lines + [""]

    lines += [
        f"[👉 立即预订](https://www.jungfraulodge.ch/en/booking/booking-step:room_selection1/)",
        "",
        f"_检查时间: {datetime.now().isoformat(timespec='seconds')}_",
    ]
    return title, "\n".join(lines)


def format_status_summary_nightly(
    arrival: str, departure: str, night_results: list[NightResult]
) -> tuple[str, str]:
    nights_with = sum(1 for nr in night_results if any(r.available for r in nr.rooms))
    title = (
        f"📋 Jungfrau Lodge 当前状态 {arrival} → {departure}: "
        f"{nights_with}/{len(night_results)} 夜有房"
    )
    lines = [
        f"### 日期范围: {arrival} → {departure} (逐夜查询)",
        "",
        f"**共查询 {len(night_results)} 夜,{nights_with} 夜有可用房型**",
        "",
    ]
    for nr in night_results:
        available = [r for r in nr.rooms if r.available]
        if available:
            lines.append(f"#### {nr.arrival} → {nr.departure} ({len(available)} 间可用)")
            for r in available:
                price = f" — **{r.price_chf:.2f} CHF**" if r.price_chf else ""
                lines.append(f"- {r.name}{price}")
            lines.append("")
        else:
            lines.append(f"#### {nr.arrival} → {nr.departure} — _无可用_")
            lines.append("")

    lines += [
        f"[👉 立即预订](https://www.jungfraulodge.ch/en/booking/booking-step:room_selection1/)",
        "",
        f"_检查时间: {datetime.now().isoformat(timespec='seconds')}_",
    ]
    return title, "\n".join(lines)


class HttpCache:
    """Cache parsed Room lists by (arrival, departure, adults, rooms) within one run.

    Multiple subscribers often request the same (date, adults, rooms) combination;
    we only need to hit the website once per combo.
    """

    def __init__(self, sleep_between: float = 1.5) -> None:
        self._cache: dict[tuple[str, str, int, int], list[Room]] = {}
        self._sleep_between = sleep_between
        self._fetch_count = 0

    def get(self, arrival: str, departure: str, adults: int, rooms: int) -> list[Room]:
        key = (arrival, departure, adults, rooms)
        if key in self._cache:
            return self._cache[key]
        if self._fetch_count > 0 and self._sleep_between > 0:
            time.sleep(self._sleep_between)
        self._fetch_count += 1
        html = fetch_html(arrival, departure, adults, rooms)
        parsed = parse_rooms(html)
        if not parsed:
            raise RuntimeError(
                "Parsed 0 rooms from response. The site structure may have changed "
                f"(arrival={arrival} departure={departure}). "
                f"Response length: {len(html)} bytes."
            )
        self._cache[key] = parsed
        return parsed


def run_nights_for(
    arrival: str,
    departure: str,
    adults: int,
    rooms_count: int,
    state_path: Path | None,
    state_key_prefix: str,
    cache: HttpCache,
    rooms_filter: list[str] | None = None,
) -> list[NightResult]:
    """Iterate every night in [arrival, departure), fetch via cache, diff against state.

    state_key_prefix lets us scope state per subscriber (or use "" for global single mode).
    """
    nights = iter_nights(arrival, departure)
    results: list[NightResult] = []
    for a, d in nights:
        rooms = cache.get(a, d, adults, rooms_count)

        snapshot = {
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "arrival": a,
            "departure": d,
            "adults": adults,
            "rooms_requested": rooms_count,
            "rooms": [asdict(r) for r in rooms],
        }

        diff = None
        if state_path:
            key = f"{state_key_prefix}{a}_{d}_a{adults}r{rooms_count}"
            prev = load_state(state_path, key)
            diff = diff_availability(prev, rooms)
            save_state(state_path, key, snapshot)
            if rooms_filter:
                diff = filter_diff_by_rooms(diff, rooms_filter)

        # Filter rooms for display/format too
        visible_rooms = (
            [r for r in rooms if matches_room_filter(r, rooms_filter or [])]
            if rooms_filter
            else rooms
        )
        results.append(NightResult(arrival=a, departure=d, rooms=visible_rooms, diff=diff))
    return results


def run_subscriptions_mode(args, subscribers: list[Subscriber], state_path: Path | None) -> int:
    if not subscribers:
        print("[info] No subscribers in subscriptions file.")
        return 0

    gmail_user = os.environ.get("GMAIL_USER")
    gmail_pw = os.environ.get("GMAIL_APP_PASSWORD")
    cache = HttpCache(sleep_between=args.sleep_between)

    print(f"Running for {len(subscribers)} subscriber(s)")
    for sub in subscribers:
        print(f"\n=== {sub.id} <{sub.email}> {sub.arrival} -> {sub.departure} ===")
        try:
            night_results = run_nights_for(
                arrival=sub.arrival,
                departure=sub.departure,
                adults=sub.adults,
                rooms_count=sub.rooms,
                state_path=state_path,
                state_key_prefix=f"sub:{sub.id}:",
                cache=cache,
                rooms_filter=sub.rooms_filter,
            )
        except Exception as e:
            print(f"  [error] {e}", file=sys.stderr)
            # One subscriber failing shouldn't break others.
            continue

        for nr in night_results:
            avail = sum(1 for r in nr.rooms if r.available)
            print(f"  {nr.arrival} -> {nr.departure}: {avail} matching available")

        if not (gmail_user and gmail_pw):
            print("  [warn] GMAIL_USER/GMAIL_APP_PASSWORD not set; skip email")
            continue

        if args.always_notify:
            title, desp = format_status_summary_nightly(sub.arrival, sub.departure, night_results)
        elif args.notify and has_changes(night_results):
            title, desp = format_notification_nightly(sub.arrival, sub.departure, night_results)
        else:
            continue

        if sub.unsubscribe_token:
            desp += f"\n\n_退订: 在邮件回复 'unsubscribe {sub.unsubscribe_token}' (待实现)_"

        if push_email(gmail_user, gmail_pw, [sub.email], title, desp):
            print(f"  [info] Pushed to {sub.email}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Jungfrau Lodge availability monitor (queries each night separately)"
    )
    p.add_argument("--arrival", help="Range start, YYYY-MM-DD (single mode)")
    p.add_argument("--departure", help="Range end (exclusive), YYYY-MM-DD (single mode)")
    p.add_argument("--adults", type=int, default=2)
    p.add_argument("--rooms", type=int, default=1)
    p.add_argument(
        "--subscriptions-file",
        help="YAML file listing subscribers; if set, --arrival/--departure are ignored",
    )
    p.add_argument("--state-file", help="JSON file to read/write availability state")
    p.add_argument(
        "--notify",
        action="store_true",
        help="Push notification when new rooms become available on any night",
    )
    p.add_argument(
        "--always-notify",
        action="store_true",
        help="Push current-status summary regardless of diff (for manual testing)",
    )
    p.add_argument(
        "--sleep-between",
        type=float,
        default=1.5,
        help="Seconds to sleep between distinct HTTP fetches (be polite)",
    )
    p.add_argument("--json", action="store_true", help="Output JSON instead of table")
    args = p.parse_args()

    state_path = Path(args.state_file) if args.state_file else None

    # Subscriptions mode: ignore --arrival/--departure, iterate per subscriber.
    if args.subscriptions_file:
        subscribers = load_subscribers(Path(args.subscriptions_file))
        return run_subscriptions_mode(args, subscribers, state_path)

    # Single mode (legacy / admin testing): requires --arrival and --departure.
    if not args.arrival or not args.departure:
        p.error("--arrival and --departure are required when --subscriptions-file is not given")

    cache = HttpCache(sleep_between=args.sleep_between)
    print(f"Checking {len(iter_nights(args.arrival, args.departure))} night(s): "
          f"{args.arrival} -> {args.departure}")
    night_results = run_nights_for(
        arrival=args.arrival,
        departure=args.departure,
        adults=args.adults,
        rooms_count=args.rooms,
        state_path=state_path,
        state_key_prefix="",
        cache=cache,
    )

    # Notification (single mode pushes to all admin channels)
    if args.always_notify:
        title, desp = format_status_summary_nightly(
            args.arrival, args.departure, night_results
        )
        channels = push_all(title, desp)
        if channels:
            print(f"[info] Pushed status summary via {channels}: {title}")
    elif args.notify and has_changes(night_results):
        title, desp = format_notification_nightly(
            args.arrival, args.departure, night_results
        )
        channels = push_all(title, desp)
        if channels:
            print(f"[info] Pushed via {channels}: {title}")

    # Output
    if args.json:
        out = {
            "arrival": args.arrival,
            "departure": args.departure,
            "adults": args.adults,
            "rooms_requested": args.rooms,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "nights": [
                {
                    "arrival": nr.arrival,
                    "departure": nr.departure,
                    "rooms": [asdict(r) for r in nr.rooms],
                    "diff": (
                        {
                            "newly_available": [
                                asdict(r) for r in nr.diff["newly_available"]
                            ],
                            "now_unavailable": nr.diff["now_unavailable"],
                            "price_drops": [
                                {"room": asdict(r), "old": old, "new": new}
                                for r, old, new in nr.diff["price_drops"]
                            ],
                        }
                        if nr.diff
                        else None
                    ),
                }
                for nr in night_results
            ],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # Table output
    print("-" * 70)
    for nr in night_results:
        avail = [r for r in nr.rooms if r.available]
        flag = "[OK]" if avail else "[--]"
        names = ", ".join(
            f"{r.name}({r.price_chf:.0f})" if r.price_chf else r.name for r in avail
        )
        print(f"{flag} {nr.arrival} -> {nr.departure}: {names or '(none)'}")
        if nr.diff and nr.diff["newly_available"]:
            print(f"     >>> NEW: {[r.name for r in nr.diff['newly_available']]}")
        if nr.diff and nr.diff["price_drops"]:
            print(f"     >>> DROP: {[(r.name, old, new) for r, old, new in nr.diff['price_drops']]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
