#!/usr/bin/env python3
"""
Governed attribution classifier for Sammy (portal 244038625).

ORIGIN-BASED: every contact is attributed by how it actually entered the pipeline,
so we do not leave large unassigned buckets. Writes original_source_channel directly,
only when it is currently BLANK (only-if-blank = write-once, enforced in code). Safe to
re-run; it never overwrites an existing value.

RULES (priority order):
  1. UTM present (sammy_utm_* / first_utm)              -> derive channel from UTM
  2. Native paid signal (hs_analytics_source PAID_*)    -> paid_ads
  3. Record origin (how the record was created):
       - Aircall-created                                -> cold_call
       - CSV import / IMPORT label:
             dialed (has Aircall activity)              -> cold_call   (CSV-then-dial)
             not dialed                                 -> cold_email  (imported cold list)
       - Clay / Instantly / Outbound Sync / EmailBison / Sammy Setup -> cold_email
       - App signup (Sammy Accounts Sync / Database Sync)-> organic_inbound
       - Manual (CRM_UI / EXTENSION)                    -> user_generated
       - Inbound (MEETINGS / FORM)                      -> organic_inbound
  4. Native organic signal (ORGANIC_SEARCH/DIRECT/REFERRALS) -> organic_inbound
  5. Has Aircall activity, origin unknown              -> cold_call
  6. Otherwise                                         -> leave blank (truly unknown)

USAGE
  export HUBSPOT_TOKEN=pat-na2-e5d783f4-...   # attribution writer app (30858065)
  python3 governed_classifier.py             # DRY RUN
  python3 governed_classifier.py --commit    # write original_source_channel
"""
import os, sys, json, time, urllib.request, urllib.error
from collections import Counter

TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
COMMIT = "--commit" in sys.argv
PAID = {"PAID_SOCIAL", "PAID_SEARCH"}
ORGANIC = {"ORGANIC_SEARCH", "DIRECT_TRAFFIC", "REFERRALS", "SOCIAL_MEDIA"}
import re as _re
PAID_URL_RX = _re.compile(r"fbclid|gclid=|ttclid|utm_source=(facebook|meta|instagram|fb)|utm_medium=(cpc|ppc|paid)", _re.I)
FIELDS = ["email", "original_source_channel", "hs_analytics_source", "hs_object_source_label",
          "hs_object_source_detail_1", "sammy_utm_source", "sammy_utm_medium",
          "sammy_utm_campaign", "first_utm", "aircall_last_call_at", "phone", "mobilephone",
          "hs_google_click_id", "hs_facebook_click_id", "hs_analytics_first_url", "hs_analytics_last_url"]

def req(method, url, body=None):
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(url, data=data,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}, method=method)
    for _ in range(5):
        try:
            resp = urllib.request.urlopen(r, timeout=60)
            return resp.status, (json.load(resp) if resp.status not in (204,) else {})
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503): time.sleep(3); continue
            return e.code, e.read().decode()[:200]
    return 0, "retry-exhausted"

def all_contacts():
    ids, after = [], None
    while True:
        b = {"limit": 100, "properties": ["hs_object_id"]}
        if after: b["after"] = after
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/search", b)
        ids += [r["id"] for r in d["results"]]
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: break
        time.sleep(0.3)
    recs = []
    for i in range(0, len(ids), 100):
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/read",
                    {"properties": FIELDS, "inputs": [{"id": x} for x in ids[i:i+100]]})
        recs += d["results"]
    return recs

def derive_from_utm(src, med):
    src, med = (src or "").lower(), (med or "").lower()
    if med == "email": return "cold_email"
    if med in ("cpc", "cpm", "ppc", "paid"): return "paid_ads"
    if med == "social" and src in ("facebook", "meta", "instagram", "google", "tiktok", "linkedin_ads"): return "paid_ads"
    if src == "linkedin" and med != "social": return "linkedin_automation"
    if med == "organic" or (src == "google" and not med): return "organic_inbound"
    if med == "referral": return "referral"
    if src in ("instantly", "mailchimp", "klaviyo"): return "cold_email"
    return None

COLD_EMAIL_TOOLS = ("Clay", "Instantly", "Outbound Sync", "EmailBison", "Email Bison", "Sammy Setup")

def classify(p):
    g = lambda k: p.get(k)
    src = g("hs_analytics_source"); detail = (g("hs_object_source_detail_1") or ""); dl = detail.lower()
    label = g("hs_object_source_label"); dialed = bool(g("aircall_last_call_at"))
    # 1. UTM
    if g("sammy_utm_source") or g("sammy_utm_medium"):
        ch = derive_from_utm(g("sammy_utm_source"), g("sammy_utm_medium"))
        if ch: return ch, "utm"
    # 2. native paid ad click
    if src in PAID: return "paid_ads", "native_paid"
    # 2b. ad click IDs or paid params captured by HubSpot tracking
    if g("hs_google_click_id") or g("hs_facebook_click_id"): return "paid_ads", "ad_click_id"
    urls = " ".join(filter(None, [g("hs_analytics_first_url"), g("hs_analytics_last_url")]))
    if PAID_URL_RX.search(urls): return "paid_ads", "paid_params_in_url"
    # 3. record origin
    if "aircall" in dl: return "cold_call", "aircall_created"
    if label == "IMPORT" or "csv" in dl:
        return ("cold_call", "csv_then_dialed") if dialed else ("cold_email", "csv_cold_list")
    if any(t in detail for t in COLD_EMAIL_TOOLS): return "cold_email", "cold_email_tool"
    if "Accounts Sync" in detail or "Database Sync" in detail: return "organic_inbound", "app_signup"
    if label in ("CRM_UI", "EXTENSION"): return "user_generated", "manual_add"
    if label in ("MEETINGS", "FORM") or any(m in detail for m in ("Google Meet","Zoom","Calendly","Meetings")): return "organic_inbound", "inbound"
    # 4. native organic
    if src in ORGANIC: return "organic_inbound", "native_organic"
    # 5. dialed, origin unknown
    if dialed: return "cold_call", "dialed_unknown_origin"
    return None, "truly_unknown"


