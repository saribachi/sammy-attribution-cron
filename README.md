# sammy-attribution-cron
Runs the governed attribution classifier hourly against HubSpot (portal 244038625).
Writes original_source_channel only for blank contacts (write-once, intent-gated).
Env: HUBSPOT_TOKEN (the attribution writer app, 30858065).

Ticket watch (`ticket_watch.py`, started by run.py): posts HubSpot Ops ticket changes
(new ticket, stage, owner, priority, title, new notes) to the sweep channel within a minute.
Env: `TICKET_WATCH` off (default) | log | slack; `TICKET_WATCH_PIPELINES` (default 2611789549);
`TICKET_WATCH_POLL` seconds (default 60). Silent on start, so a redeploy never replays.
