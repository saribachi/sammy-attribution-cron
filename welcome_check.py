"""Welcome email failed -> task for Krishna (promised on the 19 Oct Sammy call).

"Welcome, new payer (Krishna's cheat sheet)" (flow 4898086609, email 394434481855,
campaign 37291238) should reach every new payer and carded trial. When HubSpot doesn't
deliver it, Krishna gets ONE task on the contact saying why, so he can send the cheat sheet
1:1 from his inbox (sales template "Welcome cheat sheet (1:1)") or call them.

A contact counts when it entered paid_customer / carded_trial on or after SINCE (from
user_status history), it isn't an internal address, and it isn't flagged by the churn
check (the Welcome suppresses those on purpose, list 1049). Then:
  - a DROPPED / SUPPRESSED / BOUNCE event for the Welcome      -> reason from HubSpot
  - no Welcome sent after it entered, and the flow's send window has passed -> not_sent
    (returning customers always land here: became_paid_customer_date keeps the FIRST
    paid date, so the flow's 7-day filter never admits them)
A carded trial who later pays already had the Welcome as a trial: no task.

Properties it owns (group welcome_check): welcome_failed_on, welcome_failed_reason,
welcome_failed_detail, welcome_failed_task_id, welcome_returning_customer.
welcome_failed_on is the one-task-per-contact guard: it is written before the task and
never cleared, so a contact gets at most one task, ever.

  python3 welcome_check.py                         dry run, prints every task it would create
  python3 welcome_check.py --commit                create tasks + stamp properties
  python3 welcome_check.py --commit --only <id>    just this contact
  python3 welcome_check.py --test <id>             TEST task on that contact owned by Sarah, no stamps
Env WELCOME_CHECK=off mutes it in the hourly run (default on).
"""
import json, os, sys, time, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
MEL = ZoneInfo("Australia/Melbourne")
SINCE = datetime(2026, 10, 26, tzinfo=timezone.utc)  # go-live; earlier cases are in welcome-failed list of 26 Oct
APP_ID, CAMPAIGN = 2286, 37291238
KRISHNA, SARAH = "162267743", "366344191"
INTERNAL = ("@withsammy.ai", "@hirecharm.com")
P = ["email", "firstname", "lastname", "company", "phone", "mobilephone", "user_status",
     "became_paid_customer_date", "hs_marketable_status", "hs_email_optout", "hs_email_hard_bounce_reason_enum",
     "hs_email_last_send_date", "hs_email_sends_since_last_engagement", "churn_check_ever_flagged",
     "welcome_failed_on", "hs_additional_emails"]
LABEL = {"bounced": "bounced", "previously_bounced": "address bounced before",
         "opted_out": "opted out", "unengaged": "marked unengaged", "not_marketing": "not a marketing contact",
         "blocked_other": "blocked by HubSpot", "not_sent": "never sent"}
ACTION = {
    "bounced": "The address is dead. Don't email it. Call or SMS them for a working email, update the contact, then send the 'Welcome cheat sheet (1:1)' template.",
    "previously_bounced": "The address is dead. Don't email it. Call or SMS them for a working email, update the contact, then send the 'Welcome cheat sheet (1:1)' template.",
    "opted_out": "They unsubscribed from Sammy's marketing emails, so don't email them. Call them instead and walk them through getting started.",
    "unengaged": "HubSpot held it back because they haven't opened recent emails. Send the 'Welcome cheat sheet (1:1)' template from your inbox.",
    "not_marketing": "They aren't a marketing contact, so HubSpot can't send them marketing emails. Send the 'Welcome cheat sheet (1:1)' template from your inbox.",
    "blocked_other": "Send the 'Welcome cheat sheet (1:1)' template from your inbox. If it bounces, call them.",
    "not_sent": "Send the 'Welcome cheat sheet (1:1)' template from your inbox.",
}


