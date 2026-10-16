"""Send ntfy push notifications before Gradescope and Canvas deadlines.

Gradescope: logs in and skips assignments you've already submitted.
Canvas (e.g. UCLA's BruinLearn): reads your private calendar feed (.ics). The
feed has no submission info, so items are skipped only if they're also on
Gradescope.

Env vars (see README.md):
    NTFY_TOPIC        required
    GS_EMAIL, GS_PASSWORD   Gradescope login (optional if you only use Canvas)
    CANVAS_ICS_URL    Canvas calendar feed URL (optional)
    NOTIFY_HOURS      hours before the deadline to notify, default "168,72,48,24,12,6,3"
    TIMEZONE          IANA timezone for all-day Canvas deadlines,
                      default "America/Los_Angeles"
    COURSE_NICKNAMES  optional short names, e.g. "LS 7A=7A, COM SCI-131=131"
Usage:
    python notify.py          # normal run (meant for a cron / GitHub Actions)
    python notify.py --test   # list upcoming assignments and send a test push
"""

import json
import os
import re
import sys
import traceback
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from gradescopeapi.classes.connection import GSConnection

# GitHub Actions commits this back to the repo after each run that changes it.
STATE_FILE = Path(__file__).parent / "sent.json"
BASE_URL = "https://www.gradescope.com"


def parse_hours(value):
    return sorted({float(h) for h in value.split(",") if h.strip()}, reverse=True)


def parse_nicknames(value):
    """Parse "KEY=nick, KEY2=nick2" into a dict."""
    nicknames = {}
    for pair in value.split(","):
        if "=" in pair:
            key, nick = pair.split("=", 1)
            nicknames[key.strip()] = nick.strip()
    return nicknames


def short_course_name(name, nicknames):
    """If a nickname key appears in the course name, show the nickname instead."""
    for key, nickname in nicknames.items():
        if key in name:
            return nickname
    return name


def load_env_file():
    """Load a local .env file if present (for running on your laptop)."""
    env = Path(__file__).parent / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def push(topic, title, message, click_url=None, priority="high"):
    headers = {"Title": title, "Priority": priority}
    if click_url:
        headers["Click"] = click_url
    resp = requests.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()


def normalize(name):
    return " ".join(name.lower().split())


def fetch_gradescope(email, password):
    """Return (unsubmitted upcoming assignments, normalized names of all upcoming ones)."""
    conn = GSConnection()
    conn.login(email, password)
    now = datetime.now(timezone.utc)

    courses = conn.account.get_courses()["student"]
    upcoming = []
    all_names = set()
    for course_id, course in courses.items():
        # Skip old courses so we don't scrape every class you've ever taken.
        try:
            if int(course.year) < now.year - 1:
                continue
        except (TypeError, ValueError):
            pass

        for a in conn.account.get_assignments(course_id):
            if a.due_date is None or a.due_date <= now:
                continue
            all_names.add(normalize(a.name))
            if a.grade is not None or (a.submissions_status or "").strip() == "Submitted":
                continue
            upcoming.append(
                {
                    "key": f"{course_id}:{a.assignment_id or a.name}",
                    "source": "gradescope",
                    "course": course.name,
                    "name": a.name.strip(),
                    "due": a.due_date,
                    "url": f"{BASE_URL}/courses/{course_id}"
                    + (f"/assignments/{a.assignment_id}" if a.assignment_id else ""),
                }
            )
    return upcoming, all_names


def parse_ics_events(text):
    """Minimal .ics parser: returns a list of {property: value} dicts, one per VEVENT."""
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold continuation lines
    events, current = [], None
    for line in text.splitlines():
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            events.append(current)
            current = None
        elif current is not None and ":" in line:
            prop, value = line.split(":", 1)
            current[prop.split(";")[0]] = value
    return events


def ics_unescape(value):
    return re.sub(r"\\([,;\\])", r"\1", value).replace("\\n", " ")


