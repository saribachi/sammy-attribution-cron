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
          "sammy_utm_campaign", "first_utm", "aircall_last_call_at",
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
    stamp_deal_sources()

if __name__ == "__main__":
    main()