def req(method, url, body=None):
    for attempt in range(6):
        r = urllib.request.Request("https://api.hubapi.com" + url, method=method,
                                   data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        try:
            raw = urllib.request.urlopen(r, timeout=60).read()
            time.sleep(0.2)
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503) or attempt == 5:
                raise RuntimeError(f"HTTP {e.code} {method} {url}: {e.read().decode()[:300]}")
            wait = e.headers.get("Retry-After")
            time.sleep(float(wait) if wait else min(2 ** attempt * 2, 30))


def search(filters, props):
    out, after = [], None
    while True:
        b = {"filterGroups": [{"filters": filters}], "properties": props, "limit": 200}
        if after: b["after"] = after
        d = req("POST", "/crm/v3/objects/contacts/search", b)
        out += d["results"]
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: return out


def events(etype):
    out, off = [], None
    while True:
        d = req("GET", f"/email/public/v1/events?appId={APP_ID}&campaignId={CAMPAIGN}&eventType={etype}&limit=1000"
                + (f"&offset={off}" if off else ""))
        out += d.get("events", [])
        if not d.get("hasMore"): return out
        off = d["offset"]


def ts(v):
    if not v: return None
    if str(v).isdigit(): return datetime.fromtimestamp(int(v) / 1000, timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def mel(d):
    return d.astimezone(MEL).strftime("%a %d %b %H:%M") if d else "?"


def first_send_slot(t):
    """Flow: 60 min delay, then sends Mon-Fri 07:00-18:00 Melbourne."""
    s = (t + timedelta(minutes=60)).astimezone(MEL)
    while not (s.weekday() < 5 and 7 <= s.hour < 18):
        s = (s if s.weekday() < 5 and s.hour < 7 else s + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    return s


def due_ms(now):
    """Lucas task-flow rule (5090248431): 17:00 Melbourne, weekdays only; past 17:00 -> next weekday."""
    d = now.astimezone(MEL)
    d = d.replace(hour=17, minute=0, second=0, microsecond=0) if d.hour < 17 else (d + timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)
    while d.weekday() >= 5: d += timedelta(days=1)
    return int(d.timestamp() * 1000)


def status_story(hist):
    """(entered current status at, returning?, churned at, previous status) from user_status history (newest first)."""
    vers = [(v.get("value"), ts(v.get("timestamp"))) for v in reversed(hist or [])]  # oldest first
    real = []
    for val, t in vers:
        if not real or real[-1][0] != val: real.append((val, t))  # only genuine changes
    if not real: return None, False, None, None
    j = len(real) - 1  # skip sync blips: left the current status and came back within 24h (e.g. paid -> churned -> paid in 5 min)
    while True:
        k = next((k for k in range(j - 2, -1, -1) if real[k][0] == real[j][0] and real[j][1] - real[k + 1][1] < timedelta(hours=24)), None)
        if k is None: break
        j = k
    entered, prev = real[j][1], (real[j - 1][0] if j else None)
    churned_at = max((t for v, t in real[:j] if v == "churned"), default=None)
    return entered, churned_at is not None, churned_at, prev


def reason_of(e):
    t, r = e.get("type"), (e.get("dropReason") or e.get("suppressedReason") or "")
    if t == "BOUNCE": return "bounced", f"HubSpot sent it but it bounced ({e.get('category') or 'bounce'}: {(e.get('response') or e.get('status') or '')[:120]})"
    if r == "PREVIOUSLY_BOUNCED": return "previously_bounced", "HubSpot dropped it: the address bounced before"
    if "UNSUBSCRIBED" in r or "OPT_OUT" in r: return "opted_out", f"HubSpot dropped it: {e.get('dropMessage') or r}"
    if r == "GRAYMAIL_SUPPRESSED": return "unengaged", "HubSpot held it back: it marks them unengaged (no opens/clicks on recent emails)"
    if r == "NON_MARKETABLE_CONTACT": return "not_marketing", "HubSpot held it back: not a marketing contact"
    return "blocked_other", f"HubSpot {'dropped' if t == 'DROPPED' else 'held back'} it: {r} {e.get('dropMessage') or e.get('suppressedMessage') or ''}".strip()


def find(only=None):
    now = datetime.now(timezone.utc)
    cands = search([{"propertyName": "user_status", "operator": "IN", "values": ["paid_customer", "carded_trial"]}], P)
    if only: cands = [c for c in cands if c["id"] in only]
    cands = [c for c in cands if not c["properties"].get("welcome_failed_on")
             and c["properties"].get("churn_check_ever_flagged") != "true"
             and not (c["properties"].get("email") or "").lower().endswith(INTERNAL)]
    hist = {}
    for i in range(0, len(cands), 50):
        d = req("POST", "/crm/v3/objects/contacts/batch/read",
                {"inputs": [{"id": c["id"]} for c in cands[i:i + 50]], "properties": ["email"], "propertiesWithHistory": ["user_status"]})
        for r in d.get("results", []): hist[r["id"]] = r.get("propertiesWithHistory", {}).get("user_status")
    sent, bad = {}, {}
    for e in events("SENT"): sent.setdefault((e.get("recipient") or "").lower(), []).append(ts(e["created"]))
    for et in ("DROPPED", "SUPPRESSED", "BOUNCE"):
        for e in events(et):
            e["type"] = et
            bad.setdefault((e.get("recipient") or "").lower(), []).append(e)
    found = []
    for c in cands:
        p = c["properties"]; email = (p.get("email") or "").lower()
        emails = {email} | {x.strip().lower() for x in (p.get("hs_additional_emails") or "").split(";") if x.strip()}
        entered, returning, churned_at, prev = status_story(hist.get(c["id"]))
        if not entered or entered < SINCE: continue
        if p["user_status"] == "paid_customer" and prev == "carded_trial" and any(sent.get(x) for x in emails): continue  # welcomed as a trial
        if any(t >= entered - timedelta(hours=1) for x in emails for t in sent.get(x, [])): continue  # delivered after they entered
        fails = [e for x in emails for e in bad.get(x, []) if ts(e["created"]) >= entered - timedelta(hours=1)]
        if fails:
            e = max(fails, key=lambda x: x["created"])
            reason, why = reason_of(e)
            when = ts(e["created"])
        else:
            slot = first_send_slot(entered)
            if now < slot + timedelta(hours=3): continue  # flow may still send it
            reason, when = "not_sent", now
            why = ("Never sent: the Welcome flow only welcomes first-time payers (paid in the last 7 days), so it skips returning customers"
                   if returning else f"Never sent: no send, block or bounce recorded; it was due by {mel(slot)} Melbourne. The flow didn't enrol them")
            if p.get("hs_email_hard_bounce_reason_enum"):  # emailing them won't work either
                reason, why = "previously_bounced", why + f". Their address also hard-bounced before ({p['hs_email_hard_bounce_reason_enum']})"
            elif p.get("hs_email_optout") == "true":
                reason, why = "opted_out", why + ". They have also opted out of all email"
        found.append({"id": c["id"], "p": p, "reason": reason, "why": why, "when": when, "entered": entered,
                      "returning": returning, "churned_at": churned_at, "prev": prev})
    return found


def task_text(f):
    p = f["p"]
    name = " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x) or p.get("email")
    subj = f"WELCOME NOT SENT: {name} - {LABEL[f['reason']]}" + (" [RETURNING CUSTOMER]" if f["returning"] else "")
    status = "paying" if p["user_status"] == "paid_customer" else "carded trial"
    first_paid = ts(p.get("became_paid_customer_date"))
    lines = [
        f"Sammy's welcome cheat sheet didn't reach this new {'customer' if status == 'paying' else 'carded trial'}.",
        "",
        f"WHY: {f['why']} ({mel(f['when'])} Melbourne).",
        f"WHAT TO DO: {ACTION[f['reason']]}",
        "",
        "CHECK:",
        f"- Email: {p.get('email')}  |  Phone: {p.get('mobilephone') or p.get('phone') or 'none on file'}",
        f"- Became {status}: {mel(f['entered'])} Melbourne" + (f" (from {f['prev']})" if f["prev"] else ""),
    ]
    if f["returning"]:
        lines.append(f"- RETURNING CUSTOMER: churned {mel(f['churned_at'])}, came back {mel(f['entered'])}. First paid {mel(first_paid)}. Consider a 'welcome back' line.")
    lines += [
        f"- Marketing contact: {p.get('hs_marketable_status') or '?'}  |  Opted out of all email: {p.get('hs_email_optout') or 'no'}"
        f"  |  Hard bounce: {p.get('hs_email_hard_bounce_reason_enum') or 'none'}",
        f"- Last marketing email sent: {(p.get('hs_email_last_send_date') or 'never')[:10]}  |  Sends since they last opened/clicked: {p.get('hs_email_sends_since_last_engagement') or '0'}",
        "",
        "Done? Mark this task complete. One task per contact, it won't come back.",
    ]
    return subj, "<br>".join(x.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for x in lines)


def create_task(cid, subj, body, owner, now):
    return req("POST", "/crm/v3/objects/tasks", {
        "properties": {"hs_task_subject": subj, "hs_task_body": body, "hs_task_priority": "HIGH",
                       "hs_task_status": "NOT_STARTED", "hs_task_type": "TODO",
                       "hs_timestamp": due_ms(now), "hubspot_owner_id": owner},
        "associations": [{"to": {"id": cid},
                          "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 204}]}]})["id"]


def run(commit=False, only=None, test=None):
    if os.environ.get("WELCOME_CHECK", "on") == "off" and not (only or test):
        print("welcome check: off (WELCOME_CHECK=off)", flush=True); return []
    now = datetime.now(timezone.utc)
    if test:  # the pinned test: any reason, task to Sarah, nothing stamped
        c = req("GET", f"/crm/v3/objects/contacts/{test}?properties=" + ",".join(P) + "&propertiesWithHistory=user_status")
        entered, returning, churned_at, prev = status_story(c.get("propertiesWithHistory", {}).get("user_status"))
        f = {"id": test, "p": c["properties"], "reason": "unengaged", "why": "TEST of the task layout",
             "when": now, "entered": entered or now, "returning": returning, "churned_at": churned_at, "prev": prev}
        subj, body = task_text(f)
        tid = create_task(test, "TEST " + subj, body, SARAH, now)
        print("welcome check TEST task", tid, "on", test, "|", subj, flush=True); return [tid]
    found = find(only)
    for f in found:
        subj, body = task_text(f)
        print(f"welcome check: {f['id']} {f['p'].get('email')} | {f['reason']} | {subj}" + ("" if commit else " [dry-run]"), flush=True)
        if not commit: continue
        stamp = {"welcome_failed_on": int(f["when"].timestamp() * 1000), "welcome_failed_reason": f["reason"],
                 "welcome_failed_detail": f["why"][:1000], "welcome_returning_customer": "true" if f["returning"] else "false"}
        req("PATCH", f"/crm/v3/objects/contacts/{f['id']}", {"properties": stamp})  # claim first: never two tasks
        tid = create_task(f["id"], subj, body, KRISHNA, now)
        req("PATCH", f"/crm/v3/objects/contacts/{f['id']}", {"properties": {"welcome_failed_task_id": tid}})
        print(f"welcome check: task {tid} -> Krishna for {f['id']}", flush=True)
    print(f"welcome check: {len(found)} new welcome failures" + ("" if commit else " [dry-run]"), flush=True)
    return found


if __name__ == "__main__":
    a = sys.argv[1:]
    only = set(a[a.index("--only") + 1].split(",")) if "--only" in a else None
    test = a[a.index("--test") + 1] if "--test" in a else None
    run(commit="--commit" in a, only=only, test=test)