def first_call_direction(cid):
    """Earliest associated call's direction: OUTBOUND = we dialed them, INBOUND = they called us."""
    st, a = req("GET", f"https://api.hubapi.com/crm/v4/objects/contacts/{cid}/associations/calls")
    ids = [str(x["toObjectId"]) for x in (a.get("results") or [])][:20]
    if not ids: return None
    st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/calls/batch/read",
                {"properties": ["hs_call_direction", "hs_timestamp"], "inputs": [{"id": x} for x in ids]})
    calls = sorted(d.get("results", []), key=lambda c: c["properties"].get("hs_timestamp") or "9")
    return calls[0]["properties"].get("hs_call_direction") if calls else None


PERSONAL_DOMAINS = {"gmail.com","hotmail.com","outlook.com","icloud.com","live.com","live.com.au","live.co.uk",
 "yahoo.com","yahoo.com.au","bigpond.com","bigpond.net.au","ozemail.com.au","me.com","msn.com",
 "privaterelay.appleid.com","hotmail.com.au","aol.com","proton.me","protonmail.com"}

def business_domain(email):
    d = (email or "").split("@")[-1].lower()
    return d if d and "." in d and d not in PERSONAL_DOMAINS else None

def stamp_deal_sources():
    """Item 2: deal_source = associated contact's person channel (write-once: blanks only)."""
    ids, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [{"propertyName": "deal_source", "operator": "NOT_HAS_PROPERTY"}]}], "limit": 100}
        if after: b["after"] = after
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/deals/search", b)
        ids += [r["id"] for r in d.get("results", [])]
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: break
        time.sleep(0.2)
    updates = []
    for did in ids:
        st, a = req("GET", f"https://api.hubapi.com/crm/v4/objects/deals/{did}/associations/contacts")
        res = a.get("results") or []
        if not res: continue
        cid = str(res[0]["toObjectId"])
        st, c = req("GET", f"https://api.hubapi.com/crm/v3/objects/contacts/{cid}?properties=person_original_channel,original_source_channel")
        p = c.get("properties", {}) if st == 200 else {}
        ch = p.get("person_original_channel") or p.get("original_source_channel")
        if ch: updates.append({"id": did, "properties": {"deal_source": ch}})
        time.sleep(0.1)
    if COMMIT:
        for i in range(0, len(updates), 100):
            req("POST", "https://api.hubapi.com/crm/v3/objects/deals/batch/update", {"inputs": updates[i:i+100]})
            time.sleep(0.3)
    print(f"deal_source: {len(ids)} blank deals, {len(updates)} stamped from contact channel" + ("" if COMMIT else " [dry-run]"))


# Monthly plan value per sammy_pricing_plan (annual_950 = 950/12). Deal amounts
# follow the team's existing convention of monthly plan dollars; the $10 promo
# (sammy_promo_code) is deducted so deal reports tie to dashboard MRR.
PLAN_AMOUNT = {"founder_59": 59, "monthly_99": 99, "annual_950": 79}
# Recurring monthly discounts only. One-time coupons (oJiHwI0k / FIRSTMO50 =
# 50% off first month) do not reduce the ongoing amount and stay out of this map.
PROMO_MONTHLY_DISCOUNT = {"qRlQX1PO": 10}


def stamp_deal_amounts():
    """amount = associated contact's monthly plan value (write-once: blanks only).
    Contacts on free or no plan are skipped; manual amounts are never touched."""
    ids, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [{"propertyName": "amount", "operator": "NOT_HAS_PROPERTY"}]}], "limit": 100}
        if after: b["after"] = after
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/deals/search", b)
        ids += [r["id"] for r in d.get("results", [])]
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: break
        time.sleep(0.2)
    updates = []
    for did in ids:
        st, a = req("GET", f"https://api.hubapi.com/crm/v4/objects/deals/{did}/associations/contacts")
        res = a.get("results") or []
        if not res: continue
        cid = str(res[0]["toObjectId"])
        st, c = req("GET", f"https://api.hubapi.com/crm/v3/objects/contacts/{cid}?properties=sammy_pricing_plan,sammy_promo_code")
        p = c.get("properties", {}) if st == 200 else {}
        amt = PLAN_AMOUNT.get(p.get("sammy_pricing_plan"))
        if amt is None: continue
        disc = PROMO_MONTHLY_DISCOUNT.get(p.get("sammy_promo_code"), 0)
        if disc: amt = max(amt - disc, 0)
        updates.append({"id": did, "properties": {"amount": str(amt)}})
        time.sleep(0.1)
    if COMMIT:
        for i in range(0, len(updates), 100):
            req("POST", "https://api.hubapi.com/crm/v3/objects/deals/batch/update", {"inputs": updates[i:i+100]})
            time.sleep(0.3)
    print(f"deal_amount: {len(ids)} blank-amount deals, {len(updates)} stamped from plan value" + ("" if COMMIT else " [dry-run]"))


