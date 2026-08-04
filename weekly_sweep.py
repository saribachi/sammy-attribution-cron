"""Weekly Sammy sweep: deterministic health report posted to Slack.

Runs on the same always-on container as the hourly classifier. Fires once per
ISO week, at/after Monday 15:00 UTC (8am US Pacific). If the container was down
at fire time, it catches up at the next hourly tick, any day of that week.
A crashed sweep still posts a short failure line to Slack: silence means the
whole container is down, which the hourly attribution healing would also show.

Baseline for week-over-week deltas persists in /tmp; a redeploy resets it and
the report says so instead of guessing.
"""
import json, os, time, urllib.request
from datetime import datetime, timezone

TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
MARKER = "/tmp/sweep_week.txt"
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
    resp = urllib.request.urlopen(r, timeout=30)
    raw = resp.read()
    return json.loads(raw) if raw else {}


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
    c["dash_ok"] = False
    for _ in range(2):
        try:
            dash = json.load(urllib.request.urlopen("https://attribution.hirecharm.com/api/data", timeout=120))
            c["dash_ok"] = bool(dash.get("acqRows")) and bool(dash.get("campaignRows")) and bool(dash.get("funnelRows"))
            break
        except Exception:
            time.sleep(10)
    return c


def compose(c, prev):
    day = datetime.now(timezone.utc).strftime("%A %B %-d")
    problems = []
    if c["blank_channel"]: problems.append(f"{c['blank_channel']} contacts have no source channel (expected 0)")
    if c["webhook_sysinbox"]: problems.append(f"{c['webhook_sysinbox']} system-inbox contacts leaked in this week")
    if c["unknown_plans"]: problems.append(f"UNRECOGNIZED PRICING PLAN(S): {c['unknown_plans']} - dashboard and deal pricing maps need updating")
    if c["unknown_promos"]: problems.append(f"UNRECOGNIZED PROMO CODE(S): {c['unknown_promos']} - discount mapping needed or MRR will drift")
    if not c["dash_ok"]: problems.append("attribution.hirecharm.com is not returning all report sections")
    if c["deals_no_amount"] > 300: problems.append(f"blank deal amounts grew to {c['deals_no_amount']} (baseline ~248)")
    if c.get("stale_meetings"): problems.append(f"{c['stale_meetings']} past meetings (30d) still say Scheduled or have no outcome - outcomes not being updated, ask the rep to mark them")

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
              f"- Pricing integrity: all plans and promo codes recognized" if not (c["unknown_plans"] or c["unknown_promos"]) else "- Pricing integrity: SEE PROBLEMS",
              ]
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
    week = now.strftime("%G-W%V")
    done = open(MARKER).read().strip() if os.path.exists(MARKER) else ""
    if done == week or os.environ.get("SWEEP_SKIP_WEEK") == week:
        return  # already posted this week (marker, or env guard after a mid-week redeploy)
    if now.weekday() == 0 and now.hour < 15:
        return  # Monday before 8am Pacific: wait
    try:
        c = collect()
        prev = json.load(open(BASELINE)) if os.path.exists(BASELINE) else None
        msg = compose(c, prev)
        resp = post(msg)
        print("sweep posted:", resp, flush=True)
        json.dump(c, open(BASELINE, "w"))
        open(MARKER, "w").write(week)
    except Exception as e:
        try:
            post(f"Sammy Weekly Sweep FAILED to run: {type(e).__name__}: {e}. "
                 f"Numbers were not checked this week - flag it.")
            open(MARKER, "w").write(week)
        except Exception:
            pass
        print("sweep error:", e, flush=True)


if __name__ == "__main__":
    maybe_weekly_sweep()
