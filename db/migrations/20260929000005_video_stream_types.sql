-- migrate:up

-- On their own: a new enum value can only be used once the transaction adding it is
-- committed (next migration), and dbmate sends a file as one implicit transaction.
ALTER TYPE endpoint_type ADD VALUE IF NOT EXISTS 'mp4';
ALTER TYPE endpoint_type ADD VALUE IF NOT EXISTS 'dash';

-- migrate:down

-- enum values cannot be removed without recreating the type