LUCAS = "86929887"
# Auto-generated or previously groomed subjects we are allowed to manage.
MANAGED_SUBJECTS = ("call this lead immediately", "call new lead", "follow up with ",
                    "trial convert:", "winback:", "cold call:", "enrich first", "upgrade call:",
                    "onboarding nudge:", "check in:")
SYS_INBOX = ("support@", "noreply@", "no-reply@", "notifications@", "postmaster@", "mailer-daemon@", "help@")


def stamp_became_paid():
    """became_paid_customer_date = FIRST GENUINE transition into paid_customer.
    Walk user_status history chronologically and count only real value CHANGES:
    contact merges and sync re-writes log new versions with the same value and
    must not move the date (Aug 5 fix after Lucas caught re-stamped closes).
    Recomputes all paid customers hourly so drift self-corrects."""
    contacts, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [
            {"propertyName": "user_status", "operator": "IN", "values": ["paid_customer", "churned"]}]}],
            "limit": 100}
        if after: b["after"] = after
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/search", b)
        contacts += d.get("results", [])
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: break
        time.sleep(0.2)
    updates = []
    for i in range(0, len(contacts), 50):
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/read",
                    {"inputs": [{"id": c["id"]} for c in contacts[i:i+50]],
                     "properties": ["became_paid_customer_date"],
                     "propertiesWithHistory": ["user_status"]})
        for r in d.get("results", []):
            vers = list(reversed(r.get("propertiesWithHistory", {}).get("user_status") or []))  # oldest first
            first_paid, prev = None, None
            for v in vers:
                val = v.get("value")
                if val == "paid_customer" and prev != "paid_customer":
                    first_paid = v["timestamp"]
                    break  # first genuine entry into paid
                prev = val
            if first_paid and r["properties"].get("became_paid_customer_date") != first_paid:
                updates.append({"id": r["id"], "properties": {"became_paid_customer_date": first_paid}})
        time.sleep(0.2)
    if COMMIT:
        for i in range(0, len(updates), 100):
            req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/update", {"inputs": updates[i:i+100]})
            time.sleep(0.3)
    print(f"became-paid stamper: {len(contacts)} paid customers, {len(updates)} dates corrected to first genuine conversion" + ("" if COMMIT else " [dry-run]"))


