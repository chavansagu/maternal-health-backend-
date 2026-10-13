# Notification & Escalation (spec M) - backend notes

## New / changed API (base `/api/v2/notifications`)

| Method | Path                                    | Purpose                                                                                                                                                                                           |
| ------ | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| GET    | `/sync?since=<ISO>`                     | Offline sync: new + never-acknowledged unresolved alerts, `resolved_ids`, `server_time`                                                                                                           |
| POST   | `/sync/ack` `{ids:[..]}`                | Client confirms it stored these alerts (sets `delivered_at`)                                                                                                                                      |
| GET    | `/alerts/summary`                       | Dashboard alert widget: open alerts by priority/type, escalated count, top alerts                                                                                                                 |
| GET    | `/escalation-logs`                      | Escalation log (user, timestamp, action). District=district-wide, Block=block-wide, others=own. Filters: event_type, level, action, alert_key, pregnant_woman_id, from_date, to_date, skip, limit |
| POST   | `/devices/register` `{token, platform}` | Register FCM token (web/android/ios)                                                                                                                                                              |
| POST   | `/devices/unregister` `{token}`         | Call on logout                                                                                                                                                                                    |
| DELETE | `/clear-all`                            | Fixed: was shadowed by `DELETE /{id}` (422)                                                                                                                                                       |

Notification responses now also carry `alert_key, is_resolved, resolved_at, escalation_level`.

## Scheduler jobs (new)

- `referral_pending_alerts` - every 30 min: referral still `pending` after `REFERRAL_PENDING_AFTER_HOURS` (default 2) -> DP + Block + District; auto-closes when no longer pending.
- `escalate_alerts` - hourly: closes ANC/PMSMA alerts that no longer apply, then escalates unresolved alerts Block -> District by age.
- `generate_anc_pmsma_alerts` now also sends the ANC reminder SMS (beneficiary) once per `anc_due` alert.
- `escalate_mobilisation_cases` now also writes notifications + `escalation_logs`.

## Escalation timing (`services/alert_rules.ESCALATION_HOURS`)

Default Block after 24h, District after 48h (`ESCALATION_BLOCK_AFTER_HOURS`, `ESCALATION_DISTRICT_AFTER_HOURS` in .env).
Overrides: `anc_visit_missed` 24h/72h, `mobilisation_pending` District 144h, `referral_pending` 4h/12h.
A level that the matrix already notified at creation is never repeated.

## Alert closure now wired

referral accepted / re-referred / delivery outcome, mobilisation mobilised/closed/auto-closed, EDD case closed or admission recorded,
PMSMA completed/rescheduled, pregnancy no longer active (sweep). (ANC and USG closure already existed.)

## .env

```
FIREBASE_CREDENTIALS_PATH=/path/to/service-account.json   # enables push (also: pip install firebase-admin)
PUSH_ENABLED=true
REFERRAL_PENDING_AFTER_HOURS=2
ESCALATION_BLOCK_AFTER_HOURS=24
ESCALATION_DISTRICT_AFTER_HOURS=48
SMS_DP_REFERRAL_TEMPLATE_ID=<DLT id once approved>        # DP-referral SMS is skipped until set
```

## Deploy

1. Run `utilities/migrate_notification_escalation.sql` (adds columns to `notifications`; new tables auto-create on startup).
2. `pip install -r requirements.txt`
3. Restart the API (scheduler registers the two new jobs).
4. Tests: `pytest test_file/test_alert_escalation.py -v`
