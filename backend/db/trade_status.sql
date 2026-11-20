ALTER TABLE trades ADD COLUMN IF NOT EXISTS is_cancelled boolean NOT NULL DEFAULT false;
