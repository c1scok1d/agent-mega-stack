-- sql/20250809_refresh_tokens_and_profile.sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Users table
DO $$ BEGIN
    CREATE TABLE IF NOT EXISTS users (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        name TEXT,
        birthday DATE,
        profession TEXT,
        business_name TEXT,
        business_address TEXT
    );
EXCEPTION WHEN others THEN
    -- table exists; ignore
    NULL;
END $$;

-- Refresh tokens (hashed)
DO $$ BEGIN
    CREATE TABLE IF NOT EXISTS refresh_tokens (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash TEXT UNIQUE NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
EXCEPTION WHEN others THEN
    NULL;
END $$;

-- If legacy 'token' column exists, migrate values into token_hash and drop 'token'
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='refresh_tokens' AND column_name='token'
    ) THEN
        -- No plaintext available here; best-effort: disallow reuse by clearing table
        DELETE FROM refresh_tokens;
        ALTER TABLE refresh_tokens DROP COLUMN IF EXISTS token;
        ALTER TABLE refresh_tokens ADD COLUMN IF NOT EXISTS token_hash TEXT UNIQUE;
    END IF;
END $$;

-- API keys
DO $$ BEGIN
    CREATE TABLE IF NOT EXISTS api_keys (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        secret TEXT UNIQUE NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_used_at TIMESTAMPTZ
    );
EXCEPTION WHEN others THEN
    NULL;
END $$;
