-- The queries used while measuring. Nothing here changes data.

-- Every sku and its stock; the skus under ten units are the ones priced with a surcharge.
SELECT sku, quantity, quantity < 10 AS low_stock FROM stock_levels ORDER BY sku;

-- How many skus are low.
SELECT count(*) FILTER (WHERE quantity < 10) AS low_stock_skus, count(*) AS skus FROM stock_levels;
