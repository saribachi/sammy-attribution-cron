"""Weekly Sammy sweep: deterministic health report posted to Slack.

Runs on the same always-on container as the hourly classifier. Fires once per
ISO week, at/after Monday 15:00 UTC (8am US Pacific). If the container was down
at fire time, it catches up at the next hourly tick, any day of that week.
A crashed sweep still posts a short failure line to Slack: silence means the
whole container is down, which the hourly attribution healing would also show.

Baseline for week-over-week deltas persists in /tmp; a redeploy resets it and
the report says so instead of guessing.
"""
import json, os, time, urllib.error, urllib.request
from datetime import datetime, timezone

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


def collect():
    c = {}
    c["contacts"] = total([])
    c["blank_channel"] = total([{"propertyName": "original_source_channel", "operator": "NOT_HAS_PROPERTY"}])

    week_ms = str(int((time.time() - 7 * 86400) * 1000))
    d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/search", {
        "filterGroups": [{"filters": [
            {"propertyName": "createdate", "operator": "GTE", "value": week_ms},
            {"propertyName": "hs_object_source_detail_1", "operator": "CONTAINS_TOKEN", "value": "Outbound"}]}],
        "properties": ["email", "firstname"], "limit": 100})
    c["webhook_new"] = d["total"]
    c["webhook_nameless"] = sum(1 for r in d["results"] if not r["properties"].get("firstname"))
    c["webhook_sysinbox"] = sum(1 for r in d["results"] if any(
        (r["properties"].get("email") or "").startswith(p)
        for p in ("support@", "noreply@", "no-reply@", "notifications@")))

    c["deals"] = deal_total([])
    c["deals_no_source"] = deal_total([{"propertyName": "deal_source", "operator": "NOT_HAS_PROPERTY"}])
    c["deals_no_amount"] = deal_total([{"propertyName": "amount", "operator": "NOT_HAS_PROPERTY"}])

    paid, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [{"propertyName": "user_status", "operator": "EQ", "value": "paid_customer"}]}],
             "properties": ["sammy_pricing_plan", "sammy_promo_code"], "limit": 200}
        if after: b["after"] = after
        d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/search", b)
        paid += d["results"]
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: break
        time.sleep(0.2)
    c["paid"] = len(paid)
    c["unknown_plans"] = sorted({p["properties"].get("sammy_pricing_plan") for p in paid} - PLAN_VALUES - {None})
    c["unknown_promos"] = sorted({p["properties"].get("sammy_promo_code") for p in paid} - PROMO_VALUES - {None})

    c["clay_campaign"] = total([{"propertyName": "cold_email_reply_campaign", "operator": "HAS_PROPERTY"}])
    c["paid_dated"] = total([{"propertyName": "user_status", "operator": "EQ", "value": "paid_customer"},
                             {"propertyName": "became_paid_customer_date", "operator": "HAS_PROPERTY"}])
    c["closed_on_call_total"] = total([{"propertyName": "closed_on_call", "operator": "EQ", "value": "true"}])
    # Aircall auto-create watch: bare Aircall contacts created this week should be
    # 0 now that the setting is off (Aug 12). Non-zero = it turned back on.
    # Fixed cutoff = when Chris turned Aircall auto-create OFF (Aug 12 2026 18:00 UTC).
    # Counts bare Aircall contacts created SINCE the fix, so pre-change ones don't
    # keep false-alarming as the 7-day window rolls. 0 = setting still off.
    c["aircall_new"] = total([
        {"propertyName": "hs_object_source_detail_1", "operator": "CONTAINS_TOKEN", "value": "Aircall"},
        {"propertyName": "email", "operator": "NOT_HAS_PROPERTY"},
        {"propertyName": "createdate", "operator": "GTE", "value": "1786557600000"}])  # 2026-08-12T18:00:00Z

    # duplicate won-deal watch: customers with 2+ Closed Won deals in the SALES
    # pipeline. Should be 0 now the blunt 'move to won' workflow is retired and the
    # reconciler dedups on existence. >0 = a regression (something double-won a customer).
    won_deals, dafter = [], None
    while True:
        wb = {"filterGroups": [{"filters": [
            {"propertyName": "pipeline", "operator": "EQ", "value": "default"},
            {"propertyName": "dealstage", "operator": "EQ", "value": "decisionmakerboughtin"}]}],
            "properties": ["dealname"], "limit": 100}
        if dafter: wb["after"] = dafter
        dd = req("POST", "https://api.hubapi.com/crm/v3/objects/deals/search", wb)
        won_deals += dd.get("results", [])
        dafter = dd.get("paging", {}).get("next", {}).get("after")
        if not dafter: break
        time.sleep(0.2)
    wcount = {}
    wids = [d["id"] for d in won_deals]
    for i in range(0, len(wids), 100):
        aa = req("POST", "https://api.hubapi.com/crm/v4/associations/deals/contacts/batch/read",
                 {"inputs": [{"id": x} for x in wids[i:i+100]]})
        for r in aa.get("results", []):
            to = r.get("to") or []
            if to:
                k = str(to[0]["toObjectId"]); wcount[k] = wcount.get(k, 0) + 1
        time.sleep(0.2)
    c["dupe_won"] = sum(1 for n in wcount.values() if n >= 2)
    # merge-integrity spot check: paid customers whose became_paid date sits in
    # the current week but whose latest status write was a merge (would signal
    # the guard is not keeping up)
    import time as _t
    wk_start = str(int((_t.time() - (_t.time() % 604800)) * 1000))
    c["paid_future"] = total([{"propertyName": "became_paid_customer_date", "operator": "GT", "value": str(int(_t.time() * 1000))}])

    # missing-data visibility: contacts created last 7d without a phone,
    # grouped by creation source, so incomplete pipes stay visible (Chris, Aug 3)
    d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/search", {
        "filterGroups": [{"filters": [
            {"propertyName": "createdate", "operator": "GTE", "value": week_ms},
            {"propertyName": "hs_calculated_phone_number", "operator": "NOT_HAS_PROPERTY"},
            {"propertyName": "phone", "operator": "NOT_HAS_PROPERTY"}]}],
        "properties": ["hs_object_source_label", "hs_object_source_detail_1", "firstname"], "limit": 200})
    srcs = {}
    for r in d["results"]:
        p = r["properties"]
        key = (p.get("hs_object_source_detail_1") or p.get("hs_object_source_label") or "unknown source")
        e = srcs.setdefault(key, {"n": 0, "nameless": 0})
        e["n"] += 1
        if not p.get("firstname"): e["nameless"] += 1
    c["phoneless_new"] = d["total"]
    c["phoneless_by_source"] = sorted(srcs.items(), key=lambda kv: -kv[1]["n"])

    # stale meeting outcomes: meetings that already happened but still say
    # Scheduled or have no outcome (Chris's rule: impossible state, Aug 4)
    now_ms = str(int(time.time() * 1000))
    month_ago = str(int((time.time() - 30 * 86400) * 1000))
    stale = 0
    for f in ([{"propertyName": "hs_meeting_outcome", "operator": "EQ", "value": "SCHEDULED"}],
              [{"propertyName": "hs_meeting_outcome", "operator": "NOT_HAS_PROPERTY"}]):
        d = req("POST", "https://api.hubapi.com/crm/v3/objects/meetings/search", {
            "filterGroups": [{"filters": [
                {"propertyName": "hs_timestamp", "operator": "BETWEEN", "value": month_ago, "highValue": now_ms}] + f}],
            "limit": 1})
        stale += d["total"]
    c["stale_meetings"] = stale

    # MRR computed directly (same formula as the dashboard and deal stamper) so
    # the sweep never depends on the dashboard cache being warm
    mrr = 0
    for p in paid:
        amt = PLAN_AMOUNT.get(p["properties"].get("sammy_pricing_plan"), 59)
        amt -= PROMO_MONTHLY_DISCOUNT.get(p["properties"].get("sammy_promo_code"), 0)
        mrr += max(amt, 0)
    c["mrr"] = mrr

    # dashboard health check: non-fatal, retried, cold caches can take a minute
    # send audit: non-fatal, a failure here must not cost the rest of the report
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


