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
FIELDS = ["original_source_channel", "hs_analytics_source", "hs_object_source_label",
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

def main():
    if not TOKEN: sys.exit("Set HUBSPOT_TOKEN to the attribution writer token (30858065).")
    recs = all_contacts()
    blanks = [r for r in recs if not r["properties"].get("original_source_channel")]
    print(f"Total contacts: {len(recs)}  |  blank original_source_channel: {len(blanks)}\n")
    plan, reasons, to_write = Counter(), Counter(), []
    for r in blanks:
        ch, reason = classify(r["properties"]); reasons[reason] += 1
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

if __name__ == "__main__":
    main()
