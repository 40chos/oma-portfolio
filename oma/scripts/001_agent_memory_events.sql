-- Durable event log for the Manager's cross-task memory (manager/memory.py reads this
-- table). Partitioned by month; scripts/010_ensure_agent_memory_events_partitions.py
-- creates the actual monthly partitions on top of this parent table.
--
-- Run once against a fresh Postgres database before first startup:
--   psql -h $OMA_PG_HOST -p $OMA_PG_PORT -U $OMA_PG_USER -d $OMA_PG_DB -f scripts/001_agent_memory_events.sql

CREATE TABLE IF NOT EXISTS agent_memory_events (
    id BIGSERIAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type TEXT NOT NULL,
    task_id UUID,
    module TEXT,
    tags TEXT[],
    actor TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail JSONB,
    supersedes_id BIGINT,
    active BOOLEAN NOT NULL DEFAULT true,
    verified BOOLEAN NOT NULL DEFAULT false,
    stale_after INTERVAL,
    root_cause TEXT,
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

CREATE INDEX IF NOT EXISTS agent_memory_events_tags_idx
    ON agent_memory_events USING gin (tags);
CREATE INDEX IF NOT EXISTS agent_memory_events_task_id_idx
    ON agent_memory_events USING btree (task_id);
CREATE INDEX IF NOT EXISTS agent_memory_events_event_type_module_idx
    ON agent_memory_events USING btree (event_type, module);
CREATE INDEX IF NOT EXISTS agent_memory_events_active_idx
    ON agent_memory_events USING btree (active) WHERE (active = true);
CREATE INDEX IF NOT EXISTS agent_memory_events_id_idx
    ON agent_memory_events USING btree (id);

-- The id/created_at composite primary key is required by range partitioning --
-- Postgres needs the partition key in any unique constraint.
