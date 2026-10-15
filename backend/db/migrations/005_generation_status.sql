-- 005_generation_status.sql
--
-- generations.generation_status is written THREE times in a row's life, not
-- two. core/db/queries/generations.py inserts 'running' at the START of a
-- generation, before the agent runs, so a process that dies mid-way leaves a
-- row that is detectably interrupted rather than silently missing. Only later
-- does complete() move it to 'success' or 'incomplete'.
--
-- The original CHECK allowed only the two end states, so every insert failed
-- on a database built from schema.sql. This widens it to the set the code
-- actually writes.
--
-- Purely additive: the new set is a superset of the old one, so no existing
-- row can violate it and no data migration is needed.

ALTER TABLE generations
    DROP CONSTRAINT IF EXISTS generations_generation_status_check;

ALTER TABLE generations
    ADD CONSTRAINT generations_generation_status_check
    CHECK (generation_status IN ('running', 'success', 'incomplete'));
