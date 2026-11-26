DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'trades' AND column_name = 'is_cancelled'
    ) THEN
        ALTER TABLE trades ADD COLUMN is_cancelled boolean NOT NULL DEFAULT false;
    END IF;
END $$;
