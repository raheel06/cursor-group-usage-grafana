# Cursor Team Directory Group Usage Dashboard

This stack is intentionally simple:

Cursor Team Admin API
        |
        v
Python collector
        |
        v
PostgreSQL (CURRENT SNAPSHOT ONLY)
        |
        v
Grafana

It does NOT retain daily/monthly history. Every successful refresh replaces the
current group/member snapshot.

## What it uses

Cursor Team Admin API:

- GET /teams/directory-groups
- GET /teams/directory-groups/:groupId/members
- POST /teams/spend

The Cursor group endpoint supplies the group member count and per-member
monthly spending limit. The spend endpoint supplies current-cycle spend per
user.

IMPORTANT:
Cursor's /teams/spend `spendCents` is on-demand spend and excludes included
usage. That is the value used here when comparing consumption against the
spending limit.

## 1. Requirements

Windows 10 + WSL2
Docker Desktop with WSL2 integration enabled
(or Docker Engine + Docker Compose installed inside WSL2)

## 2. Put the project in WSL

Recommended:

mkdir -p ~/cursor-group-usage
cd ~/cursor-group-usage

Copy/extract this project there.

Do not put the project on a slow /mnt/c filesystem if you can avoid it.

## 3. Configure the environment

cp .env.example .env

Edit .env:

CURSOR_API_KEY=your_real_team_admin_api_key
GRAFANA_ADMIN_PASSWORD=your_password

The five group names are already configured from the request. You can change
GROUP_NAMES later if needed.

The Cursor API key must be a Team API key with read:* access.

## 4. Start

docker compose up -d --build

Check:

docker compose ps

You should see:

cursor-usage-postgres
cursor-usage-collector
cursor-usage-grafana

## 5. Watch the collector

docker compose logs -f collector

A successful run should look similar to:

Refresh successful: 5 groups, 123 users

If the API key is wrong, you'll see a clear HTTP 401 error.

## 6. Open Grafana

From Windows:

http://localhost:3000

Login:

admin
<the GRAFANA_ADMIN_PASSWORD from .env>

The dashboard is automatically provisioned:

Cursor -> Cursor Usage

## 7. Force a refresh

The collector runs immediately when it starts and then waits for
COLLECTION_INTERVAL_SECONDS.

To force a one-time refresh:

docker compose restart collector

## 8. Useful diagnostics

Collector:

docker compose logs --tail=200 collector

Postgres:

docker compose exec postgres psql -U cursor -d cursor_usage

Then:

SELECT * FROM cursor_group_report;
SELECT * FROM cursor_user_report;
SELECT * FROM collector_status;

## 9. Important behavior with multiple groups

Cursor says that when a user belongs to multiple groups, the highest
applicable spend limit wins.

This dashboard therefore treats each selected group as a view of the
consumption of its members. If the same user belongs to two selected groups,
their consumption will appear in both group views. It is NOT an accounting
allocation and the overall group totals should not be summed across overlapping
groups.

If your five groups are mutually exclusive, the group totals are directly
additive.

## 10. Temporary exceptions

Cursor's /teams/spend response exposes effectivePerUserLimitDollars.

The collector uses the higher of:
- the selected directory group's limit
- the user's effective limit

This makes temporary higher user limits visible in the user table and group
allocation.

## 11. Data retention

There is no historical usage table.

The following tables are overwritten on every refresh:

cursor_group_current
cursor_member_current
cursor_group_member_current

Only the latest snapshot is retained.

PostgreSQL's Docker volume is persistent so the dashboard survives restarts,
but it does not accumulate snapshots.

## 12. Updating the five groups

Edit GROUP_NAMES in .env and restart:

docker compose restart collector

The collector discovers group IDs by name every refresh, so you do not need
to copy team_group_ IDs manually.

## 13. Stop everything

docker compose down

To also delete the current snapshot and Grafana/Postgres volumes:

docker compose down -v

WARNING: `-v` removes all local data.
