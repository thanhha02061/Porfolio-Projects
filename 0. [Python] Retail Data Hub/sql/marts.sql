-- MARTS: small, report-ready tables built only from fct_sales / fct_inventory.
-- Every dashboard number comes from here, so every report shows the same number.

CREATE OR REPLACE TABLE mart_daily_channel AS
SELECT dt, channel,
       count(DISTINCT order_id) AS orders, sum(qty) AS units, sum(net) AS net_revenue
FROM fct_sales
WHERE is_valid AND master_sku IS NOT NULL
GROUP BY ALL;

CREATE OR REPLACE TABLE mart_sku_30d AS
WITH s AS (
    SELECT master_sku, any_value(product_name) AS product_name, any_value(category) AS category,
           sum(qty) AS units_30d, sum(net) AS net_30d
    FROM fct_sales
    WHERE is_valid AND master_sku IS NOT NULL AND dt > DATE '{end}' - INTERVAL 30 DAY
    GROUP BY master_sku
), inv AS (
    SELECT master_sku, sum(on_hand) AS on_hand, sum(on_hand * avg_cost) AS stock_value
    FROM fct_inventory GROUP BY master_sku
)
SELECT s.*, inv.on_hand, inv.stock_value,
       round(inv.on_hand / nullif(s.units_30d / 30.0, 0), 1) AS days_cover
FROM s LEFT JOIN inv USING (master_sku);

CREATE OR REPLACE TABLE mart_store_30d AS
SELECT f.store_code, coalesce(sm.store_name, 'Online') AS store_name, coalesce(sm.city, 'Online') AS city,
       count(DISTINCT f.order_id) AS orders, sum(f.net) AS net_30d
FROM fct_sales f LEFT JOIN store_master sm USING (store_code)
WHERE f.is_valid AND f.master_sku IS NOT NULL AND f.dt > DATE '{end}' - INTERVAL 30 DAY
GROUP BY ALL;
