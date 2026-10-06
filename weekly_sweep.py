"""Weekly Sammy sweep: a short, change-only health report posted to Slack.

Runs on the same always-on container as the hourly classifier. Fires once per
ISO week, at/after Monday 15:00 UTC (8am US Pacific); a failure retries hourly
for the rest of that Monday and posts one FAILED line.

The post reports what changed this week (customers gained and lost, trial
activation, the post-purchase flows, email send health), then lists only what is
new or worsening. Every watch is counted over the last 7 days; a quiet watch
says nothing. Standing conditions are deliberately not reported every week.
The Gone Quiet week-over-week delta uses a baseline in /tmp, so it is omitted
for the first post after a redeploy.
"""
import json, os, time, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone

TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
MARKER = "/tmp/sweep_week.txt"
FAILED_MARKER = "/tmp/sweep_failed_week.txt"
BASELINE = "/tmp/sweep_baseline.json"

PLAN_VALUES = {"founder_59", "monthly_99", "annual_950", "free"}
PROMO_VALUES = {"qRlQX1PO", "oJiHwI0k"}
# same pricing model as the dashboard and deal stamper
PLAN_AMOUNT = {"founder_59": 59, "monthly_99": 99, "annual_950": 79}
PROMO_MONTHLY_DISCOUNT = {"qRlQX1PO": 10}


def req(method, url, body=None):
    r = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
    r.add_header("Authorization", "Bearer " + TOKEN)
    r.add_header("Content-Type", "application/json")
    # The sweep runs straight after the classifier has spent the search budget,
    # so a 429 here is expected now and then: back off and retry, honouring
    # Retry-After when HubSpot sends it.
    for attempt in range(6):
        try:
            resp = urllib.request.urlopen(r, timeout=30)
            raw = resp.read()
            time.sleep(0.25)
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503) or attempt == 5:
                raise
            wait = e.headers.get("Retry-After")
            time.sleep(float(wait) if wait else min(2 ** attempt * 2, 30))


def total(filters):
    d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/search",
            {"filterGroups": [{"filters": filters}], "limit": 1})
    return d["total"]


def deal_total(filters):
    d = req("POST", "https://api.hubapi.com/crm/v3/objects/deals/search",
            {"filterGroups": [{"filters": filters}], "limit": 1})
    return d["total"]


def _when(v):
    """HubSpot datetime, epoch-ms or plain date -> aware UTC datetime (None if blank)."""
    if not v:
        return None
    if v.isdigit():
        return datetime.fromtimestamp(int(v) / 1000, timezone.utc)
    if "T" in v:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    return datetime.fromisoformat(v).replace(tzinfo=timezone.utc)


def _search_all(obj, filters, props, page=200):
    out, after = [], None
    while True:
        b = {"filterGroups": [{"filters": filters}], "properties": props, "limit": page}
        if after: b["after"] = after
        d = req("POST", f"https://api.hubapi.com/crm/v3/objects/{obj}/search", b)
        out += d["results"]
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: return out


