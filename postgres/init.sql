CREATE TABLE IF NOT EXISTS cursor_group_current (
    group_id TEXT PRIMARY KEY,
    group_name TEXT NOT NULL,
    member_count INTEGER NOT NULL,
    group_limit_per_user NUMERIC(12,2),
    total_group_limit NUMERIC(14,2),
    current_consumption NUMERIC(14,4) NOT NULL DEFAULT 0,
    remaining_amount NUMERIC(14,4),
    utilization_percent NUMERIC(8,3),
    members_over_80 INTEGER NOT NULL DEFAULT 0,
    members_over_90 INTEGER NOT NULL DEFAULT 0,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cursor_member_current (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    display_name TEXT,
    spend NUMERIC(14,4) NOT NULL DEFAULT 0,
    spend_overage NUMERIC(14,4) NOT NULL DEFAULT 0,
    spend_included NUMERIC(14,4) NOT NULL DEFAULT 0,
    effective_limit NUMERIC(12,2),
    group_count INTEGER NOT NULL DEFAULT 0,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cursor_group_member_current (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    email TEXT NOT NULL,
    display_name TEXT,
    spend NUMERIC(14,4) NOT NULL DEFAULT 0,
    spend_overage NUMERIC(14,4) NOT NULL DEFAULT 0,
    spend_included NUMERIC(14,4) NOT NULL DEFAULT 0,
    effective_limit NUMERIC(12,2),
    group_limit_per_user NUMERIC(12,2),
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS collector_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL,
    message TEXT,
    subscription_cycle_start BIGINT,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Upgrade existing volumes (init only runs on first boot).
ALTER TABLE cursor_member_current ADD COLUMN IF NOT EXISTS spend_overage NUMERIC(14,4) NOT NULL DEFAULT 0;
ALTER TABLE cursor_member_current ADD COLUMN IF NOT EXISTS spend_included NUMERIC(14,4) NOT NULL DEFAULT 0;
ALTER TABLE cursor_group_member_current ADD COLUMN IF NOT EXISTS spend_overage NUMERIC(14,4) NOT NULL DEFAULT 0;
ALTER TABLE cursor_group_member_current ADD COLUMN IF NOT EXISTS spend_included NUMERIC(14,4) NOT NULL DEFAULT 0;
ALTER TABLE collector_status ADD COLUMN IF NOT EXISTS subscription_cycle_start BIGINT;

CREATE OR REPLACE VIEW cursor_group_report AS
SELECT
    group_id,
    group_name,
    member_count,
    group_limit_per_user,
    total_group_limit,
    current_consumption,
    remaining_amount,
    utilization_percent,
    members_over_80,
    members_over_90,
    refreshed_at
FROM cursor_group_current
ORDER BY group_name;

DROP VIEW IF EXISTS cursor_user_report;
CREATE VIEW cursor_user_report AS
SELECT
    m.group_id,
    g.group_name,
    m.user_id,
    m.email,
    m.display_name,
    m.spend,
    m.spend_overage,
    m.spend_included,
    m.group_limit_per_user,
    m.effective_limit,
    GREATEST(
        COALESCE(m.group_limit_per_user, 0),
        COALESCE(m.effective_limit, 0)
    ) AS applicable_limit,
    CASE
        WHEN GREATEST(COALESCE(m.group_limit_per_user, 0), COALESCE(m.effective_limit, 0)) = 0 THEN NULL
        ELSE ROUND((m.spend / GREATEST(COALESCE(m.group_limit_per_user, 0), COALESCE(m.effective_limit, 0))) * 100, 2)
    END AS group_usage_percent,
    m.refreshed_at
FROM cursor_group_member_current m
JOIN cursor_group_current g ON g.group_id = m.group_id
ORDER BY g.group_name, group_usage_percent DESC NULLS LAST;
