"""Ticket watch: post HubSpot ticket changes to the sweep channel as they happen.

Why (Chris, 21 Oct 2026): the Ops board moves (new ticket, stage, owner, a note
from Krishna) were only visible by opening HubSpot. This puts each one in the
internal sweep channel (Sarah + Chris; Sammy does not use Slack) within a minute.

How: a long-running loop next to the hourly classifier. Every POLL seconds it
searches the watched pipelines for tickets modified since the last poll, diffs
the fields below against what it saw last time, and posts one message per
ticket that really changed. New notes are found by searching notes created since
the last poll and keeping those linked to a watched ticket.

Restart safety: on start it reads every watched ticket SILENTLY and only reports
changes after that moment. Nothing is replayed after a redeploy; a change made
during the ~1 minute the container is down is not reported.

Reads HubSpot only. Never writes a ticket.

Env:
  TICKET_WATCH            off (default) | log | slack
                          log = compose and print to the container log, post nothing
  TICKET_WATCH_PIPELINES  comma-separated pipeline ids (default 2611789549, HubSpot Ops)
  TICKET_WATCH_POLL       seconds between polls (default 60)
  SLACK_WEBHOOK_URL       the sweep channel webhook (shared with weekly_sweep.py)

  python3 ticket_watch.py          run the loop
  python3 ticket_watch.py --once   seed, then print what the next poll would post, then exit
"""
import html, json, os, re, sys, time, urllib.error, urllib.request
from datetime import datetime

TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
MODE = os.environ.get("TICKET_WATCH", "off").strip().lower()
PIPELINES = [p.strip() for p in os.environ.get("TICKET_WATCH_PIPELINES", "2611789549").split(",") if p.strip()]
POLL = int(os.environ.get("TICKET_WATCH_POLL", "60"))
PORTAL = "244038625"
OVERLAP_MS = 120_000  # search index lags writes; re-read a 2 min overlap and diff, so nothing doubles

FIELDS = ["subject", "hs_pipeline", "hs_pipeline_stage", "hubspot_owner_id", "hs_ticket_priority"]
LABELS = {"hs_pipeline": "Board", "hs_pipeline_stage": "Stage", "hubspot_owner_id": "Owner",
          "hs_ticket_priority": "Priority", "subject": "Title"}
API = "https://api.hubapi.com"


def req(method, url, body=None):
    r = urllib.request.Request(API + url, data=json.dumps(body).encode() if body is not None else None, method=method)
    r.add_header("Authorization", "Bearer " + TOKEN)
    r.add_header("Content-Type", "application/json")
    # Shares the portal's search budget with the hourly classifier, so a 429 is
    # expected around the top of the hour: back off, honouring Retry-After.
    for attempt in range(6):
        try:
            raw = urllib.request.urlopen(r, timeout=30).read()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503) or attempt == 5:
                raise
            wait = e.headers.get("Retry-After")
            time.sleep(float(wait) if wait else min(2 ** attempt * 2, 30))


def post(text):
    r = urllib.request.Request(WEBHOOK, data=json.dumps({"text": text}).encode(),
                               headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(r, timeout=30).read().decode()


def search(obj, filters, props, sorts=None):
    out, after = [], None
    while True:
        b = {"filterGroups": [{"filters": filters}], "properties": props, "limit": 100}
        if sorts: b["sorts"] = sorts
        if after: b["after"] = after
        d = req("POST", f"/crm/v3/objects/{obj}/search", b)
        out += d.get("results", [])
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: return out
        time.sleep(0.2)


class Names:
    """Pipeline/stage labels and owner names, looked up from HubSpot and refreshed
    hourly so a new stage or a new team member shows by name, not by id."""

    def __init__(self):
        self.pipe, self.stage, self.owner, self.user, self.at = {}, {}, {}, {}, 0

    def refresh(self, force=False):
        if not force and time.time() - self.at < 3600:
            return
        try:
            for p in req("GET", "/crm/v3/pipelines/tickets").get("results", []):
                self.pipe[p["id"]] = p["label"]
                for s in p.get("stages", []):
                    self.stage[s["id"]] = s["label"]
        except Exception as e:
            print("ticket watch: pipeline labels unavailable:", e, flush=True)
        try:
            after = None
            while True:
                d = req("GET", "/crm/v3/owners?limit=100" + (f"&after={after}" if after else ""))
                for o in d.get("results", []):
                    name = " ".join(x for x in (o.get("firstName"), o.get("lastName")) if x) or o.get("email") or o["id"]
                    self.owner[str(o["id"])] = name
                    if o.get("userId"): self.user[str(o["userId"])] = name
                after = d.get("paging", {}).get("next", {}).get("after")
                if not after: break
        except Exception as e:
            print("ticket watch: owner names unavailable:", e, flush=True)
        self.at = time.time()

    def show(self, field, v):
        if not v:
            return "none"
        if field == "hs_pipeline": return self.pipe.get(v, v)
        if field == "hs_pipeline_stage": return self.stage.get(v, v)
        if field == "hubspot_owner_id": return self.owner.get(v, f"owner {v}")
        if field == "hs_ticket_priority": return v.capitalize()
        return v


def _ms(v):
    """HubSpot datetime (ISO or epoch-ms) -> epoch ms, 0 if blank."""
    if not v:
        return 0
    if v.isdigit():
        return int(v)
    return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000)


def link(tid, subject):
    title = (subject or f"Ticket {tid}").replace("<", "").replace(">", "").replace("|", "/")
    return f"<https://app-na2.hubspot.com/contacts/{PORTAL}/record/0-5/{tid}|{title}>"


def note_text(body, limit=300):
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", body or "", flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "…"


