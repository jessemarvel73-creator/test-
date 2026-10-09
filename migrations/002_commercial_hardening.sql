-- CPM Shop Commercial Edition: commercial hardening migration
-- Safe to run repeatedly on PostgreSQL 13+.

ALTER TABLE promo_redemptions
  ADD COLUMN IF NOT EXISTS quantity INTEGER NOT NULL DEFAULT 1;

ALTER TABLE inventory_items
  ADD COLUMN IF NOT EXISTS sold_order_id TEXT;

ALTER TABLE order_deliveries
  ADD COLUMN IF NOT EXISTS delivery_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE order_deliveries
  ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMP;
ALTER TABLE order_deliveries
  ADD COLUMN IF NOT EXISTS delivery_error TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_orders_pending_cleanup
  ON orders(status, created_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_inventory_product_status
  ON inventory_items(product_id, status);
CREATE INDEX IF NOT EXISTS idx_payments_status_created
  ON payments(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status_created
  ON orders(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_promo_redemptions_user_code
  ON promo_redemptions(user_id, promo_code_id, created_at DESC);

UPDATE promo_redemptions SET quantity = 1
 WHERE quantity IS NULL OR quantity < 1;

INSERT INTO settings(key, value) VALUES
  ('schema_version', '002_commercial_hardening'),
  ('analytics_enabled', '1')
ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value;