def compose(c, prev):
    day = datetime.now(timezone.utc).strftime("%A %B %-d")
    problems = []
    if c["blank_channel"]: problems.append(f"{c['blank_channel']} contacts have no source channel (expected 0)")
    if c["paid_dated"] < c["paid"]: problems.append(f"{c['paid'] - c['paid_dated']} paying customers missing a conversion date (stamper gap)")
    if c.get("paid_future"): problems.append(f"{c['paid_future']} conversions dated in the future - merge or clock artifact, investigate")
    if c["webhook_sysinbox"]: problems.append(f"{c['webhook_sysinbox']} system-inbox contacts leaked in this week")
    if c["unknown_plans"]: problems.append(f"UNRECOGNIZED PRICING PLAN(S): {c['unknown_plans']} - dashboard and deal pricing maps need updating")
    if c["unknown_promos"]: problems.append(f"UNRECOGNIZED PROMO CODE(S): {c['unknown_promos']} - discount mapping needed or MRR will drift")
    if not c["dash_ok"]: problems.append("attribution.hirecharm.com is not returning all report sections")
    if c["deals_no_amount"] > 300: problems.append(f"blank deal amounts grew to {c['deals_no_amount']} (baseline ~248)")
    if c.get("stale_meetings"): problems.append(f"{c['stale_meetings']} past meetings (30d) still say Scheduled or have no outcome - outcomes not being updated, ask the rep to mark them")
    if c.get("aircall_new"): problems.append(f"{c['aircall_new']} new bare Aircall contacts created this week - the Aircall auto-create-contact setting has turned back on, switch it off again")
    sa = c.get("send_audit") or {}
    if sa.get("error"): problems.append(f"send audit did not run ({sa['error']}) - email misfires were NOT checked this week")
    for k, n in sorted((sa.get("misfires") or {}).items()):
        problems.append(f"EMAIL MISFIRE - {k}: {n} contact{'s' if n != 1 else ''} (names in the cron log)")
    if sa.get("unmapped"):
        problems.append(f"send audit has no rule for: {', '.join(sorted(sa['unmapped']))} - add one so these are checked")
    if c.get("dupe_won"): problems.append(f"{c['dupe_won']} customers have 2+ Closed Won deals in the sales pipeline - duplicate-win dedup regressed, investigate")

    lines = [f"*Sammy Weekly Sweep: {day}*", ""]
    if prev:
        dc, dm = c["paid"] - prev.get("paid", 0), c["mrr"] - prev.get("mrr", 0)
        lines.append(f"*{c['paid']} paying customers / ${c['mrr']:,} MRR* "
                     f"({'+' if dc >= 0 else ''}{dc} customers, {'+' if dm >= 0 else '-'}${abs(dm):,} vs last week)")
    else:
        lines.append(f"*{c['paid']} paying customers / ${c['mrr']:,} MRR* (baseline reset after redeploy, deltas resume next week)")
    lines += ["",
              "*Status*",
              f"- Attribution coverage: {c['contacts'] - c['blank_channel']:,} of {c['contacts']:,} contacts have a source ({'100%' if not c['blank_channel'] else 'GAPS'})",
              f"- New webhook contacts this week: {c['webhook_new']} ({c['webhook_nameless']} without names, {c['webhook_sysinbox']} system inboxes)",
              f"- Deals: {c['deals'] - c['deals_no_source']} of {c['deals']} have a source; {c['deals_no_amount']} blank amounts (free/no-plan contacts)",
              f"- Campaign visibility: {c['clay_campaign']} contacts carry a reply campaign",
              f"- Sales tracking: {c['paid_dated']} of {c['paid']} paying customers have an exact conversion date; {c['closed_on_call_total']} all-time same-day demo closes",
              ]
    # Regression watches: every check still runs, but a permanently-green watch
    # does not earn its own line every week. Detail appears only when one fires;
    # the specifics then land in *Needs attention* above.
    if "checked" in sa:
        lines.append(f"- Email send audit: {sa['checked']} automated sends in the last 7 days checked against each "
                     f"recipient's status at send time, {sum(sa['misfires'].values())} misfires")
    watches = [("pricing", not (c["unknown_plans"] or c["unknown_promos"])),
               ("future-dated conversions", not c.get("paid_future")),
               ("Aircall auto-create", not c.get("aircall_new")),
               ("duplicate wins", not c.get("dupe_won"))]
    failing = [n for n, ok in watches if not ok]
    if failing:
        lines.append(f"- Regression watches: {', '.join(failing)} SEE PROBLEMS "
                     f"({len(watches) - len(failing)} of {len(watches)} green)")
    else:
        lines.append(f"- Regression watches: all green "
                     f"({', '.join(n for n, _ in watches)})")
    if c.get("phoneless_new"):
        lines += ["", f"*Contacts created this week with NO phone: {c['phoneless_new']}*"]
        for src, e in c["phoneless_by_source"][:6]:
            lines.append(f"- {src}: {e['n']}" + (f" ({e['nameless']} also nameless)" if e["nameless"] else ""))
        lines.append("These get no call task until a phone lands. Tighten the source or enrich.")

    lines += ["", "*Needs attention*"]
    if problems:
        lines += [f"{i+1}. {p}" for i, p in enumerate(problems)]
    else:
        lines.append("Nothing. All checks green.")
    if os.path.exists("NEW_THIS_WEEK.md"):
        items = [l.strip() for l in open("NEW_THIS_WEEK.md") if l.strip().startswith("-")]
        if items:
            lines += ["", "*New since last sweep*"] + items
    if os.path.exists("OUTSTANDING.md"):
        items = [l.strip() for l in open("OUTSTANDING.md") if l.strip().startswith("-")]
        if items:
            lines += ["", "*Outstanding with the team*"] + [l for l in items]
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


if __name__ == "__main__":
    maybe_weekly_sweep()
