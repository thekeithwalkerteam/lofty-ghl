#!/usr/bin/env python3
"""
sync_fm1.py — Lofty → GHL Family Member 1 sync (add/update only, never delete).

For every Lofty lead that has a Family Member 1 and a primary email:
look up the GHL contact by email and upsert the 6 FM custom fields +
the 'Has Family Member' tag. Idempotent: unchanged records are skipped,
so a failed run self-corrects on the next one.

Safety rails (match the Walker Team change ritual):
  * DRY RUN by default — pass --apply to write.
  * skip_lead_ids in config.json (the 58 FM1-flagged) are NEVER synced.
  * approved_relationships filter (empty = all) for Kris's ruling.
  * NEVER adds the 'apination' tag (API Nation loop prevention).
  * Add/update only — never removes tags or clears fields in GHL.
  * Emits run_report.md (GitHub Actions writes it to the job summary).

Env: LOFTY_API_TOKEN, GHL_API_TOKEN, GHL_LOCATION_ID
"""
import argparse, json, os, sys, time, urllib.request, urllib.parse
from urllib.error import HTTPError

LOFTY = "https://api.lofty.com/v1.0"
GHL = "https://services.leadconnectorhq.com"
GHL_VERSION = "2021-07-28"

CFG = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
SKIP = set(CFG["skip_lead_ids"])
APPROVED = [r.lower() for r in CFG.get("approved_relationships", [])]
FIELD_NAMES = CFG["ghl_field_names"]
ADD_TAG = CFG.get("add_tag", "Has Family Member")
FORBIDDEN_TAGS = {t.lower() for t in CFG.get("never_add_tags", ["apination"])}


def env(name):
    v = os.environ.get(name, "").strip().strip("'\"")
    if not v:
        sys.exit(f"Missing env var {name}")
    return v


