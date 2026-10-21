# sammy-attribution-cron
Runs the governed attribution classifier hourly against HubSpot (portal 244038625).
Writes original_source_channel only for blank contacts (write-once, intent-gated).
Env: HUBSPOT_TOKEN (the attribution writer app, 30858065).

Ticket watch (`ticket_watch.py`, started by run.py): posts each NEW HubSpot Ops ticket and each
comment by Krishna (notes owned by him, logged via Krishna Connection) to the sweep channel within
a minute. No other changes are posted. Env: `TICKET_WATCH` off (default) | log | slack;
`TICKET_WATCH_PIPELINES` (default 2611789549); `TICKET_WATCH_COMMENTERS` owner ids (default
162267743); `TICKET_WATCH_POLL` seconds (default 60). Silent on start, so a redeploy never replays.
