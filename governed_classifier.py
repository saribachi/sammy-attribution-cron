#!/usr/bin/env python3
"""
Governed attribution classifier for Sammy (portal 244038625).

Writes the derived channel to original_source_channel_input (NOT original_source_channel),
so the HubSpot "Freeze Original Source Channel" workflow copies it into the real field
write-once. This makes attribution overwrite-proof: re-running this can never corrupt a
value that is already set.

RULES (intent-gated — a raw import with no signal stays blank BY DESIGN):
  1. UTM present (sammy_utm_* or first_utm)  -> derive channel from UTM
  2. Native paid signal (hs_analytics_source PAID_SOCIAL/PAID_SEARCH) -> paid_ads
  3. Record origin:
       - Aircall-created (dialed a fresh number)          -> cold_call
       - App signup (Sammy Accounts Sync / Setup)         -> organic_inbound
       - CSV import / Clay with no engagement             -> leave blank (intent-gated)
  4. Native organic signal (ORGANIC_SEARCH/DIRECT/REFERRALS) -> organic_inbound
  5. Otherwise -> leave blank

Only fills contacts whose original_source_channel is currently BLANK. Contacts that
already have a channel are left alone (the Freeze workflow protects them anyway).
Contamination fixes on already-set contacts (e.g. the 12 warm cold_calls) are a
separate supervised correction, not this pass.

USAGE
  export HUBSPOT_TOKEN=pat-na2-e5d783f4-...   # the attribution writer app (30858065)
  python3 governed_classifier.py             # DRY RUN: report only, no writes
  python3 governed_classifier.py --commit    # write original_source_channel_input
"""
import os, sys, json, time, urllib.request, urllib.error
from collections import Counter

TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
COMMIT = "--commit" in sys.argv
PAID = {"PAID_SOCIAL", "PAID_SEARCH"}
ORGANIC = {"ORGANIC_SEARCH", "DIRECT_TRAFFIC", "REFERRALS", "SOCIAL_MEDIA"}
FIELDS = ["original_source_channel", "hs_analytics_source", "hs_object_source_label",
          "hs_object_source_detail_1", "sammy_utm_source", "sammy_utm_medium",
          "sammy_utm_campaign", "first_utm", "user_status"]

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

def classify(p):
    g = lambda k: p.get(k)
    # 1. UTM
    if g("sammy_utm_source") or g("sammy_utm_medium"):
        ch = derive_from_utm(g("sammy_utm_source"), g("sammy_utm_medium"))
        if ch: return ch, "utm"
    # 2. native paid
    if g("hs_analytics_source") in PAID: return "paid_ads", "native_paid"
    # 3. origin
    detail = (g("hs_object_source_detail_1") or "")
    label = g("hs_object_source_label")
    if "Aircall" in detail: return "cold_call", "aircall_created_cold_dial"
    if "Accounts Sync" in detail or "Setup" in detail or "Database Sync" in detail:
        return "organic_inbound", "app_signup"
    if label == "IMPORT" or "csv" in detail.lower():
        return None, "raw_import_intent_gated_blank"
    # 4. native organic
    if g("hs_analytics_source") in ORGANIC: return "organic_inbound", "native_organic"
    return None, "no_signal_blank"

def main():
    if not TOKEN: sys.exit("Set HUBSPOT_TOKEN to the attribution writer token (30858065).")
    recs = all_contacts()
    blanks = [r for r in recs if not r["properties"].get("original_source_channel")]
    print(f"Total contacts: {len(recs)}  |  currently blank original_source_channel: {len(blanks)}\n")
    plan, reasons = Counter(), Counter()
    to_write = []
    for r in blanks:
        ch, reason = classify(r["properties"])
        reasons[reason] += 1
        if ch:
            plan[ch] += 1
            to_write.append((r["id"], ch))
    print("Would ATTRIBUTE (write to original_source_channel_input):")
    for ch, n in plan.most_common(): print(f"   {ch:18} {n}")
    print(f"\nWould leave BLANK (intent-gated, by design): {len(blanks) - len(to_write)}")
    print("Reason breakdown:")
    for reason, n in reasons.most_common(): print(f"   {reason:34} {n}")
    print(f"\nTotal to write: {len(to_write)}")
    if not COMMIT:
        print("\nDRY RUN — no writes. Re-run with --commit to write original_source_channel_input.")
        return
    print("\nCOMMITTING to original_source_channel_input ...")
    ok = err = 0
    for i in range(0, len(to_write), 100):
        batch = to_write[i:i+100]
        inputs = [{"id": cid, "properties": {"original_source_channel_input": ch}} for cid, ch in batch]
        st, d = req("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/update", {"inputs": inputs})
        if st in (200, 201, 202): ok += len(batch)
        else: err += len(batch); print("  batch err", st, str(d)[:150])
        time.sleep(0.3)
    print(f"\nDone. {ok} written to _input, {err} errors. The Freeze workflow copies each into original_source_channel once.")

if __name__ == "__main__":
    main()