def collect():
    """Everything the post reports is about THIS week: what changed, and anything
    new or worsening. Standing conditions that never move are deliberately not
    measured here, so they cannot fill the post with the same lines every week."""
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    week_ms = str(int(week_ago.timestamp() * 1000))
    c = {}

    # ---- Customers: who is paying, who joined, who left
    paid = _search_all("contacts", [{"propertyName": "user_status", "operator": "EQ", "value": "paid_customer"}],
                       ["sammy_pricing_plan", "sammy_promo_code", "became_paid_customer_date", "last_estimate_date",
                        "last_app_login_date", "quotes_since_purchase"])
    c["paid"] = len(paid)
    c["mrr"] = sum(max(PLAN_AMOUNT.get(p["properties"].get("sammy_pricing_plan"), 59)
                       - PROMO_MONTHLY_DISCOUNT.get(p["properties"].get("sammy_promo_code"), 0), 0) for p in paid)
    c["new_payers"] = sum(1 for p in paid if (_when(p["properties"].get("became_paid_customer_date")) or now) >= week_ago
                          and p["properties"].get("became_paid_customer_date"))
    churned = _search_all("contacts", [{"propertyName": "user_status", "operator": "EQ", "value": "churned"}], ["email"])
    c["churned"] = 0
    ids = [x["id"] for x in churned]
    for i in range(0, len(ids), 50):
        d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/read",
                {"properties": ["email"], "propertiesWithHistory": ["user_status"], "inputs": [{"id": x} for x in ids[i:i + 50]]})
        for r in d.get("results", []):
            h = r.get("propertiesWithHistory", {}).get("user_status", [])
            if h and h[0]["value"] == "churned" and _when(h[0]["timestamp"]) >= week_ago:
                c["churned"] += 1
    c["unknown_plans"] = sorted({p["properties"].get("sammy_pricing_plan") for p in paid} - PLAN_VALUES - {None})
    c["unknown_promos"] = sorted({p["properties"].get("sammy_promo_code") for p in paid} - PROMO_VALUES - {None})
    c["paid_undated"] = sum(1 for p in paid if not p["properties"].get("became_paid_customer_date"))
    c["paid_future"] = sum(1 for p in paid if (_when(p["properties"].get("became_paid_customer_date")) or now) > now)

    # ---- Trials: started this week, and how last week's cohort activated
    trials = _search_all("contacts", [{"propertyName": "sammy_trial_start_date", "operator": "GTE",
                                       "value": str(int((now - timedelta(days=14)).timestamp() * 1000))}],
                         ["sammy_trial_start_date", "estimate_count"])
    this_wk = [t for t in trials if _when(t["properties"].get("sammy_trial_start_date")) >= week_ago]
    last_wk = [t for t in trials if _when(t["properties"].get("sammy_trial_start_date")) < week_ago]
    c["trials"] = len(this_wk)
    c["prev_trials"] = len(last_wk)
    c["prev_activated"] = sum(1 for t in last_wk if float(t["properties"].get("estimate_count") or 0) >= 1)

    # ---- Post-purchase flows: are they working
    def dormant(p):
        pr = p["properties"]
        bp, lg, es = (_when(pr.get(k)) for k in ("became_paid_customer_date", "last_app_login_date", "last_estimate_date"))
        return bp and (now - bp).days > 42 and (not lg or (now - lg).days > 10) and (not es or (now - es).days > 10)
    dorm = [p for p in paid if dormant(p)]
    c["gone_quiet"] = len(dorm)
    c["gone_quiet_mrr"] = sum(max(PLAN_AMOUNT.get(p["properties"].get("sammy_pricing_plan"), 59)
                                  - PROMO_MONTHLY_DISCOUNT.get(p["properties"].get("sammy_promo_code"), 0), 0) for p in dorm)
    fsw = [p for p in paid if _when(p["properties"].get("became_paid_customer_date"))
           and 14 < (now - _when(p["properties"]["became_paid_customer_date"])).days <= 28]
    c["fsw"] = len(fsw)
    c["fsw_hit3"] = sum(1 for p in fsw if float(p["properties"].get("quotes_since_purchase") or 0) >= 3)

    # ---- Watches, all counted over THIS week only
    c["new_no_channel"] = total([{"propertyName": "createdate", "operator": "GTE", "value": week_ms},
                                 {"propertyName": "original_source_channel", "operator": "NOT_HAS_PROPERTY"}])
    d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/search", {
        "filterGroups": [{"filters": [
            {"propertyName": "createdate", "operator": "GTE", "value": week_ms},
            {"propertyName": "hs_object_source_detail_1", "operator": "CONTAINS_TOKEN", "value": "Outbound"}]}],
        "properties": ["email"], "limit": 100})
    c["webhook_sysinbox"] = sum(1 for r in d["results"] if any(
        (r["properties"].get("email") or "").startswith(p) for p in ("support@", "noreply@", "no-reply@", "notifications@")))
    c["aircall_new"] = total([
        {"propertyName": "hs_object_source_detail_1", "operator": "CONTAINS_TOKEN", "value": "Aircall"},
        {"propertyName": "email", "operator": "NOT_HAS_PROPERTY"},
        {"propertyName": "createdate", "operator": "GTE", "value": week_ms}])
    # duplicate wins created THIS week: a won deal from the last 7 days on a customer who already had one
    won = _search_all("deals", [{"propertyName": "pipeline", "operator": "EQ", "value": "default"},
                                {"propertyName": "dealstage", "operator": "EQ", "value": "decisionmakerboughtin"}],
                      ["createdate"], page=100)
    by_contact = {}
    wids = [x["id"] for x in won]
    created = {x["id"]: _when(x["properties"].get("createdate")) for x in won}
    for i in range(0, len(wids), 100):
        aa = req("POST", "https://api.hubapi.com/crm/v4/associations/deals/contacts/batch/read",
                 {"inputs": [{"id": x} for x in wids[i:i + 100]]})
        for r in aa.get("results", []):
            to = r.get("to") or []
            if to: by_contact.setdefault(str(to[0]["toObjectId"]), []).append(created.get(str(r["from"]["id"])))
    c["new_dupe_won"] = sum(1 for ds in by_contact.values()
                            if len(ds) >= 2 and max(x for x in ds if x) >= week_ago)
    # demos held THIS week that still have no outcome. Demos only: an unmarked demo
    # breaks same-day-close reporting; internal meetings and support calls do not.
    stale = 0
    for f in ([{"propertyName": "hs_meeting_outcome", "operator": "EQ", "value": "SCHEDULED"}],
              [{"propertyName": "hs_meeting_outcome", "operator": "NOT_HAS_PROPERTY"}]):
        dd = req("POST", "https://api.hubapi.com/crm/v3/objects/meetings/search", {
            "filterGroups": [{"filters": [{"propertyName": "hs_timestamp", "operator": "BETWEEN", "value": week_ms,
                                           "highValue": str(int(now.timestamp() * 1000))},
                                          {"propertyName": "is_demo", "operator": "EQ", "value": "true"}] + f}], "limit": 1})
        stale += dd["total"]
    c["stale_meetings"] = stale

    try:
        c["send_audit"] = audit_sends()
    except Exception as e:
        print("send audit error:", e, flush=True)
        c["send_audit"] = {"error": f"{type(e).__name__}: {e}"}

    c["dash_ok"] = False
    for _ in range(2):
        try:
            dash = json.load(urllib.request.urlopen("https://attribution.hirecharm.com/api/data", timeout=120))
            c["dash_ok"] = bool(dash.get("acqRows")) and bool(dash.get("campaignRows")) and bool(dash.get("funnelRows"))
            break
        except Exception:
            time.sleep(10)
    return c


