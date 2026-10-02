-- The outer task-plan layer (manager/task_plan.py) persists through
-- agent_memory_events like everything else (event_type='plan_item_status',
-- detail carries plan_id/plan_item_id/status/contract) -- this view is the
-- "latest row wins" projection that module's own docstring describes: one
-- row per (plan_id, plan_item_id), the most recent status for each.
--
-- Run once against the oma database (setup.sh/entrypoint-app.sh do this
-- for you):
--   psql -h $OMA_PG_HOST -p $OMA_PG_PORT -U $OMA_PG_USER -d $OMA_PG_DB -f scripts/002_active_task_plan_items_view.sql

CREATE OR REPLACE VIEW active_task_plan_items AS
SELECT DISTINCT ON (detail->>'plan_id', detail->>'plan_item_id')
    id,
    created_at,
    detail->>'plan_id' AS plan_id,
    detail->>'plan_item_id' AS plan_item_id,
    detail->>'status' AS status,
    detail
FROM agent_memory_events
WHERE event_type = 'plan_item_status'
  AND active = true
ORDER BY detail->>'plan_id', detail->>'plan_item_id', created_at DESC;
