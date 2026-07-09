# sammy-attribution-cron
Runs the governed attribution classifier hourly against HubSpot (portal 244038625).
Writes original_source_channel only for blank contacts (write-once, intent-gated).
Env: HUBSPOT_TOKEN (the attribution writer app, 30858065).