# ---- Send audit -------------------------------------------------------------
# Replays every automated email sent in the last 7 days against the recipient's
# state AT SEND TIME (from property history), so an email reaching someone it was
# not written for shows up within a week instead of being found by hand.
# A repeated hs_email_last_email_name history entry is HubSpot re-stamping the
# field during an unrelated write, not a second send: only first occurrences count.
# Rules keyed by the "Sammy - NN" prefix. An automated email with no rule here is
# reported as unmapped, so a new email cannot slip past the audit silently.
TRIAL = {"active_trial"}
AUDIT_RULES = {
    "01": ("Activation", TRIAL, None), "02": ("Activation", TRIAL, 0), "03": ("Activation Follow-up", TRIAL, 0),
    "18": ("Activation Follow-up", TRIAL, 0), "10": ("Trial Ending", TRIAL, None), "11": ("Trial Ending", TRIAL, None),
    "12": ("Trial Ending", TRIAL, None), "16": ("Win-back", {"trial_expired"}, 0), "17": ("Win-back", {"trial_expired"}, 0),
    "19": ("Second Quote", {"active_trial", "carded_trial"}, None), "20": ("Second Quote", {"active_trial", "carded_trial"}, 1),
    "21": ("First Six Weeks", {"paid_customer"}, None), "22": ("First Six Weeks", {"paid_customer"}, None),
    "23": ("First Six Weeks", {"paid_customer"}, None), "24": ("Customer Gone Quiet", {"paid_customer"}, None),
    "25": ("Customer Gone Quiet", {"paid_customer"}, None), "WNP": ("Welcome, new payer", {"paid_customer", "carded_trial"}, None),
    # retired with the 14 Sep rebuild: any send at all is a misfire
    "04": ("RETIRED email", set(), None), "05": ("RETIRED email", set(), None),
    "06": ("RETIRED email", set(), None), "07": ("RETIRED email", set(), None),
}
PRE_PAID = {"01", "02", "03", "18", "10", "11", "12", "16", "17", "19", "20"}  # never for anyone who has paid
WINDOWED = {"21", "22", "23", "24", "25", "WNP"}  # Mon-Fri 07:00-18:00 Melbourne
GRACE = 600  # seconds: a status that flipped within 10 min of the send was a race, not a misfire


