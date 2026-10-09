-- Preserve MAX receipts; newly created Telegram RUB orders have a distinct origin.
ALTER TABLE native_payment_orders ADD COLUMN platform text NOT NULL DEFAULT 'max'
    CHECK(platform IN ('max','telegram'));