def fetch_canvas(ics_url, skip_names, local_tz):
    """Return upcoming Canvas assignments from the calendar feed."""
    resp = requests.get(ics_url, timeout=30)
    resp.raise_for_status()
    now = datetime.now(timezone.utc)

    upcoming = []
    for ev in parse_ics_events(resp.text):
        uid = ev.get("UID", "")
        if not uid.startswith("event-assignment-"):
            continue  # skip regular calendar events

        start = ev.get("DTSTART", "")
        if "T" in start:
            due = datetime.strptime(start, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        else:
            # Canvas exports assignments due at 11:59 PM as all-day events.
            day = datetime.strptime(start, "%Y%m%d").date()
            due = datetime.combine(day, time(23, 59), local_tz)
        if due <= now:
            continue

        # SUMMARY looks like "Assignment name [COURSE-A / COURSE-B]"
        summary = ics_unescape(ev.get("SUMMARY", ""))
        m = re.match(r"(.*?)\s*\[([^\]]*)\]\s*$", summary)
        name, course = (m.group(1), m.group(2).split(" / ")[0]) if m else (summary, "Canvas")
        if normalize(name) in skip_names:
            continue  # already tracked via Gradescope (which knows if it's submitted)

        url = ev.get("URL", "")
        link = re.match(r"(https://[^/]+)/calendar\?include_contexts=course_(\d+).*#assignment_(\d+)", url)
        if link:
            url = f"{link.group(1)}/courses/{link.group(2)}/assignments/{link.group(3)}"

        upcoming.append(
            {"key": f"canvas:{uid}", "source": "canvas", "course": course, "name": name,
             "due": due, "url": url}
        )
    return upcoming


def time_left(hours):
    return f"{hours / 24:.0f}d" if hours >= 47.5 else f"{hours:.0f}h"


def load_state():
    """Return (sources seen before, {assignment key: {due, sent, name, source}})."""
    data = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if "assignments" not in data:  # old format: just the assignments dict
        data = {"assignments": data}
    return set(data.get("seeded", [])), data["assignments"]


def main():
    load_env_file()
    topic = os.environ["NTFY_TOPIC"]
    email = os.environ.get("GS_EMAIL")
    password = os.environ.get("GS_PASSWORD")
    canvas_url = os.environ.get("CANVAS_ICS_URL")
    thresholds = parse_hours(os.environ.get("NOTIFY_HOURS") or "168,72,48,24,12,6,3")
    local_tz = ZoneInfo(os.environ.get("TIMEZONE") or "America/Los_Angeles")
    nicknames = parse_nicknames(os.environ.get("COURSE_NICKNAMES") or "")
    test_mode = "--test" in sys.argv

    if not (email and password) and not canvas_url:
        sys.exit("Set GS_EMAIL + GS_PASSWORD and/or CANVAS_ICS_URL.")

    seeded, state = load_state()

    # Fetch each source independently so one breaking doesn't silence the other.
    upcoming, gs_names, fetched, failed = [], set(), set(), False
    if email and password:
        try:
            gs_upcoming, gs_names = fetch_gradescope(email, password)
            upcoming += gs_upcoming
            fetched.add("gradescope")
        except Exception:
            traceback.print_exc()
            failed = True
    if canvas_url:
        # Also skip names from earlier Gradescope runs, in case Gradescope is down now.
        gs_names |= {
            normalize(v["name"]) for v in state.values()
            if v.get("source") == "gradescope" and "name" in v
        }
        try:
            upcoming += fetch_canvas(canvas_url, gs_names, local_tz)
            fetched.add("canvas")
        except Exception:
            traceback.print_exc()
            failed = True
    now = datetime.now(timezone.utc)

    if test_mode:
        print(f"Found {len(upcoming)} unsubmitted upcoming assignment(s):")
        for a in sorted(upcoming, key=lambda x: x["due"]):
            hours = (a["due"] - now).total_seconds() / 3600
            print(f"  [{a['course']}] {a['name']} - due in {hours:.1f}h")
        push(topic, "Deadline notifier works", f"Tracking {len(upcoming)} upcoming assignment(s).")
        print("Test notification sent.")
        return

    for a in upcoming:
        hours_left = (a["due"] - now).total_seconds() / 3600
        due_iso = a["due"].isoformat()
        # The first successful fetch of a source just records what's there, so
        # setting this up doesn't announce every existing assignment as new.
        if a["key"] not in state and a["source"] in seeded:
            due_local = a["due"].astimezone(local_tz).strftime("%a %b %-d, %-I:%M %p")
            push(
                topic,
                title=f"New: {a['name']}",
                message=f"{short_course_name(a['course'], nicknames)} · due {due_local}",
                click_url=a["url"],
                priority="default",
            )
            print(f"New assignment: {a['course']} / {a['name']}")
        sent = state.get(a["key"], {})
        # If the due date changed (e.g. extension), reset what we've sent.
        if sent.get("due") != due_iso:
            sent = {"due": due_iso, "sent": []}

        crossed = [t for t in thresholds if hours_left <= t]
        if crossed and any(t not in sent["sent"] for t in crossed):
            push(
                topic,
                title=f"Due in {time_left(hours_left)}: {a['name']}",
                message=short_course_name(a["course"], nicknames),
                click_url=a["url"],
                # The last reminder before the deadline is the urgent one.
                priority="urgent" if min(crossed) == thresholds[-1] else "high",
            )
            print(f"Notified: {a['course']} / {a['name']} ({hours_left:.1f}h left)")
            sent["sent"] = sorted(set(sent["sent"]) | set(crossed))
        sent.update(name=a["name"], source=a["source"])
        state[a["key"]] = sent

    # Drop entries whose deadline has passed.
    state = {
        k: v for k, v in state.items()
        if datetime.fromisoformat(v["due"]) > now
    }
    data = {"seeded": sorted(seeded | fetched), "assignments": state}
    STATE_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    if failed:
        sys.exit(1)  # makes the GitHub Actions run show red so you get an email


if __name__ == "__main__":
    main()