def _ts(v):
    if v.isdigit():
        return datetime.fromtimestamp(int(v) / 1000, timezone.utc)
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def _at(hist, when):
    for e in hist:  # newest first
        if _ts(e["timestamp"]) <= when:
            return e["value"]
    return None


def _key(name):
    if name.startswith("Sammy - ") and name[8:10].isdigit():
        return name[8:10]
    if name.startswith("Welcome, new payer"):
        return "WNP"
    return None


def audit_sends(days=7):
    from zoneinfo import ZoneInfo
    mel = ZoneInfo("Australia/Melbourne")
    since = datetime.now(timezone.utc).timestamp() - days * 86400
    ids, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [{"propertyName": "hs_email_last_send_date", "operator": "GTE",
                                            "value": str(int((since - 86400) * 1000))}]}],
             "properties": ["email"], "limit": 200}
        if after: b["after"] = after
        d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/search", b)
        ids += [r["id"] for r in d["results"]]
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: break
    found, unmapped, checked = {}, {}, 0
    for i in range(0, len(ids), 50):
        d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/read", {
            "properties": ["email"], "inputs": [{"id": x} for x in ids[i:i + 50]],
            "propertiesWithHistory": ["hs_email_last_email_name", "user_status", "estimate_count"]})
        for r in d.get("results", []):
            h = r.get("propertiesWithHistory", {})
            st, ec = h.get("user_status", []), h.get("estimate_count", [])
            seen = set()
            for e in reversed(h.get("hs_email_last_email_name", [])):  # oldest first
                name, t = e["value"], _ts(e["timestamp"])
                if name in seen: continue
                seen.add(name)
                k = _key(name)
                if t.timestamp() < since or not name: continue
                if k is None:
                    if name.startswith("Sammy - "): unmapped[name] = unmapped.get(name, 0) + 1
                    continue
                if k not in AUDIT_RULES:
                    unmapped[name] = unmapped.get(name, 0) + 1
                    continue
                checked += 1
                flow, ok_status, max_est = AUDIT_RULES[k]
                early = datetime.fromtimestamp(t.timestamp() - GRACE, timezone.utc)
                status, status_early = _at(st, t), _at(st, early)
                why = []
                if status not in ok_status and status_early not in ok_status:
                    why.append(f"sent while {status}")
                est = _at(ec, t)
                try: est = float(est) if est not in (None, "") else 0.0
                except ValueError: est = 0.0
                if max_est is not None and est > max_est:
                    why.append("recipient had already made estimates")
                prior = [x for x in st if _ts(x["timestamp"]) < early]
                was_paid = any(x["value"] == "paid_customer" for x in prior)
                if k in PRE_PAID and was_paid:
                    why.append("recipient has been a paying customer")
                if k == "WNP" and was_paid:
                    # paid before, left paid, now back: a returning customer, not a new payer
                    seq = [x["value"] for x in reversed(prior)]  # oldest first
                    if any(v != "paid_customer" for v in seq[seq.index("paid_customer") + 1:]):
                        why.append("returning customer got the new-payer welcome")
                if k in WINDOWED:
                    m = t.astimezone(mel)
                    if m.weekday() > 4 or not 7 <= m.hour < 18:
                        why.append("sent outside Mon-Fri 07:00-18:00")
                for w in why:
                    key = f"{flow} ({name.split(' (')[0]}): {w}"
                    found.setdefault(key, []).append(r["properties"].get("email"))
    for key, who in found.items():  # detail stays in the container log, not Slack
        print("send audit:", key, "->", ", ".join(sorted(set(filter(None, who)))), flush=True)
    return {"checked": checked, "misfires": {k: len(v) for k, v in found.items()}, "unmapped": unmapped}


