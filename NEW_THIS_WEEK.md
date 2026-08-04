# New this week (included in the next Monday sweep, then cleared via commit)
- New contact property "Became Paid Customer Date": exact moment each customer started paying, backfilled for all existing customers from status history, stamped hourly. This is now the source of truth for sales counting everywhere.
- New contact property "Closed On Call": marks customers who paid the same day as a completed demo with Lucas. 21 historical same-day closes found. Updates hourly.
- New meeting property "Is Demo": tags every booking from Lucas's scheduling link (260 backfilled, new ones tag within the hour). Reports filter on this instead of meeting source.
- Deal records now mirror reality: every paying customer has a won deal in the Sales Pipeline dated to their actual conversion (49 corrected, 5 created). Enforced hourly. Sales and Onboarding pipelines each keep their own deal by design.
- Lucas scorecard live at attribution.hirecharm.com/lucas: dials, unique dials, demos booked and completed, outcomes, closed on call, sales made, with click-through to every record behind each number.
- New HubSpot dashboards built by Chris: Lucas daily dashboard and weekly sales dashboard (dials, unique dials, demos, stacked outcome chart, closed on call, sales made).
- Week convention unified: all surfaces use HubSpot's Sunday-start weeks in Melbourne time.
- Task system: no call tasks are created without a phone number; tasks appear automatically the hour a phone number lands; queue deals Lucas 100 tasks per day.
- Meeting hygiene rule live: past meetings still marked Scheduled or unmarked are flagged in this sweep (53 found at rollout, Lucas clearing).