def stamp_closed_on_call():
    """closed_on_call = became a paying customer the SAME Melbourne day as a
    completed meeting with Lucas. Cross-object comparison HubSpot calculated
    properties cannot do; recomputed hourly so late-marked meeting outcomes
    upgrade false -> true (Chris, Aug 5)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    MEL = ZoneInfo("Australia/Melbourne")
    def mel_day(ts):
        if not ts: return None
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(MEL).date()

    paid, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [
            {"propertyName": "user_status", "operator": "IN", "values": ["paid_customer", "churned"]},
            {"propertyName": "became_paid_customer_date", "operator": "HAS_PROPERTY"}]}],
            "properties": ["became_paid_customer_date", "closed_on_call"], "limit": 200}
        if after: b["after"] = after
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/search", b)
        paid += d.get("results", [])
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: break
        time.sleep(0.2)

    # contact -> meeting associations in batch
    assoc = {}
    for i in range(0, len(paid), 100):
        st, a = req("POST", "https://api.hubapi.com/crm/v4/associations/contacts/meetings/batch/read",
                    {"inputs": [{"id": c["id"]} for c in paid[i:i+100]]})
        for r in a.get("results", []):
            assoc[str(r["from"]["id"])] = [str(t["toObjectId"]) for t in r.get("to", [])]
        time.sleep(0.2)
    mids = sorted({m for v in assoc.values() for m in v})
    minfo = {}
    for i in range(0, len(mids), 100):
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/meetings/batch/read",
                    {"inputs": [{"id": x} for x in mids[i:i+100]],
                     "properties": ["hs_meeting_outcome", "hubspot_owner_id", "hs_timestamp"]})
        for r in d.get("results", []): minfo[r["id"]] = r["properties"]
        time.sleep(0.2)

    updates = []
    for c in paid:
        paid_day = mel_day(c["properties"].get("became_paid_customer_date"))
        val = "false"
        for mid in assoc.get(c["id"], []):
            m = minfo.get(mid) or {}
            if (m.get("hs_meeting_outcome") == "COMPLETED" and m.get("hubspot_owner_id") == LUCAS
                    and mel_day(m.get("hs_timestamp")) == paid_day):
                val = "true"; break
        if (c["properties"].get("closed_on_call") or "") != val:
            updates.append({"id": c["id"], "properties": {"closed_on_call": val}})
    if COMMIT:
        for i in range(0, len(updates), 100):
            req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/update", {"inputs": updates[i:i+100]})
            time.sleep(0.3)
    trues = sum(1 for u in updates if u["properties"]["closed_on_call"] == "true")
    print(f"closed-on-call stamper: {len(paid)} paid checked, {len(updates)} updated ({trues} set true this run)" + ("" if COMMIT else " [dry-run]"))


def stamp_demo_meetings():
    """is_demo = true on every scheduler-booked (Meetings Public) Lucas meeting
    missing the flag. Native Call and Meeting Types tab is unavailable in this
    portal, so this custom property is the demo marker (Chris, Aug 5). Hourly
    so new bookings self-tag within the hour."""
    ids, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [
            {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": LUCAS},
            {"propertyName": "hs_meeting_source", "operator": "EQ", "value": "MEETINGS_PUBLIC"},
            {"propertyName": "is_demo", "operator": "NOT_HAS_PROPERTY"}]}], "limit": 100}
        if after: b["after"] = after
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/meetings/search", b)
        ids += [r["id"] for r in d.get("results", [])]
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: break
        time.sleep(0.2)
    if COMMIT:
        for i in range(0, len(ids), 100):
            req("POST", "https://api.hubapi.com/crm/v3/objects/meetings/batch/update",
                {"inputs": [{"id": x, "properties": {"is_demo": "true"}} for x in ids[i:i+100]]})
            time.sleep(0.3)
    print(f"demo stamper: {len(ids)} new scheduler meetings tagged is_demo" + ("" if COMMIT else " [dry-run]"))


def guard_merge_overwrites():
    """Contact merges bypass write-once and can rewrite attribution channels
    (found Aug 5: 5 contacts flipped). If the LATEST write to an attribution
    property came from a merge AND changed the value, restore the pre-merge
    value. Scans recently modified contacts each run."""
    PROPS = ["original_source_channel", "person_original_channel"]
    cutoff = str(int((time.time() - 3 * 86400) * 1000))
    ids, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [
            {"propertyName": "lastmodifieddate", "operator": "GTE", "value": cutoff},
            {"propertyName": "original_source_channel", "operator": "HAS_PROPERTY"}]}], "limit": 100}
        if after: b["after"] = after
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/search", b)
        ids += [r["id"] for r in d.get("results", [])]
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: break
        time.sleep(0.2)
    fixes = []
    for i in range(0, len(ids), 50):
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/read",
                    {"inputs": [{"id": x} for x in ids[i:i+50]], "propertiesWithHistory": PROPS})
        for r in d.get("results", []):
            props = {}
            for p in PROPS:
                vers = r.get("propertiesWithHistory", {}).get(p) or []
                if len(vers) >= 2 and vers[0].get("sourceType") == "MERGE_OBJECTS" and vers[0]["value"] != vers[1]["value"]:
                    props[p] = vers[1]["value"]
            if props: fixes.append({"id": r["id"], "properties": props})
        time.sleep(0.2)
    if COMMIT:
        for i in range(0, len(fixes), 100):
            req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/update", {"inputs": fixes[i:i+100]})
            time.sleep(0.3)
    print(f"merge guard: {len(ids)} recently modified scanned, {len(fixes)} merge-overwritten channels restored" + ("" if COMMIT else " [dry-run]"))


KRISHNA = "162267743"
SAMMY_BOT = "162258278"

def route_tickets():
    """Support routing (Krishna's rule Aug 6): current customer tickets -> Krishna,
    everyone else (trial etc) -> Lucas. Routes by the associated contact's
    user_status. Only reassigns tickets still owned by the bot or unassigned, so
    manual reassignments and dev/founder escalations are never overridden. Open
    tickets only; re-checks hourly so a trial->paid change reroutes."""
    tickets, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [
            {"propertyName": "hs_pipeline_stage", "operator": "NEQ", "value": "4"}]}],
            "properties": ["hubspot_owner_id"], "limit": 100}
        if after: b["after"] = after
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/tickets/search", b)
        tickets += d.get("results", [])
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: break
        time.sleep(0.2)
    routable = [t for t in tickets if (t["properties"].get("hubspot_owner_id") or SAMMY_BOT) in (SAMMY_BOT, "")]
    updates = []
    for t in routable:
        st, a = req("GET", f"https://api.hubapi.com/crm/v4/objects/tickets/{t['id']}/associations/contacts")
        res = a.get("results") or []
        if not res: continue
        cid = str(res[0]["toObjectId"])
        st, c = req("GET", f"https://api.hubapi.com/crm/v3/objects/contacts/{cid}?properties=user_status")
        status = (c.get("properties", {}) if st == 200 else {}).get("user_status")
        owner = KRISHNA if status == "paid_customer" else LUCAS
        if t["properties"].get("hubspot_owner_id") != owner:
            updates.append({"id": t["id"], "properties": {"hubspot_owner_id": owner}})
        time.sleep(0.1)
    if COMMIT:
        for i in range(0, len(updates), 100):
            req("POST", "https://api.hubapi.com/crm/v3/objects/tickets/batch/update", {"inputs": updates[i:i+100]})
            time.sleep(0.3)
    k = sum(1 for u in updates if u["properties"]["hubspot_owner_id"] == KRISHNA)
    print(f"ticket router: {len(tickets)} open, {len(routable)} bot/unassigned, {len(updates)} routed ({k} Krishna, {len(updates)-k} Lucas)" + ("" if COMMIT else " [dry-run]"))


def ticket_followups():
    """When a ticket is Resolved, create a +2 day follow-up task for its owner to
    confirm the issue stayed resolved (Krishna: 'we like to follow up'). Dedup via
    followup_task_created; owned-by-bot resolved tickets get Krishna as default."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    tickets, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [
            {"propertyName": "hs_pipeline_stage", "operator": "EQ", "value": "4"},
            {"propertyName": "followup_task_created", "operator": "NOT_HAS_PROPERTY"},
            {"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": str(int((time.time() - 3 * 86400) * 1000))}]}],
            "properties": ["subject", "hubspot_owner_id"], "limit": 100}
        if after: b["after"] = after
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/tickets/search", b)
        tickets += d.get("results", [])
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: break
        time.sleep(0.2)
    mel = ZoneInfo("Australia/Melbourne")
    due_date = datetime.now(mel).date() + timedelta(days=2)
    due = (datetime(due_date.year, due_date.month, due_date.day) - timedelta(days=1)).strftime("%Y-%m-%dT22:00:00Z")
    made = 0
    for t in tickets:
        owner = t["properties"].get("hubspot_owner_id")
        if owner in (None, "", SAMMY_BOT): owner = KRISHNA
        subj = t["properties"].get("subject") or f"Ticket {t['id']}"
        if COMMIT:
            req("POST", "https://api.hubapi.com/crm/v3/objects/tasks", {
                "properties": {"hs_task_subject": f"Confirm resolved: {subj}", "hs_task_priority": "MEDIUM",
                               "hs_task_status": "NOT_STARTED", "hs_task_type": "TODO",
                               "hs_timestamp": due, "hubspot_owner_id": owner},
                "associations": [{"to": {"id": t["id"]},
                                  "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 226}]}]})
            req("PATCH", f"https://api.hubapi.com/crm/v3/objects/tickets/{t['id']}", {"properties": {"followup_task_created": "true"}})
            made += 1
        time.sleep(0.15)
    print(f"ticket follow-ups: {len(tickets)} newly-resolved without a follow-up, {made} tasks created" + ("" if COMMIT else " [dry-run]"))


def groom_lucas_tasks():
    """Rename + reprioritize Lucas's open auto-tasks by what the contact IS now.
    paid/churned contacts: task archived (Chris's standing rule, Jul 28).
    Hand-written tasks (non-managed subjects) are never touched."""
    tasks, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [
            {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": LUCAS},
            {"propertyName": "hs_task_status", "operator": "NEQ", "value": "COMPLETED"}]}],
            "properties": ["hs_task_subject", "hs_task_priority"], "limit": 100}
        if after: b["after"] = after
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/tasks/search", b)
        tasks += d.get("results", [])
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: break
        time.sleep(0.2)
    managed = [t for t in tasks if (t["properties"].get("hs_task_subject") or "").strip().lower().startswith(MANAGED_SUBJECTS)]

    assoc = {}
    for i in range(0, len(managed), 100):
        st, a = req("POST", "https://api.hubapi.com/crm/v4/associations/tasks/contacts/batch/read",
                    {"inputs": [{"id": t["id"]} for t in managed[i:i+100]]})
        for r in a.get("results", []):
            to = r.get("to") or []
            if to: assoc[str(r["from"]["id"])] = str(to[0]["toObjectId"])
        time.sleep(0.2)

    cids = sorted(set(assoc.values()))
    cinfo = {}
    for i in range(0, len(cids), 100):
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/read",
                    {"inputs": [{"id": c} for c in cids[i:i+100]],
                     "properties": ["email", "firstname", "lastname", "user_status", "phone", "hs_calculated_phone_number"]})
        for r in d.get("results", []): cinfo[r["id"]] = r["properties"]
        time.sleep(0.2)

    updates, archives = [], []
    for t in managed:
        cid = assoc.get(t["id"])
        if not cid or cid not in cinfo: continue
        c = cinfo[cid]
        email = (c.get("email") or "").lower()
        name = " ".join(x for x in (c.get("firstname"), c.get("lastname")) if x) or c.get("email") or "contact"
        status = c.get("user_status")
        if status in ("paid_customer", "churned") or any(email.startswith(p) for p in SYS_INBOX):
            archives.append(t["id"]); continue
        has_phone = bool(c.get("hs_calculated_phone_number") or c.get("phone"))
        # Sign-in-with-Apple relay addresses with no phone: unreachable ghost,
        # and the anonymized email can never be waterfall-enriched (Chris, Jul 28)
        if email.endswith("@privaterelay.appleid.com") and not has_phone:
            archives.append(t["id"]); continue
        # internal, test, and vendor addresses are never sales calls (Aug 3)
        if email.endswith(("@withsammy.ai", ".withsammy.ai", "@paintmelbourne.com",
                           "ghostinspector.com")) or email.endswith("@linkedin.com"):
            archives.append(t["id"]); continue
        if status == "active_trial":            subject, prio = f"Trial convert: {name}", "HIGH"
        elif status == "trial_expired":         subject, prio = f"Winback: {name}", "MEDIUM"
        elif status == "free":                  subject, prio = f"Upgrade call: {name}", "MEDIUM"
        elif status == "incomplete_onboarding": subject, prio = f"Onboarding nudge: {name}", "MEDIUM"
        elif status:                            subject, prio = f"Check in: {name}", "MEDIUM"
        elif has_phone:                         subject, prio = f"Cold call: {name}", "MEDIUM"
        else:
            # no phone = not a callable task; archived. create_ready_tasks()
            # re-creates the task the hour a phone lands (Chris, Aug 3)
            archives.append(t["id"]); continue
        p = t["properties"]
        if p.get("hs_task_subject") != subject or p.get("hs_task_priority") != prio:
            updates.append({"id": t["id"], "properties": {"hs_task_subject": subject, "hs_task_priority": prio}})
    if COMMIT:
        for i in range(0, len(updates), 100):
            req("POST", "https://api.hubapi.com/crm/v3/objects/tasks/batch/update", {"inputs": updates[i:i+100]})
            time.sleep(0.3)
        for i in range(0, len(archives), 100):
            req("POST", "https://api.hubapi.com/crm/v3/objects/tasks/batch/archive", {"inputs": [{"id": x} for x in archives[i:i+100]]})
            time.sleep(0.3)
    print(f"task groomer: {len(managed)} managed tasks, {len(updates)} renamed/reprioritized, {len(archives)} archived (contact now paid/churned/system-inbox)" + ("" if COMMIT else " [dry-run]"))


RATION_PER_DAY = int(os.environ.get("LUCAS_RATION_PER_DAY", "100"))  # matches rep dial target


def create_ready_tasks():
    """Contacts that gained a phone after their lead event get their call task
    here (the workflow only enrolls once, at the event, and now requires a
    phone). Guard rails: recent contacts only, zero existing task associations
    (a closed task means Lucas already worked them), no paid/churned/internal."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    cands, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [
            {"propertyName": "hs_calculated_phone_number", "operator": "HAS_PROPERTY"},
            {"propertyName": "createdate", "operator": "GTE", "value": "2026-06-01T00:00:00Z"}]}],
            "properties": ["email", "firstname", "lastname", "user_status"], "limit": 200}
        if after: b["after"] = after
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/search", b)
        cands += d.get("results", [])
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: break
        time.sleep(0.2)
    cands = [c for c in cands if c["properties"].get("user_status") not in ("paid_customer", "churned")
             and not any((c["properties"].get("email") or "").lower().startswith(p) for p in SYS_INBOX)
             and not (c["properties"].get("email") or "").lower().endswith(
                 ("withsammy.ai", "@paintmelbourne.com", "ghostinspector.com", "@linkedin.com", "@privaterelay.appleid.com"))]
    untasked = []
    for i in range(0, len(cands), 100):
        st, a = req("POST", "https://api.hubapi.com/crm/v4/associations/contacts/tasks/batch/read",
                    {"inputs": [{"id": c["id"]} for c in cands[i:i+100]]})
        tasked = {str(r["from"]["id"]) for r in a.get("results", []) if r.get("to")}
        untasked += [c for c in cands[i:i+100] if c["id"] not in tasked]
        time.sleep(0.2)
    untasked = untasked[:50]  # per-run cap
    mel_tomorrow = datetime.now(ZoneInfo("Australia/Melbourne")).date() + timedelta(days=1)
    due = (datetime(mel_tomorrow.year, mel_tomorrow.month, mel_tomorrow.day) - timedelta(days=1)).strftime("%Y-%m-%dT22:00:00Z")
    made = 0
    if COMMIT:
        for c in untasked:
            name = " ".join(x for x in (c["properties"].get("firstname"), c["properties"].get("lastname")) if x) or c["properties"].get("email") or "contact"
            req("POST", "https://api.hubapi.com/crm/v3/objects/tasks", {
                "properties": {"hs_task_subject": f"Cold call: {name}", "hs_task_priority": "MEDIUM",
                               "hs_task_status": "NOT_STARTED", "hs_task_type": "CALL",
                               "hs_timestamp": due, "hubspot_owner_id": LUCAS},
                "associations": [{"to": {"id": c["id"]},
                                  "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 204}]}]})
            made += 1
            time.sleep(0.15)
    print(f"ready-task creator: {len(cands)} phone-known recent contacts, {len(untasked)} untasked -> {made} tasks created" + ("" if COMMIT else " [dry-run]"))



def _mel_due_ts(mel_date):
    """hs_timestamp for 8am Melbourne on mel_date = 22:00Z the prior day (AEST)."""
    from datetime import datetime, timedelta
    d = datetime(mel_date.year, mel_date.month, mel_date.day) - timedelta(days=1)
    return d.strftime("%Y-%m-%dT22:00:00Z")


def ration_lucas_tasks():
    """Deal Lucas a finishable day: up to RATION_PER_DAY managed tasks due today
    (Melbourne), remainder spread over following days by priority then age.
    Tasks already due today are never pushed out. Enrich-first parks +7 days."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Australia/Melbourne")).date()

    tasks, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [
            {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": LUCAS},
            {"propertyName": "hs_task_status", "operator": "NEQ", "value": "COMPLETED"}]}],
            "properties": ["hs_task_subject", "hs_task_priority", "hs_timestamp", "hs_createdate"], "limit": 100}
        if after: b["after"] = after
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/tasks/search", b)
        tasks += d.get("results", [])
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: break
        time.sleep(0.2)
    managed = [t for t in tasks if (t["properties"].get("hs_task_subject") or "").strip().lower().startswith(MANAGED_SUBJECTS)]

    def mel_date_of(ts):
        if not ts: return None
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("Australia/Melbourne")).date()

    updates = []
    park, pool, due_today = [], [], []
    for t in managed:
        subj = (t["properties"].get("hs_task_subject") or "").lower()
        if subj.startswith("enrich first"):
            park.append(t); continue
        if mel_date_of(t["properties"].get("hs_timestamp")) == today:
            due_today.append(t)
        else:
            pool.append(t)

    # park enrich-first a rolling week out
    park_date = today + timedelta(days=7)
    for t in park:
        if mel_date_of(t["properties"].get("hs_timestamp")) != park_date:
            updates.append({"id": t["id"], "properties": {"hs_timestamp": _mel_due_ts(park_date)}})

    # deal the pool: priority rank, then oldest created first
    RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    pool.sort(key=lambda t: (RANK.get(t["properties"].get("hs_task_priority"), 3),
                             t["properties"].get("hs_createdate") or ""))
    slots_today = max(RATION_PER_DAY - len(due_today), 0)
    day, filled = today, len(due_today)
    for t in pool:
        if filled >= RATION_PER_DAY:
            day = day + timedelta(days=1); filled = 0
        target = day if (day != today or slots_today > 0) else today + timedelta(days=1)
        if day == today and slots_today <= 0:
            day = today + timedelta(days=1); filled = 0; target = day
        if mel_date_of(t["properties"].get("hs_timestamp")) != target:
            updates.append({"id": t["id"], "properties": {"hs_timestamp": _mel_due_ts(target)}})
        filled += 1
        if day == today: slots_today -= 1
    if COMMIT:
        for i in range(0, len(updates), 100):
            req("POST", "https://api.hubapi.com/crm/v3/objects/tasks/batch/update", {"inputs": updates[i:i+100]})
            time.sleep(0.3)
    print(f"task rationer: {len(managed)} managed ({len(due_today)} already today, {len(pool)} pooled, {len(park)} parked), {len(updates)} due dates set, ration {RATION_PER_DAY}/day" + ("" if COMMIT else " [dry-run]"))