def misfire_lines(sa):
    """One plain line per flow from audit_sends() output."""
    problems = []
    flows = {}
    plain = {"recipient had already made estimates": "to people who had made estimates",
             "recipient has been a paying customer": "to past or current paying customers",
             "returning customer got the new-payer welcome": "to returning customers",
             "sent outside Mon-Fri 07:00-18:00": "outside the send window",
             "sent while trial_expired": "after the trial had ended", "sent while churned": "after they churned",
             "sent while active_trial": "during a trial", "sent while paid_customer": "to paying customers"}
    for k, n in (sa.get("misfires") or {}).items():  # key: "Flow (Email name): reason"
        flow, rest = k.split(" (", 1)
        email, reason = rest.split("): ", 1)
        num = email.replace("Sammy - ", "")[:2]
        r = flows.setdefault(flow, {}).setdefault(plain.get(reason, reason), [0, set()])
        r[0] += n
        if num.isdigit(): r[1].add(num)
    for flow, rs in sorted(flows.items(), key=lambda kv: -sum(v[0] for v in kv[1].values())):
        parts = [f"{n} {reason}" + (f" (email {'+'.join(sorted(nums))})" if nums else "")
                 for reason, (n, nums) in sorted(rs.items(), key=lambda kv: -kv[1][0])]
        problems.append(f"Email misfires, {flow}: {'; '.join(parts)}. Names in the cron log.")
    return problems


def compose(c, prev):
    """Short by design: what changed this week, then only what is new or worsening.
    A watch that is quiet says nothing."""
    day = datetime.now(timezone.utc).strftime("%a %-d %b")
    pct = lambda a, b: f"{round(100 * a / b)}%" if b else "n/a"
    sa = c.get("send_audit") or {}

    gq = f"{c['gone_quiet']} dormant paying customers (${c['gone_quiet_mrr']:,}/mo)"
    if prev and "gone_quiet" in prev:
        d = c["gone_quiet"] - prev["gone_quiet"]
        gq += f", {'+' if d > 0 else ''}{d} vs last week" if d else ", unchanged vs last week"
    lines = [f"*Sammy weekly - {day}*", "",
             f"*Customers:* {c['paid']} paying, ${c['mrr']:,} MRR. +{c['new_payers']} new, -{c['churned']} churned this week.",
             f"*Trials:* {c['trials']} started this week. {c['prev_activated']} of last week's {c['prev_trials']} "
             f"({pct(c['prev_activated'], c['prev_trials'])}) made a first quote.",
             f"*Gone Quiet:* {gq}.",
             f"*First Six Weeks:* {c['fsw_hit3']} of the {c['fsw']} customers in their first six weeks have 3+ quotes since paying."]
    if "checked" in sa:
        n = sum(sa["misfires"].values())
        lines.append(f"*Email health:* {sa['checked']} automated sends checked, "
                     + ("0 misfires." if not n else f"{n} misfires (below)."))

    problems = []
    if sa.get("error"): problems.append(f"Email send audit did not run ({sa['error']}). Misfires were not checked this week.")
    problems += misfire_lines(sa)
    if sa.get("unmapped"): problems.append(f"New automated email with no audit rule: {', '.join(sorted(sa['unmapped']))}")
    if c["unknown_plans"]: problems.append(f"Unrecognised pricing plan {c['unknown_plans']}: MRR and deal values need the new mapping")
    if c["unknown_promos"]: problems.append(f"Unrecognised promo code {c['unknown_promos']}: MRR will drift until it is mapped")
    if c["paid_undated"]: problems.append(f"{c['paid_undated']} paying customers have no conversion date (stamper gap)")
    if c["paid_future"]: problems.append(f"{c['paid_future']} conversions dated in the future (merge or clock artefact)")
    if c["new_no_channel"]: problems.append(f"{c['new_no_channel']} contacts created this week have no source channel")
    if c["webhook_sysinbox"]: problems.append(f"{c['webhook_sysinbox']} system-inbox contacts leaked in this week")
    if c["aircall_new"]: problems.append(f"{c['aircall_new']} bare Aircall contact{'s' if c['aircall_new'] != 1 else ''} created this week: check whether Aircall auto-create is back on")
    if c["new_dupe_won"]: problems.append(f"{c['new_dupe_won']} customers got a second Closed Won deal this week")
    if c["stale_meetings"]: problems.append(f"{c['stale_meetings']} demo{'s' if c['stale_meetings'] != 1 else ''} held this week have no outcome marked")
    if not c["dash_ok"]: problems.append("attribution.hirecharm.com is not returning all report sections")

    lines += ["", "*Needs attention*"]
    lines += [f"{i + 1}. {p}" for i, p in enumerate(problems)] if problems else ["Nothing new this week."]
    return "\n".join(lines)


