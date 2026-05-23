"""Jungfrau Lodge room availability monitor.

Usage:
    # Show current state for a date range
    python monitor.py --arrival 2026-07-06 --departure 2026-07-07

    # Compare against state file, push notification on new availability
    python monitor.py --arrival 2026-07-06 --departure 2026-07-07 \\
        --state-file state.json --notify
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

import requests
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


def format_notification(arrival: str, departure: str, diff: dict) -> tuple[str, str]:
    new_rooms = diff["newly_available"]
    title = f"🏨 Jungfrau Lodge 有新房! {arrival} → {departure}"
    lines = [
        f"### 日期: {arrival} → {departure}",
        "",
        "## ✨ 新可用房型",
        "",
    ]
    for r in new_rooms:
        price = f" — **{r.price_chf:.2f} CHF/night**" if r.price_chf else ""
        lines.append(f"- {r.name}{price}")

    if diff["price_drops"]:
        lines += ["", "## 💰 降价房型", ""]
        for r, old, new in diff["price_drops"]:
            lines.append(f"- {r.name}: {old:.2f} → **{new:.2f} CHF/night**")

    lines += [
        "",
        f"[👉 立即预订](https://www.jungfraulodge.ch/en/booking/booking-step:room_selection1/)",
        "",
        f"_检查时间: {datetime.now().isoformat(timespec='seconds')}_",
    ]
    return title, "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Jungfrau Lodge availability monitor")
    p.add_argument("--arrival", required=True, help="YYYY-MM-DD")
    p.add_argument("--departure", required=True, help="YYYY-MM-DD")
    p.add_argument("--adults", type=int, default=2)
    p.add_argument("--rooms", type=int, default=1)
    p.add_argument("--state-file", help="JSON file to read/write availability state")
    p.add_argument(
        "--notify",
        action="store_true",
        help="Push Server酱 notification when new rooms become available",
    )
    p.add_argument("--json", action="store_true", help="Output JSON instead of table")
    p.add_argument("--save-html", help="Save raw HTML to this path (for debugging)")
    args = p.parse_args()

    html = fetch_html(args.arrival, args.departure, args.adults, args.rooms)
    if args.save_html:
        Path(args.save_html).write_text(html, encoding="utf-8")
    rooms = parse_rooms(html)
    if not rooms:
        raise RuntimeError(
            "Parsed 0 rooms from response. The site structure may have changed "
            f"(arrival={args.arrival} departure={args.departure}). "
            f"Response length: {len(html)} bytes."
        )

    snapshot = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "arrival": args.arrival,
        "departure": args.departure,
        "adults": args.adults,
        "rooms_requested": args.rooms,
        "rooms": [asdict(r) for r in rooms],
    }

    # Diff + notify
    diff = None
    if args.state_file:
        state_path = Path(args.state_file)
        key = f"{args.arrival}_{args.departure}_a{args.adults}r{args.rooms}"
        prev = load_state(state_path, key)
        diff = diff_availability(prev, rooms)
        save_state(state_path, key, snapshot)

        if args.notify and (diff["newly_available"] or diff["price_drops"]):
            send_key = os.environ.get("SERVERJIANG_SENDKEY")
            if not send_key:
                print("[warn] SERVERJIANG_SENDKEY env var not set; skip push", file=sys.stderr)
            else:
                title, desp = format_notification(args.arrival, args.departure, diff)
                if push_serverjiang(send_key, title, desp):
                    print(f"[info] Pushed: {title}")

    # Output
    if args.json:
        out = dict(snapshot)
        if diff is not None:
            out["diff"] = {
                "newly_available": [asdict(r) for r in diff["newly_available"]],
                "now_unavailable": diff["now_unavailable"],
                "price_drops": [
                    {"room": asdict(r), "old": old, "new": new}
                    for r, old, new in diff["price_drops"]
                ],
            }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    available = [r for r in rooms if r.available]
    print(f"Checked {args.arrival} -> {args.departure} for {args.adults} adult(s)")
    print(f"Total rooms listed: {len(rooms)} | Available: {len(available)}")
    print("-" * 70)
    for r in rooms:
        flag = "[OK] " if r.available else "[--] "
        price = f"  {r.price_chf:.2f} CHF/night" if r.price_chf else ""
        print(f"{flag}{r.name:<40s}{price}")
    if diff:
        if diff["newly_available"]:
            print(f"\n>>> NEW available: {[r.name for r in diff['newly_available']]}")
        if diff["price_drops"]:
            print(f">>> Price drops: {[(r.name, old, new) for r, old, new in diff['price_drops']]}")
        if diff["now_unavailable"]:
            print(f">>> Gone: {diff['now_unavailable']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
