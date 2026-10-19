-- Cat ranks 0-25 (was 0-5): 20 new ranks above Legend. Only the CHECK on cat_adoptions.cat_rank changes; every cat keeps its rank.
-- Idempotent: any older CHECK that mentions cat_rank is dropped, then the 0-25 one is added once.
DO $$
DECLARE c RECORD;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'cat_adoptions'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%cat_rank%' AND conname <> 'cat_adoptions_cat_rank_25'
    LOOP
        EXECUTE format('ALTER TABLE cat_adoptions DROP CONSTRAINT %I', c.conname);
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'cat_adoptions'::regclass AND conname = 'cat_adoptions_cat_rank_25') THEN
        ALTER TABLE cat_adoptions ADD CONSTRAINT cat_adoptions_cat_rank_25 CHECK (cat_rank BETWEEN 0 AND 25);
    END IF;
END $$;
