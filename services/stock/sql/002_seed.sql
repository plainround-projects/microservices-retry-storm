-- DML: the 20 skus of the catalog (sku-001 to sku-020). Four of them have fewer than ten units, which pricing
-- turns into a 15 percent surcharge. Safe to run again.
INSERT INTO stock_levels (sku, quantity)
SELECT format('sku-%s', lpad(n::text, 3, '0')), CASE WHEN n % 5 = 0 THEN n % 7 + 1 ELSE 20 + n * 3 END
FROM generate_series(1, 20) AS n
ON CONFLICT (sku) DO NOTHING;