class Watch:
    def __init__(self):
        self.names = Names()
        self.seen = {}        # ticket id -> {field: value}
        self.since = 0        # epoch ms; everything at/before this has been seen
        self.notes_seen = set()
        self.notes_ok = True  # flips off for good if the token can't read notes

    def tickets(self, since_ms=None):
        out = []
        for pid in PIPELINES:
            f = [{"propertyName": "hs_pipeline", "operator": "EQ", "value": pid}]
            if since_ms is not None:
                f.append({"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": str(since_ms)})
            out += search("tickets", f, FIELDS + ["createdate", "hs_lastmodifieddate", "hs_updated_by_user_id"])
        return out

    def seed(self):
        self.names.refresh(force=True)
        self.since = int(time.time() * 1000)
        for t in self.tickets():
            self.seen[t["id"]] = {k: t["properties"].get(k) for k in FIELDS}
        if self.notes_ok:
            try:
                for n in search("notes", [{"propertyName": "hs_createdate", "operator": "GTE",
                                           "value": str(self.since - OVERLAP_MS)}], ["hs_createdate"]):
                    self.notes_seen.add(n["id"])
            except urllib.error.HTTPError as e:
                self.notes_off(e)
        print(f"ticket watch: seeded {len(self.seen)} tickets in "
              f"{', '.join(self.names.pipe.get(p, p) for p in PIPELINES)}; mode={MODE}", flush=True)

    def notes_off(self, e):
        self.notes_ok = False
        print(f"ticket watch: notes not readable (HTTP {e.code}), reporting field changes only", flush=True)

    def ticket_messages(self, start_ms):
        msgs = []
        for t in self.tickets(start_ms - OVERLAP_MS):
            p, tid = t["properties"], t["id"]
            now = {k: p.get(k) for k in FIELDS}
            before = self.seen.get(tid)
            self.seen[tid] = now
            who = self.names.user.get(p.get("hs_updated_by_user_id") or "")
            by = f"  _by {who}_" if who else ""
            if before is None:
                created = _ms(p.get("createdate"))
                head = "New ticket" if created >= self.since - OVERLAP_MS else "Ticket moved onto the board"
                bits = [f"{LABELS[k]} {self.names.show(k, now[k])}" for k in ("hs_pipeline_stage", "hubspot_owner_id", "hs_ticket_priority") if now[k]]
                msgs.append(f":ticket: *{head}*: {link(tid, now['subject'])}\n" + " · ".join(bits) + by)
                continue
            diffs = [f"• {LABELS[k]}: {self.names.show(k, before[k])} → {self.names.show(k, now[k])}"
                     for k in FIELDS if before.get(k) != now[k]]
            if diffs:
                msgs.append(f":arrows_counterclockwise: *Ticket updated*: {link(tid, now['subject'])}\n" + "\n".join(diffs) + by)
        return msgs

    def note_messages(self, start_ms):
        if not self.notes_ok:
            return []
        try:
            notes = search("notes", [{"propertyName": "hs_createdate", "operator": "GTE",
                                      "value": str(start_ms - OVERLAP_MS)}],
                           ["hs_note_body", "hubspot_owner_id", "hs_createdate"],
                           sorts=[{"propertyName": "hs_createdate", "direction": "ASCENDING"}])
        except urllib.error.HTTPError as e:
            self.notes_off(e)
            return []
        msgs = []
        for n in notes:
            if n["id"] in self.notes_seen:
                continue
            self.notes_seen.add(n["id"])
            a = req("GET", f"/crm/v4/objects/notes/{n['id']}/associations/tickets")
            tids = [str(r["toObjectId"]) for r in a.get("results", []) if str(r["toObjectId"]) in self.seen]
            if not tids:
                continue
            p = n["properties"]
            who = self.names.owner.get(p.get("hubspot_owner_id") or "", "someone")
            text = note_text(p.get("hs_note_body"))
            quoted = "\n".join("> " + line for line in text.splitlines()) if text else "> (no text)"
            for tid in tids:
                msgs.append(f":memo: *Note from {who}* on {link(tid, self.seen[tid].get('subject'))}\n{quoted}")
            time.sleep(0.1)
        return msgs

    def poll(self):
        self.names.refresh()
        start, now = self.since, int(time.time() * 1000)
        msgs = self.ticket_messages(start) + self.note_messages(start)
        self.since = now  # only after a clean read, so a failed poll is retried from the same point
        if len(self.notes_seen) > 5000:  # only the overlap window matters
            self.notes_seen = set(list(self.notes_seen)[-1000:])
        return msgs


def deliver(msgs):
    for m in msgs:
        if MODE == "slack" and WEBHOOK:
            try:
                post(m)
            except Exception as e:
                print("ticket watch: post failed:", e, "|", m.splitlines()[0], flush=True)
                continue
        print(("ticket watch posted: " if MODE == "slack" else "ticket watch [log]: ") + m.replace("\n", " | "), flush=True)


def main():
    if MODE not in ("log", "slack"):
        print("ticket watch: off (set TICKET_WATCH=log or slack)", flush=True)
        return
    if MODE == "slack" and not WEBHOOK:
        print("ticket watch: SLACK_WEBHOOK_URL not set, logging only", flush=True)
    w = Watch()
    while True:
        try:
            w.seed()
            break
        except Exception as e:
            print("ticket watch: seed failed, retrying in 60s:", e, flush=True)
            time.sleep(60)
    if "--once" in sys.argv:
        time.sleep(POLL)
        for m in w.poll():
            print(m + "\n", flush=True)
        return
    while True:
        time.sleep(POLL)
        try:
            deliver(w.poll())
        except Exception as e:
            # the cursor did not move, so the next poll re-reads this window
            print("ticket watch: poll error:", type(e).__name__, e, flush=True)


if __name__ == "__main__":
    main()
