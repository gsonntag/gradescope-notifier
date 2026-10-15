# Gradescope / Canvas deadline notifier

Sends a push notification to your phone (via [ntfy](https://ntfy.sh)) before
your Gradescope and Canvas deadlines — 24h and 12h before by default. Gradescope
assignments you've already submitted are skipped.

## Setup

1. Fork this repo (or use it as a template).
2. Install the ntfy app on your phone and subscribe to a hard-to-guess topic
   name, e.g. `deadlines-8f3k2x`.
3. In your fork, go to **Settings → Secrets and variables → Actions** and add:

   | Secret           | Value                                                    |
   | ---------------- | -------------------------------------------------------- |
   | `NTFY_TOPIC`     | your ntfy topic                                          |
   | `GS_EMAIL`       | Gradescope email (skip if you don't use Gradescope)      |
   | `GS_PASSWORD`    | Gradescope password                                      |
   | `CANVAS_ICS_URL` | Canvas calendar feed (optional, see below)               |

   Optional **variables** (same page, *Variables* tab):

   | Variable           | Default               | Example                       |
   | ------------------ | --------------------- | ----------------------------- |
   | `NOTIFY_HOURS`     | `24,12`               | `48,24,3`                     |
   | `TIMEZONE`         | `America/Los_Angeles` | `America/New_York`            |
   | `COURSE_NICKNAMES` | none                  | `LS 7A=7A, COM SCI-131=131`   |

   `COURSE_NICKNAMES` shortens the course name shown in the notification: if
   the text before `=` appears in the course name, the text after it is shown.

4. Open the **Actions** tab, enable workflows, and run *Deadline notifier*
   once manually to check it works. After that it runs every 30 minutes.

### Canvas calendar feed

In Canvas, open **Calendar** and click **Calendar Feed** (bottom right) to
copy your private `.ics` URL. The feed doesn't say whether you've submitted, so
Canvas assignments are only skipped when the same assignment is on Gradescope.

## Running locally

```sh
pip install -r requirements.txt
touch .env               # then put the variables above in .env as KEY=value lines
python notify.py --test   # lists upcoming assignments and sends a test push
```

Which reminders have been sent is kept in `sent.json`. On GitHub Actions it is
stored in the Actions cache between runs rather than committed to the repo.
