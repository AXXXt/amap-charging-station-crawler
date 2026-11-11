# 2026-11-11 collector source and shared API release

## Source versions

- AMap Android app: 0.1.42.21 (versionCode 63), project directory: android/.
- Baidu Android app: 0.1.61 (versionCode 62), separate repository: hjw-alt/collector-baidu, branch main.
- Shared scheduler and result ingestion: this repository, branch master.

## Shared server deployment

Sync api_server.py, site_exploration_bridge.py, and the NEW daily_tariff_policy.py together. Also synchronize priority_station_scopes.py and config/priority_station_scopes.json so a later batch recreation retains the current allocation: Baidu 995 stations and AMap 223 stations. Do not reactivate temporary batches or run maintenance mutation scripts.

Follow the existing service deployment process and restart the API process. The recorded deployment uses /opt/amap-crawler and the systemd service amap-api. Preserve the server's existing .env and data directory. This release uses existing JSON fields and requires no database schema migration.

A git pull of this repository does not deploy the EVCS chart frontend/backend. That is a separate application/release.

## Runtime behavior

Task claims can include dailyTariff from the same station and explicit map platform. Cached schedules are reusable only on the same Beijing calendar day, while younger than six hours, complete across 00:00-24:00, and with valid electricity/service/total fee splits. The phone also checks the page's current price against the cached period for the CURRENT time, not the price observed in the morning.

A missing/incomplete schedule, new day, price mismatch, unreadable current price, or six-hour expiry triggers full tariff reading. Cache failures fall back to full collection without blocking task claims. Reused schedules preserve priceScheduleCapturedAt; priceScheduleMode and priceSchedulePlatform retain provenance in snapshots. The first collection after deployment can legitimately have no usable cache until a new phone upload records this metadata.

Baidu reads fast/super/slow card counts on the station detail landing page without entering or scrolling the individual-pile page. New live card summaries replace old fast/super/slow categories as a group. Previously collected static pile details remain available and do not override the fresh aggregate counts.

## Verification

- Python: python -m unittest test_daily_tariff_policy test_site_exploration_bridge test_monitor_retry_limit -v (29 tests).
- Android: Gradle :core:test :app:assembleDebug for the AMap project and the separate Baidu export.
- After deployment: check /health, normal device task claims, successful new snapshots, and original tariff capture timestamps on cache reuse. Use real device requests for observation; do not consume/reset tasks for a deployment smoke check.

No database data changes, phone collection run, or production deployment are part of the source push.