def normalize_phones(recs):
    """AU numbers stored without +61 (bare 1300/1800, 04 mobiles, 0x landlines) are
    unparseable by HubSpot, which breaks click-to-call. Normalize to E.164 hourly."""
    import re as _re2
    def fix(p):
        if not p: return None
        d = _re2.sub(r"\D", "", p)
        if p.strip().startswith("+"):
            # +61 0xxxxxxxxx double-prefix: drop the 0 after 61
            if d.startswith("610") and len(d) == 12: return "+61" + d[3:]
            # AU 1800/1300 wrongly saved with +1: 9 digits starting 800/300
            if d.startswith("1800") and len(d) == 10 and p.strip().startswith("+1 8"): return "+61" + d
            if d.startswith("1300") and len(d) == 10 and p.strip().startswith("+1 3"): return "+61" + d
            return None
        if d.startswith("61") and len(d) in (11, 12): return "+" + d
        if (d.startswith("1300") or d.startswith("1800")) and len(d) == 10: return "+61" + d
        if d.startswith("13") and len(d) == 6: return "+61" + d
        if d.startswith("0") and len(d) == 10: return "+61" + d[1:]
        return None
    updates = []
    for r in recs:
        props = {}
        for f in ("phone", "mobilephone"):
            n = fix(r["properties"].get(f))
            if n: props[f] = n
        if props: updates.append({"id": r["id"], "properties": props})
    if updates and COMMIT:
        for i in range(0, len(updates), 100):
            req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/update", {"inputs": updates[i:i+100]})
            time.sleep(0.3)
    if updates: print(f"phones normalized to E.164: {len(updates)}" + ("" if COMMIT else " [dry-run]"))


