-- Optional Telegram Stars support. No user FK: delayed financial updates must survive
-- account deletion and updates received before ordinary /start registration.
CREATE TABLE donation_orders (
    id bigserial PRIMARY KEY,
    payload text NOT NULL UNIQUE CHECK(octet_length(payload) BETWEEN 1 AND 128),
    user_id bigint NOT NULL CHECK(user_id > 0),
    amount integer NOT NULL CHECK(amount BETWEEN 1 AND 2500),
    currency text NOT NULL DEFAULT 'XTR' CHECK(currency = 'XTR'),
    status text NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','checkout','expired','paid','refunded')),
    checkout_query_id text UNIQUE CHECK(octet_length(checkout_query_id) BETWEEN 1 AND 2048),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '1 hour',
    paid_at timestamptz,
    refunded_at timestamptz,
    CHECK(expires_at > created_at),
    UNIQUE(id,payload,user_id,amount,currency)
);
CREATE INDEX ix_donation_orders_user ON donation_orders(user_id,id DESC);

CREATE TABLE donation_payments (
    telegram_payment_charge_id text PRIMARY KEY
        CHECK(octet_length(telegram_payment_charge_id) BETWEEN 1 AND 2048),
    order_id bigint NOT NULL UNIQUE,
    payload text NOT NULL UNIQUE,
    user_id bigint NOT NULL CHECK(user_id > 0),
    amount integer NOT NULL CHECK(amount BETWEEN 1 AND 2500),
    currency text NOT NULL CHECK(currency = 'XTR'),
    paid_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(order_id,payload,user_id,amount,currency)
        REFERENCES donation_orders(id,payload,user_id,amount,currency),
    UNIQUE(telegram_payment_charge_id,payload,currency,amount)
);

CREATE TABLE donation_refunds (
    id bigserial PRIMARY KEY,
    telegram_payment_charge_id text NOT NULL UNIQUE,
    payload text NOT NULL,
    currency text NOT NULL CHECK(currency = 'XTR'),
    amount integer NOT NULL CHECK(amount BETWEEN 1 AND 2500),
    refunded_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(telegram_payment_charge_id,payload,currency,amount)
        REFERENCES donation_payments(telegram_payment_charge_id,payload,currency,amount)
);

CREATE TABLE donation_support_requests (
    id bigserial PRIMARY KEY,
    user_id bigint NOT NULL CHECK(user_id > 0),
    message text NOT NULL CHECK(char_length(message) BETWEEN 1 AND 4000),
    status text NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    reply_text text CHECK(char_length(reply_text) BETWEEN 1 AND 4000),
    replied_at timestamptz
);
CREATE INDEX ix_donation_support_open ON donation_support_requests(created_at) WHERE status='open';

CREATE FUNCTION donation_reject_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Donation ledger records are append-only' USING ERRCODE='23000';
END;
$$;
CREATE TRIGGER donation_payments_append_only BEFORE UPDATE OR DELETE ON donation_payments
    FOR EACH ROW EXECUTE FUNCTION donation_reject_ledger_mutation();
CREATE TRIGGER donation_refunds_append_only BEFORE UPDATE OR DELETE ON donation_refunds
    FOR EACH ROW EXECUTE FUNCTION donation_reject_ledger_mutation();

CREATE FUNCTION donation_keep_order_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id,NEW.payload,NEW.user_id,NEW.amount,NEW.currency,NEW.created_at,NEW.expires_at)
        IS DISTINCT FROM
       (OLD.id,OLD.payload,OLD.user_id,OLD.amount,OLD.currency,OLD.created_at,OLD.expires_at) THEN
        RAISE EXCEPTION 'Donation order identity and price are immutable' USING ERRCODE='23000';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER donation_orders_identity BEFORE UPDATE ON donation_orders
    FOR EACH ROW EXECUTE FUNCTION donation_keep_order_identity();
