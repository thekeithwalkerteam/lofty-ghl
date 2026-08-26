# lofty-ghl-fm-sync

Hourly GitHub Action that syncs **Family Member 1** data from Lofty to
GoHighLevel. When someone adds a family member to a Lofty contact, this job
picks it up on its next run and fills the 6 GHL custom fields (Family Member
Name / Email / Phone / Relationship, Lofty Lead ID) plus the
`Has Family Member` tag on the matching GHL contact.

**This replaces nothing that already ran.** The one-time backfill was done
via bulk import (`ghl_family_import_v3.csv`, 646 records, 2026-08-25). This
job only keeps GHL current for NEW/changed family members going forward.

## Design rules (Walker Team change ritual)
- **Add/update only.** Never deletes contacts, never clears fields, never
  removes tags.
- **Idempotent.** Unchanged records are skipped; a failed run self-corrects
  on the next hourly run.
- **The 58 FM1-flagged records** (`config.json → skip_lead_ids`) are never
  synced until Kris rules on them (then delete their IDs from the list).
- **`apination` is never added** — API Nation loop prevention.
- Matching is by **primary email** (same key as the GHL seed). Leads with
  FM1 but no email can't attach in GHL; they're listed in the run report
  for the monthly catch-up import.

## Setup (one time)
1. Create a **private** GitHub repo (e.g. `walker-team/lofty-ghl-fm-sync`)
   and push these files.
2. Repo → Settings → Secrets and variables → Actions → add:
   - `LOFTY_API_TOKEN` — Lofty personal API key (⚠ expires ~30 days;
     rotating it here is part of the monthly ritual)
   - `GHL_API_TOKEN` — GHL Private Integration token (contacts read/write)
   - `GHL_LOCATION_ID` — the location id
3. Actions tab → enable workflows.
4. **First run = manual dry run:** Actions → "FM1 sync" → Run workflow →
   apply = `false`. Read the job summary (counts + would-apply list).
5. If the dry run looks right: Run workflow with apply = `true`, check the
   summary again, spot-check 2–3 contacts in GHL.
6. Done — it runs hourly on its own. Each run's report is in the job
   summary; a red run means errors (it also self-corrects next hour, but
   look at the log).

## Local test (before GitHub, optional)
```
export LOFTY_API_TOKEN='...'; export GHL_API_TOKEN='...'; export GHL_LOCATION_ID='...'
python3 sync_fm1.py            # dry run
python3 sync_fm1.py --apply --limit 1   # apply exactly one
python3 sync_fm1.py --apply
```

## Config (`config.json`)
- `skip_lead_ids` — the 58 held for Kris. Remove IDs as he rules.
- `approved_relationships` — empty = sync all. To restrict per Kris's
  ruling: `["Spouse","Wife","Husband","Partner","Fiance","Fiancee"]`.
- `ghl_field_names` — must match the GHL custom-field names exactly
  (resolved to IDs at runtime; the job aborts if any name is missing).

## Known limits
- Lofty has no webhooks → this is polling. "Hourly" is GitHub's cron,
  which can drift a few minutes; worst case a new family member appears in
  GHL ~1 hour after being added in Lofty.
- FM1 only (first family member), matching what's live in GHL today.
  FM2+ and separate FM contacts are a future decision (Kris).
- If Lofty's key expires the run fails loudly (red X + email from GitHub);
  rotate the secret and re-run.
