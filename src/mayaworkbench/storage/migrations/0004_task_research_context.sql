-- Phase 3 follow-up: version-pinned research context on task contracts.
-- The contract records exactly which research interpretation a task implements.
ALTER TABLE task_contracts ADD COLUMN research_context_json TEXT;