def fix_call_owners():
    """Aircall logs the true agent only in the call note; HubSpot assigns the activity
    to the CONTACT owner. Re-own recent calls to the person who actually dialed."""
    import re as _re3, datetime as _dt
    NAME2OWNER = {"krishna": "162267743", "lucas": "86929887", "jared": "160312345", "chris": "162042962",
                  # rollup decision (Jul 17/18): Liam rolls up to Krishna. Ekaterina and
                  # Delia were never Sammy agents (mistake); their calls are staged for
                  # deletion and any NEW call from them should fire the unmapped WARNING.
                  "liam": "162267743"}
    RX = _re3.compile(r"made by\s*<strong>\s*([A-Za-z]+)", _re3.I)
    since = (_dt.datetime.utcnow() - _dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fixes = []; after = None
    total = parsed = 0; unmapped = Counter()
    while True:
        b = {"filterGroups": [{"filters": [
              {"propertyName": "hs_call_app_id", "operator": "EQ", "value": "36503"},
              {"propertyName": "hs_timestamp", "operator": "GTE", "value": since}]}],
             "properties": ["hs_call_body", "hubspot_owner_id"], "limit": 100}
        if after: b["after"] = after
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/calls/search", b)
        for c in d.get("results", []):
            total += 1
            m = RX.search(c["properties"].get("hs_call_body") or "")
            if not m: continue
            parsed += 1
            agent = m.group(1).lower()
            want = NAME2OWNER.get(agent)
            if not want:
                unmapped[agent] += 1
                continue
            if c["properties"].get("hubspot_owner_id") != want:
                fixes.append({"id": c["id"], "properties": {"hubspot_owner_id": want}})
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after: break
        time.sleep(0.2)
    if fixes and COMMIT:
        for i in range(0, len(fixes), 100):
            req("POST", "https://api.hubapi.com/crm/v3/objects/calls/batch/update", {"inputs": fixes[i:i+100]})
            time.sleep(0.3)
    print(f"call-owner healing: {total} calls in window, {parsed} parsed, {len(fixes)} corrected")
    if unmapped:
        print(f"WARNING unmapped agents (calls stay contact-owner-owned, need HubSpot seats or mapping): {dict(unmapped)}")
    if total >= 10 and parsed / max(total, 1) < 0.7:
        print(f"WARNING call-note parse rate {round(parsed/total*100)}% — Aircall may have changed its note format; healer degraded")

def main():
    if not TOKEN: sys.exit("Set HUBSPOT_TOKEN to the attribution writer token (30858065).")
    recs = all_contacts()
    # Second responsibility: WE own first_utm. Compose it (write-once) from the raw
    # sammy_utm_* fields that the Sammy app writes. Never overwrites an existing value.
    futm = []
    for r in recs:
        p = r["properties"]
        if not p.get("first_utm") and p.get("sammy_utm_source"):
            joined = "|".join(filter(None, [p.get("sammy_utm_source"), p.get("sammy_utm_medium"), p.get("sammy_utm_campaign")]))
            if joined: futm.append((r["id"], joined))
    if futm and COMMIT:
        for i in range(0, len(futm), 100):
            batch = futm[i:i+100]
            req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/update",
                {"inputs": [{"id": cid, "properties": {"first_utm": v}} for cid, v in batch]})
            time.sleep(0.3)
    if futm: print(f"first_utm composed (write-once) for {len(futm)} contacts" + ("" if COMMIT else " [dry-run]"))
    blanks = [r for r in recs if not r["properties"].get("original_source_channel")]
    print(f"Total contacts: {len(recs)}  |  blank original_source_channel: {len(blanks)}\n")
    # Item 3: same-domain inheritance index. If colleagues at a business domain are
    # already attributed, a signal-less colleague record inherits the majority channel.
    dom_idx = {}
    for r in recs:
        ch0 = r["properties"].get("original_source_channel")
        d0 = business_domain(r["properties"].get("email"))
        if ch0 and d0: dom_idx.setdefault(d0, Counter())[ch0] += 1
    plan, reasons, to_write = Counter(), Counter(), []
    for r in blanks:
        ch, reason = classify(r["properties"])
        if not ch:
            d0 = business_domain(r["properties"].get("email"))
            if d0 and d0 in dom_idx:
                ch, reason = dom_idx[d0].most_common(1)[0][0], "same_domain_inheritance"
        # Direction gate: an Aircall-origin contact whose FIRST call was INBOUND called us,
        # so it is inbound interest, not a cold call.
        if ch == "cold_call" and reason in ("aircall_created", "dialed_unknown_origin"):
            if first_call_direction(r["id"]) == "INBOUND":
                ch, reason = "organic_inbound", "inbound_caller"
        reasons[reason] += 1
        if ch: plan[ch] += 1; to_write.append((r["id"], ch))
    print("Would ATTRIBUTE:")
    for ch, n in plan.most_common(): print(f"   {ch:18} {n}")
    print("Reason breakdown:")
    for reason, n in reasons.most_common(): print(f"   {reason:24} {n}")
    print(f"\nWould leave blank (truly unknown): {len(blanks) - len(to_write)}")
    print(f"Total to write: {len(to_write)}")
    if not COMMIT:
        print("\nDRY RUN. Re-run with --commit to write."); return
    print("\nCOMMITTING to original_source_channel ...")
    ok = err = 0
    for i in range(0, len(to_write), 100):
        batch = to_write[i:i+100]
        inputs = [{"id": cid, "properties": {"original_source_channel": ch}} for cid, ch in batch]
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/update", {"inputs": inputs})
        if st in (200, 201, 202): ok += len(batch)
        else: err += len(batch); print("  batch err", st, str(d)[:150])
        time.sleep(0.3)
    print(f"\nDone. {ok} written, {err} errors.")
    normalize_phones(recs)
    stamp_deal_sources()
    stamp_deal_amounts()
    stamp_became_paid()
    stamp_closed_on_call()
    stamp_demo_meetings()
    guard_merge_overwrites()
    route_tickets()
    ticket_followups()
    reconcile_deals()
    groom_lucas_tasks()
    ration_lucas_tasks()
    create_ready_tasks()
    fix_call_owners()

if __name__ == "__main__":
    main()
