-- Card/SBP receipts are separate from XTR; legacy Telegram Stars never change.
CREATE TABLE native_payment_orders (
    id uuid PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES telegram_users(telegram_user_id),
    chat_id bigint NOT NULL REFERENCES telegram_chats(telegram_chat_id),
    request_key text NOT NULL UNIQUE,
    amount_minor integer NOT NULL CHECK(amount_minor BETWEEN 10000 AND 1000000),
    currency text NOT NULL DEFAULT 'RUB' CHECK(currency='RUB'),
    method text NOT NULL CHECK(method IN ('bank_card','sbp')),
    idempotency_key uuid NOT NULL UNIQUE,
    provider_payment_id text UNIQUE,
    checkout_url text,
    status text NOT NULL DEFAULT 'creating' CHECK(status IN ('creating','pending','succeeded','canceled')),
    provider_test boolean,
    created_at timestamptz NOT NULL DEFAULT now(),
    confirmed_at timestamptz,
    last_checked_at timestamptz,
    notified_at timestamptz
);
CREATE INDEX ix_native_payment_user ON native_payment_orders(user_id,created_at DESC);
CREATE TABLE native_payment_refunds (
    order_id uuid PRIMARY KEY REFERENCES native_payment_orders(id),
    idempotency_key uuid NOT NULL UNIQUE,
    provider_refund_id text UNIQUE,
    status text NOT NULL DEFAULT 'creating' CHECK(status IN ('creating','pending','succeeded','canceled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    confirmed_at timestamptz,
    last_checked_at timestamptz,
    notified_at timestamptz
);
CREATE TABLE native_payment_support (
    id bigserial PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES telegram_users(telegram_user_id),
    request_key text NOT NULL UNIQUE,
    message text NOT NULL CHECK(length(message) BETWEEN 1 AND 4000),
    created_at timestamptz NOT NULL DEFAULT now()
);