def post(text):
    r = urllib.request.Request(WEBHOOK, data=json.dumps({"text": text}).encode(),
                               headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(r, timeout=30).read().decode()


def maybe_weekly_sweep():
    if not WEBHOOK:
        print("sweep: SLACK_WEBHOOK_URL not set, skipping", flush=True)
        return
    now = datetime.now(timezone.utc)
    # ONLY post on Mondays, at/after 15:00 UTC (8am US Pacific). Hard weekday gate
    # so a mid-week redeploy can never trigger a post: the marker lives in /tmp and
    # is wiped on redeploy, which previously made the once-per-week check re-fire on
    # every ship. The weekday gate makes that impossible outside Monday.
    if now.weekday() != 0 or now.hour < 15:
        return
    week = now.strftime("%G-W%V")
    done = open(MARKER).read().strip() if os.path.exists(MARKER) else ""
    if done == week or os.environ.get("SWEEP_SKIP_WEEK") == week:
        return  # already posted this week (marker, or env guard for a Monday-afternoon redeploy)
    try:
        c = collect()
        prev = json.load(open(BASELINE)) if os.path.exists(BASELINE) else None
        msg = compose(c, prev)
        resp = post(msg)
        print("sweep posted:", resp, flush=True)
        json.dump(c, open(BASELINE, "w"))
        open(MARKER, "w").write(week)
    except Exception as e:
        # Do not burn the week's marker on a failure: the next hourly tick
        # retries. The failure line posts once per week, not every hour.
        if (open(FAILED_MARKER).read().strip() if os.path.exists(FAILED_MARKER) else "") != week:
            try:
                post(f"Sammy Weekly Sweep FAILED to run: {type(e).__name__}: {e}. "
                     f"Retrying hourly; a full report follows if a retry succeeds.")
                open(FAILED_MARKER, "w").write(week)
            except Exception:
                pass
        print("sweep error:", e, flush=True)


DAILY_MARKER = "/tmp/daily_audit_day.txt"


def maybe_daily_audit():
    """Daily email misfire check. Runs once per UTC day at/after 21:00 UTC (8am
    Melbourne in summer) over the last 24 hours, and posts ONLY when something is
    wrong. A clean day says nothing. Skipped on a day the weekly sweep posted,
    since that post already carries the same check over 7 days."""
    if not WEBHOOK:
        return
    now = datetime.now(timezone.utc)
    if now.hour < 21:
        return
    day = now.strftime("%Y-%m-%d")
    if (open(DAILY_MARKER).read().strip() if os.path.exists(DAILY_MARKER) else "") == day:
        return
    if (open(MARKER).read().strip() if os.path.exists(MARKER) else "") == now.strftime("%G-W%V") and now.weekday() == 0:
        open(DAILY_MARKER, "w").write(day)
        return
    try:
        sa = audit_sends(days=1)
    except Exception as e:
        sa = {"error": f"{type(e).__name__}: {e}"}
    lines = []
    if sa.get("error"):
        lines.append(f"The daily email check did not run ({sa['error']}). Emails were not checked today.")
    lines += misfire_lines(sa)
    if sa.get("unmapped"):
        lines.append(f"New automated email with no audit rule: {', '.join(sorted(sa['unmapped']))}")
    if lines:
        try:
            post("*Sammy email alert*\n" + "\n".join(f"- {l}" for l in lines))
            print("daily audit posted:", len(lines), "issue(s)", flush=True)
        except Exception as e:
            print("daily audit post failed:", e, flush=True)
            return
    else:
        print(f"daily audit: {sa.get('checked', 0)} sends checked, clean", flush=True)
    open(DAILY_MARKER, "w").write(day)


if __name__ == "__main__":
    maybe_weekly_sweep()
    maybe_daily_audit()
