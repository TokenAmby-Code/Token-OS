-- Review-only outline. Prefer the adjacent Python migration because it safely
-- handles live DBs where instances.persona_lock may or may not exist.
-- Emperor-gated: do not apply to live DB from an agent worktree.

BEGIN;
UPDATE personas SET default_rank = 'astartes' WHERE slug = 'inquisitor';
-- If instances.persona_lock exists:
-- UPDATE instances SET persona_lock = NULL
--  WHERE persona_id IN (SELECT id FROM personas WHERE slug = 'inquisitor');
DELETE FROM personas WHERE slug IN ('profile_1', 'profile_3', 'profile_5', 'profile_7', 'profile_8');
COMMIT;
