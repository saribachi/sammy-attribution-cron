"""Churn check: flag HubSpot records whose user_status = churned looks wrong.

Why (Ops ticket 340512582363, Yuri on Krishna's go, 14 Oct 2026): when a tradie
deletes their Sammy account and signs up again, or signs up with a second email,
the old record keeps the dead account's sammy_user_id and stays "churned". The
sync writes by sammy_user_id, so the old record is never touched again. Until
Linear SAM-258 ships, "churned" is a lead to check, not a fact.

What it does, hourly, reading HubSpot only (it cannot see the app database):
  A churned record is flagged when
    paying_elsewhere   another record that shares its phone, business email
                       domain, or full name is a paying customer
    live_elsewhere     ...that record is on a trial, free, or onboarding
    active_after_churn it quoted or paid after the day it went churned
  The live record on the other side gets churn_check_quoted_elsewhere = the most
  estimates on any churned record linked to it, so trial "make your first quote"
  emails can hold back from someone who already quoted on their first account.

What it never does: write user_status (the app is the only writer), or flag a
record that Sammy's team has marked checked (churn_check_checked_on on or after
the day it went churned).

Properties it owns (group churn_check): churn_check_flag, churn_check_detail,
churn_check_linked_record, churn_check_flagged_at, churn_check_quoted_elsewhere,
churn_check_ever_flagged. The last one is STICKY: set true the first time a record
is flagged and never cleared, so when Sammy hand-fixes a record from churned to
paid, "Welcome, new payer" can hold back (19 Oct: Joshua Marcinkewycz got the
welcome twice, once per record, after a hand-fix).
Sammy's team owns churn_check_checked_on.

  python3 churn_check.py                dry run, prints every change
  python3 churn_check.py --commit       write
  python3 churn_check.py --commit --only <contactId>[,<id>]   write just these
"""
import json, os, re, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
MEL = ZoneInfo("Australia/Melbourne")
LIVE = {"paid_customer": "paying", "carded_trial": "trial", "active_trial": "trial",
        "free": "free", "incomplete_onboarding": "onboarding"}
FREE_MAIL = {"gmail.com", "googlemail.com", "hotmail.com", "hotmail.com.au", "outlook.com", "outlook.com.au",
             "live.com", "live.com.au", "msn.com", "yahoo.com", "yahoo.com.au", "ymail.com", "y7mail.com",
             "icloud.com", "me.com", "mac.com", "privaterelay.appleid.com", "bigpond.com", "bigpond.net.au",
             "bigpond.com.au", "optusnet.com.au", "iinet.net.au", "westnet.com.au", "tpg.com.au",
             "internode.on.net", "dodo.com.au", "aol.com", "protonmail.com", "proton.me", "adam.com.au",
             "people.net.au", "netspace.net.au", "ozemail.com.au", "outlook.co.nz", "xtra.co.nz", "gmx.com",
             "withsammy.ai", "hirecharm.com"}
FLAG_LABEL = {"paying_elsewhere": "Looks wrong: the business pays on another record",
              "live_elsewhere": "Looks wrong: they have a live account on another record",
              "active_after_churn": "Looks wrong: they used Sammy after going churned"}
P = ["email", "firstname", "lastname", "company", "phone", "mobilephone", "user_status", "estimate_count",
     "last_estimate_date", "last_app_login_date", "became_paid_customer_date",
     "churn_check_flag", "churn_check_detail", "churn_check_linked_record", "churn_check_flagged_at",
     "churn_check_quoted_elsewhere", "churn_check_checked_on", "churn_check_ever_flagged"]


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


def when(v):
    if not v: return None
    if v.isdigit(): return datetime.fromtimestamp(int(v) / 1000, timezone.utc)
    if "T" in v: return datetime.fromisoformat(v.replace("Z", "+00:00"))
    return datetime.fromisoformat(v).replace(tzinfo=MEL)  # date-only fields are Melbourne days


def phone_key(p):
    d = re.sub(r"\D", "", (p.get("phone") or "") + "")
    m = re.sub(r"\D", "", p.get("mobilephone") or "")
    return {x[-9:] for x in (d, m) if len(x) >= 9}


def domain(p):
    e = (p.get("email") or "").lower()
    d = e.split("@")[-1] if "@" in e else ""
    return d if d and d not in FREE_MAIL else None


def name_key(p):
    f, l = (p.get("firstname") or "").strip().lower(), (p.get("lastname") or "").strip().lower()
    return f"{f} {l}" if f and l else None


def num(v):
    try: return float(v)
    except (TypeError, ValueError): return 0.0


