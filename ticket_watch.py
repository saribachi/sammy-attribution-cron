"""Ticket watch: post new HubSpot Ops tickets and Krishna's comments to the sweep channel.

Why (Chris, 21 Oct 2026): a new Ops ticket, or Krishna answering on one, was only
visible by opening HubSpot. This puts each in the internal sweep channel
(Sarah + Chris; Sammy does not use Slack) within a minute. Only those two events:
stage, owner and other field changes are deliberately not posted.

How: a long-running loop next to the hourly classifier. Every POLL seconds it
searches the Ops board for tickets created since the last poll, and notes
created since the last poll that are owned by a watched commenter and linked to
an Ops ticket. Krishna's comments arrive through his assistant's integration
("Krishna Connection", source INTEGRATION 40740797) with his owner id and NO
user id, so commenters are matched on the note's owner, not on who wrote it.

Restart safety: on start it reads the board and recent notes SILENTLY and only
reports what appears after that moment. Nothing is replayed after a redeploy; a
ticket or comment made during the ~1 minute the container is down is not posted.

Reads HubSpot only. Never writes anything.

Env:
  TICKET_WATCH             off (default) | log | slack
                           log = compose and print to the container log, post nothing
  TICKET_WATCH_PIPELINES   comma-separated pipeline ids (default 2611789549, HubSpot Ops)
  TICKET_WATCH_COMMENTERS  comma-separated owner ids whose notes post (default 162267743, Krishna)
  TICKET_WATCH_POLL        seconds between polls (default 60)
  SLACK_WEBHOOK_URL        the sweep channel webhook (shared with weekly_sweep.py)

  python3 ticket_watch.py          run the loop
  python3 ticket_watch.py --once   seed, then print what the next poll would post, then exit
"""
import html, json, os, re, sys, time, urllib.error, urllib.request

TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
MODE = os.environ.get("TICKET_WATCH", "off").strip().lower()
PIPELINES = [p.strip() for p in os.environ.get("TICKET_WATCH_PIPELINES", "2611789549").split(",") if p.strip()]
COMMENTERS = {p.strip() for p in os.environ.get("TICKET_WATCH_COMMENTERS", "162267743").split(",") if p.strip()}
POLL = int(os.environ.get("TICKET_WATCH_POLL", "60"))
PORTAL = "244038625"
OVERLAP_MS = 120_000  # search index lags writes; re-read a 2 min overlap, ids already posted are skipped
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
    """Stage labels and owner names, looked up from HubSpot and refreshed hourly
    so a new stage or a new team member shows by name, not by id."""

    def __init__(self):
        self.stage, self.owner, self.at = {}, {}, 0

    def refresh(self, force=False):
        if not force and time.time() - self.at < 3600:
            return
        try:
            for p in req("GET", "/crm/v3/pipelines/tickets").get("results", []):
                for s in p.get("stages", []):
                    self.stage[s["id"]] = s["label"]
        except Exception as e:
            print("ticket watch: stage labels unavailable:", e, flush=True)
        try:
            after = None
            while True:
                d = req("GET", "/crm/v3/owners?limit=100" + (f"&after={after}" if after else ""))
                for o in d.get("results", []):
                    name = " ".join(x for x in (o.get("firstName"), o.get("lastName")) if x) or o.get("email") or o["id"]
                    self.owner[str(o["id"])] = name
                after = d.get("paging", {}).get("next", {}).get("after")
                if not after: break
        except Exception as e:
            print("ticket watch: owner names unavailable:", e, flush=True)
        self.at = time.time()


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
        self.subject = {}     # Ops ticket id -> subject (every ticket on the board)
        self.since = 0        # epoch ms; everything before this has been seen
        self.notes_seen = set()

    def tickets(self, since_ms=None):
        out = []
        for pid in PIPELINES:
            f = [{"propertyName": "hs_pipeline", "operator": "EQ", "value": pid}]
            if since_ms is not None:
                f.append({"propertyName": "createdate", "operator": "GTE", "value": str(since_ms)})
            out += search("tickets", f, ["subject", "hs_pipeline_stage", "hubspot_owner_id", "hs_ticket_priority"])
        return out

    def notes(self, since_ms):
        return search("notes", [{"propertyName": "hs_createdate", "operator": "GTE", "value": str(since_ms)}],
                      ["hs_note_body", "hubspot_owner_id"],
                      sorts=[{"propertyName": "hs_createdate", "direction": "ASCENDING"}])

    def seed(self):
        self.names.refresh(force=True)
        self.since = int(time.time() * 1000)
        for t in self.tickets():
            self.subject[t["id"]] = t["properties"].get("subject")
        for n in self.notes(self.since - OVERLAP_MS):
            self.notes_seen.add(n["id"])
        print(f"ticket watch: seeded {len(self.subject)} Ops tickets; commenters "
              f"{', '.join(self.names.owner.get(c, c) for c in sorted(COMMENTERS))}; mode={MODE}", flush=True)

    def new_tickets(self, start_ms):
        msgs = []
        for t in self.tickets(start_ms - OVERLAP_MS):
            if t["id"] in self.subject:
                continue
            p = t["properties"]
            self.subject[t["id"]] = p.get("subject")
            bits = [x for x in (
                self.names.stage.get(p.get("hs_pipeline_stage") or "", ""),
                self.names.owner.get(p.get("hubspot_owner_id") or "", ""),
                (p.get("hs_ticket_priority") or "").capitalize() and f"{p['hs_ticket_priority'].capitalize()} priority") if x]
            msgs.append(f":ticket: *New Ops ticket*: {link(t['id'], p.get('subject'))}" + ("\n" + " · ".join(bits) if bits else ""))
        return msgs

    def comments(self, start_ms):
        msgs = []
        for n in self.notes(start_ms - OVERLAP_MS):
            if n["id"] in self.notes_seen:
                continue
            p = n["properties"]
            if (p.get("hubspot_owner_id") or "") not in COMMENTERS:
                self.notes_seen.add(n["id"])
                continue
            a = req("GET", f"/crm/v4/objects/notes/{n['id']}/associations/tickets")
            self.notes_seen.add(n["id"])
            tids = [str(r["toObjectId"]) for r in a.get("results", []) if str(r["toObjectId"]) in self.subject]
            who = self.names.owner.get(p["hubspot_owner_id"], "Krishna")
            text = note_text(p.get("hs_note_body"))
            quoted = "\n".join("> " + line for line in text.splitlines()) if text else "> (no text)"
            for tid in tids:
                msgs.append(f":speech_balloon: *{who} commented* on {link(tid, self.subject[tid])}\n{quoted}")
            time.sleep(0.1)
        return msgs

    def poll(self):
        self.names.refresh()
        start, now = self.since, int(time.time() * 1000)
        msgs = self.new_tickets(start) + self.comments(start)
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