def http(method, url, headers, body=None, tries=3):
    for attempt in range(tries):
        req = urllib.request.Request(url, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8", "replace") or "{}")
        except HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                time.sleep(5 * (attempt + 1)); continue
            if e.code >= 500 and attempt < tries - 1:
                time.sleep(3); continue
            raise
    return {}


# ---------- Lofty ----------
def lofty_headers():
    return {"Authorization": f"token {env('LOFTY_API_TOKEN')}", "Content-Type": "application/json"}


def pull_lofty_leads():
    """Paged pull of all leads; returns list of dicts."""
    leads, offset, limit = [], 0, 100
    while True:
        url = f"{LOFTY}/leads?limit={limit}&offset={offset}"
        d = http("GET", url, lofty_headers())
        batch = d.get("leads") or d.get("data") or d.get("list") or []
        if not batch:
            break
        leads.extend(batch)
        offset += limit
        if len(batch) < limit:
            break
        time.sleep(0.3)
    return leads


def first_email(lead):
    ems = lead.get("emails") or []
    if isinstance(ems, list):
        for e in ems:
            v = e.get("email") if isinstance(e, dict) else str(e)
            v = (v or "").strip()
            if v and "@" in v and not v.lower().startswith("no-reply"):
                return v.lower()
    e = (lead.get("email") or "").strip()
    return e.lower() if "@" in e else ""


def fm1_of(lead):
    fam = lead.get("leadFamilyMemberList") or lead.get("familyMembers") or []
    if not fam:
        return None
    f = fam[0]
    name = " ".join(x for x in [(f.get("firstName") or "").strip(), (f.get("lastName") or "").strip()] if x)
    return {
        "name": name,
        "email": (f.get("email") or "").strip().lower(),
        "phone": (f.get("phone") or f.get("phoneNumber") or "").strip(),
        "relationship": (f.get("relationship") or "").strip(),
    }


# ---------- GHL ----------
def ghl_headers():
    return {"Authorization": f"Bearer {env('GHL_API_TOKEN')}",
            "Version": GHL_VERSION, "Content-Type": "application/json"}


def ghl_field_ids():
    """Resolve custom-field name -> id."""
    loc = env("GHL_LOCATION_ID")
    d = http("GET", f"{GHL}/locations/{loc}/customFields", ghl_headers())
    by_name = {}
    for f in d.get("customFields", []):
        by_name[(f.get("name") or "").strip().lower()] = f.get("id")
    ids = {}
    missing = []
    for key, name in FIELD_NAMES.items():
        fid = by_name.get(name.strip().lower())
        if fid:
            ids[key] = fid
        else:
            missing.append(name)
    if missing:
        sys.exit(f"GHL custom fields not found: {missing} — check names in config.json")
    return ids


def ghl_lookup(email):
    loc = env("GHL_LOCATION_ID")
    q = urllib.parse.quote(email)
    d = http("GET", f"{GHL}/contacts/?locationId={loc}&query={q}&limit=5", ghl_headers())
    for c in d.get("contacts", []):
        if (c.get("email") or "").strip().lower() == email:
            return c
    return None


def current_custom(contact, fid):
    for cf in contact.get("customFields", []) or []:
        if cf.get("id") == fid:
            return (str(cf.get("value") or "")).strip()
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--limit", type=int, default=0, help="max GHL updates this run (0 = no cap)")
    a = ap.parse_args()

    fids = ghl_field_ids()
    leads = pull_lofty_leads()
    stats = dict(lofty_total=len(leads), with_fm1=0, skipped_58=0, skipped_rel=0,
                 no_email=0, not_in_ghl=0, in_sync=0, updated=0, errors=0)
    changes, not_in_ghl = [], []

    for lead in leads:
        lid = str(lead.get("leadId") or lead.get("id") or "").strip()
        fm = fm1_of(lead)
        if not fm or not fm["name"]:
            continue
        stats["with_fm1"] += 1
        if lid in SKIP:
            stats["skipped_58"] += 1; continue
        if APPROVED and fm["relationship"].lower() not in APPROVED:
            stats["skipped_rel"] += 1; continue
        email = first_email(lead)
        if not email:
            stats["no_email"] += 1; continue

        try:
            contact = ghl_lookup(email)
        except Exception as e:
            stats["errors"] += 1; print(f"  ! lookup {email}: {e}"); continue
        if not contact:
            stats["not_in_ghl"] += 1; not_in_ghl.append(f"{email} ({lid})"); continue

        desired = {"fm_name": fm["name"], "fm_email": fm["email"],
                   "fm_phone": fm["phone"], "fm_relationship": fm["relationship"],
                   "lofty_lead_id": lid}
        diffs = {k: v for k, v in desired.items()
                 if v and current_custom(contact, fids[k]) != v}
        have_tags = {t.lower() for t in (contact.get("tags") or [])}
        need_tag = ADD_TAG and ADD_TAG.lower() not in have_tags

        if not diffs and not need_tag:
            stats["in_sync"] += 1; continue

        desc = f"{email}: " + ", ".join(f"{k}->'{v}'" for k, v in diffs.items())
        if need_tag:
            desc += f" +tag:{ADD_TAG}"
        changes.append(desc)

        if a.apply:
            body = {"customFields": [{"id": fids[k], "value": v} for k, v in diffs.items()]}
            if need_tag:
                new_tags = list(contact.get("tags") or []) + [ADD_TAG]
                body["tags"] = [t for t in new_tags if t.lower() not in FORBIDDEN_TAGS]
            try:
                http("PUT", f"{GHL}/contacts/{contact['id']}", ghl_headers(), body)
                stats["updated"] += 1
                time.sleep(0.25)
            except Exception as e:
                stats["errors"] += 1; print(f"  ! update {email}: {e}")
        else:
            stats["updated"] += 1  # would-update count in dry run

        if a.limit and stats["updated"] >= a.limit:
            print(f"  (stopping at --limit {a.limit})"); break

    mode = "APPLY" if a.apply else "DRY RUN"
    lines = [f"# FM1 sync — {mode}", ""]
    lines += [f"- {k}: **{v}**" for k, v in stats.items()]
    if changes:
        lines += ["", f"## {'Applied' if a.apply else 'Would apply'} ({len(changes)})"] + [f"- {c}" for c in changes[:100]]
        if len(changes) > 100:
            lines.append(f"- …and {len(changes)-100} more")
    if not_in_ghl:
        lines += ["", f"## In Lofty w/ FM1 but NOT in GHL ({len(not_in_ghl)}) — next monthly catch-up import"]
        lines += [f"- {x}" for x in not_in_ghl[:50]]
    report = "\n".join(lines)
    open("run_report.md", "w").write(report)
    print(report)
    if stats["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