def run(commit=False, only=None):
    churned = search([{"propertyName": "user_status", "operator": "EQ", "value": "churned"}], P)
    live = search([{"propertyName": "user_status", "operator": "IN", "values": list(LIVE)}], P)
    # records we flagged before, which may need clearing
    try:
        prior = search([{"propertyName": "churn_check_flag", "operator": "HAS_PROPERTY"}], P)
        prior += search([{"propertyName": "churn_check_quoted_elsewhere", "operator": "GT", "value": "0"}], P)
    except RuntimeError as e:  # properties not created yet
        print("churn check: no prior flags readable:", str(e)[:120], flush=True)
        prior = []
    by_id = {c["id"]: c for c in churned + live + prior}

    # when each churned record went churned: latest transition into churned
    went = {}
    ids = [c["id"] for c in churned]
    for i in range(0, len(ids), 50):
        d = req("POST", "/crm/v3/objects/contacts/batch/read",
                {"inputs": [{"id": x} for x in ids[i:i + 50]], "properties": ["user_status"],
                 "propertiesWithHistory": ["user_status"]})
        for r in d["results"]:
            h = r.get("propertiesWithHistory", {}).get("user_status", [])  # newest first
            t = None
            for e in h:
                if e["value"] == "churned": t = when(e["timestamp"])
                else: break
            went[r["id"]] = t

    idx = {"phone": {}, "domain": {}, "name": {}}
    for c in live:
        p = c["properties"]
        for k in phone_key(p): idx["phone"].setdefault(k, []).append(c["id"])
        if domain(p): idx["domain"].setdefault(domain(p), []).append(c["id"])
        if name_key(p): idx["name"].setdefault(name_key(p), []).append(c["id"])

    want = {}  # contact id -> desired churn_check_* values
    quoted_elsewhere = {}
    for c in churned:
        p, cid = c["properties"], c["id"]
        t = went.get(cid)
        checked = when(p.get("churn_check_checked_on"))
        if checked and t and checked.date() >= t.astimezone(MEL).date():
            want[cid] = {"churn_check_flag": "", "churn_check_detail": f"Checked by Sammy on {p['churn_check_checked_on'][:10]}",
                         "churn_check_linked_record": ""}
            continue
        links = {}
        for k in phone_key(p):
            for o in idx["phone"].get(k, []): links.setdefault(o, set()).add("phone")
        if domain(p):
            for o in idx["domain"].get(domain(p), []): links.setdefault(o, set()).add("email domain")
        if name_key(p):
            for o in idx["name"].get(name_key(p), []): links.setdefault(o, set()).add("name")
        links.pop(cid, None)
        flag, detail, linked = "", "", []
        if links:
            order = sorted(links, key=lambda o: (by_id[o]["properties"]["user_status"] != "paid_customer", o))
            linked = order[:3]
            parts = []
            for o in linked:
                q = by_id[o]["properties"]
                who = " ".join(filter(None, [(q.get("firstname") or "").strip(), (q.get("lastname") or "").strip()])) or q.get("email")
                parts.append(f"same {' and '.join(sorted(links[o]))} as {who} ({o}), {LIVE[q['user_status']]}")
            flag = "paying_elsewhere" if by_id[linked[0]]["properties"]["user_status"] == "paid_customer" else "live_elsewhere"
            detail = "; ".join(parts)
            for o in links:
                quoted_elsewhere[o] = max(quoted_elsewhere.get(o, 0), num(p.get("estimate_count")))
        elif t:
            day = t.astimezone(MEL).date()
            acts = []
            # A login alone does not count: if the sync wrote a login, it is still
            # writing to this record, so the record is not on a dead account id.
            for f, label in [("last_estimate_date", "quoted"), ("became_paid_customer_date", "paid")]:
                w = when(p.get(f))
                if w and w.astimezone(MEL).date() > day:
                    acts.append(f"{label} {w.astimezone(MEL).date()}")
            if acts:
                flag, detail = "active_after_churn", f"Went churned {day}, then {', '.join(acts)}"
        want[cid] = {"churn_check_flag": flag, "churn_check_detail": detail if flag else "",
                     "churn_check_linked_record": ",".join(linked) if flag else ""}
        if flag: want[cid]["churn_check_ever_flagged"] = "true"  # sticky, never cleared

    # records no longer churned lose any flag
    for c in prior:
        if c["id"] not in want and c["properties"].get("churn_check_flag"):
            want[c["id"]] = {"churn_check_flag": "", "churn_check_detail": "", "churn_check_linked_record": ""}
    # live side: estimates on linked churned records
    for c in live + prior:
        q = int(quoted_elsewhere.get(c["id"], 0))
        want.setdefault(c["id"], {})["churn_check_quoted_elsewhere"] = str(q) if q else ""

    now_ms = str(int(time.time() * 1000))
    updates, changes = [], []
    for cid, props in want.items():
        cur = by_id[cid]["properties"]
        diff = {k: v for k, v in props.items() if (cur.get(k) or "") != v}
        if not diff: continue
        if diff.get("churn_check_flag"): diff["churn_check_flagged_at"] = now_ms if not cur.get("churn_check_flag") else cur["churn_check_flagged_at"]
        if "churn_check_flag" in diff and not diff["churn_check_flag"]: diff["churn_check_flagged_at"] = ""
        diff = {k: v for k, v in diff.items() if (cur.get(k) or "") != v}
        if only and cid not in only: continue
        updates.append({"id": cid, "properties": diff})
        changes.append((cid, cur.get("email"), cur.get("user_status"), diff))

    if commit:
        for i in range(0, len(updates), 100):
            req("POST", "/crm/v3/objects/contacts/batch/update", {"inputs": updates[i:i + 100]})
    flags = {}
    for cid, props in want.items():
        f = props.get("churn_check_flag")
        if f: flags[f] = flags.get(f, 0) + 1
    print(f"churn check: {len(churned)} churned records, {sum(flags.values())} look wrong {flags}, "
          f"{sum(1 for v in quoted_elsewhere.values() if v)} live records quoted on another record, "
          f"{len(updates)} updates" + ("" if commit else " [dry-run]"), flush=True)
    return changes


if __name__ == "__main__":
    only = None
    if "--only" in sys.argv: only = set(sys.argv[sys.argv.index("--only") + 1].split(","))
    for c in run(commit="--commit" in sys.argv, only=only):
        print(" ", c[0], c[1], c[2], json.dumps(c[3]))
