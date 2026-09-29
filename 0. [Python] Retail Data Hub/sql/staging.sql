-- STAGING: mỗi nguồn một view, tất cả được đưa về MỘT cấu trúc chung.
-- Các quy tắc áp dụng tại đây (định nghĩa duy nhất mà mọi báo cáo kế thừa):
--   * thời gian       -> giờ Việt Nam (UTC+7)
--   * mã sản phẩm     -> master_sku qua sku_master (mã lạ để NULL và bị gắn cờ)
--   * doanh thu thuần -> giá niêm yết x số lượng - giảm giá do shop chịu (không trừ giảm giá do sàn tài trợ)
--   * đơn huỷ/void    -> is_valid = false (giữ lại để đối soát, loại khỏi marts)
-- {start} / {end} giới hạn các phân vùng được đọc (partition pruning).

CREATE OR REPLACE TABLE sku_master AS SELECT * FROM read_csv('{hub}/master/sku_master.csv', all_varchar = true);
-- master data: mã sản phẩm của mọi kênh đều trỏ về MỘT master_sku
CREATE OR REPLACE TABLE sku_map AS
          SELECT 'pos'    AS channel, pos_code   AS source_sku, master_sku FROM sku_master
UNION ALL SELECT 'shopee', shopee_sku, master_sku FROM sku_master
UNION ALL SELECT 'tiktok', tiktok_sku, master_sku FROM sku_master
UNION ALL SELECT 'web',    barcode,    master_sku FROM sku_master;
CREATE OR REPLACE TABLE store_master AS SELECT * FROM read_csv('{hub}/master/store_master.csv', all_varchar = true);

CREATE OR REPLACE VIEW stg_pos AS
SELECT 'pos' AS channel, MaHD AS order_id, MaCH AS store_code,
       strptime(NgayBan, '%d/%m/%Y %H:%M') AS order_ts, MaHang AS source_sku,
       SL::INT AS qty, SL::BIGINT * DonGia::BIGINT AS gross, GiamGia::BIGINT AS discount, TRUE AS is_valid
FROM read_csv('{hub}/lake/pos/*/*.csv', hive_partitioning = true, all_varchar = true)
WHERE dt BETWEEN '{start}' AND '{end}';

CREATE OR REPLACE VIEW stg_shopee AS
SELECT DISTINCT 'shopee' AS channel, order_sn AS order_id, 'ONLINE' AS store_code,
       make_timestamp((create_time::BIGINT + 7 * 3600) * 1000000) AS order_ts, item_sku AS source_sku,
       quantity::INT AS qty, quantity::BIGINT * original_price::BIGINT AS gross, seller_discount::BIGINT AS discount,
       order_status = 'COMPLETED' AS is_valid
FROM read_csv('{hub}/lake/shopee/*/*.csv', hive_partitioning = true, all_varchar = true)
WHERE dt BETWEEN '{start}' AND '{end}';

CREATE OR REPLACE VIEW stg_tiktok AS
SELECT 'tiktok' AS channel, order_id, 'ONLINE' AS store_code,
       strptime(created_at, '%Y-%m-%dT%H:%M:%SZ') + INTERVAL 7 HOUR AS order_ts, lower(seller_sku) AS source_sku,
       qty::INT AS qty, qty::BIGINT * sku_unit_original_price::BIGINT AS gross, seller_discount::BIGINT AS discount,
       order_status = 'DELIVERED' AS is_valid
FROM read_csv('{hub}/lake/tiktok/*/*.csv', hive_partitioning = true, all_varchar = true)
WHERE dt BETWEEN '{start}' AND '{end}';

CREATE OR REPLACE VIEW stg_web AS
SELECT 'web' AS channel, id AS order_id, 'ONLINE' AS store_code,
       strptime(left(created_on, 19), '%Y-%m-%dT%H:%M:%S') AS order_ts, variant_code AS source_sku,
       qty::INT AS qty, qty::BIGINT * price::BIGINT AS gross, discount_amount::BIGINT AS discount,
       financial_status = 'paid' AS is_valid
FROM read_csv('{hub}/lake/web/*/*.csv', hive_partitioning = true, all_varchar = true)
WHERE dt BETWEEN '{start}' AND '{end}';

-- một bảng fact chung cho mọi kênh
CREATE OR REPLACE TABLE fct_sales AS
WITH unioned AS (
    SELECT * FROM stg_pos UNION ALL SELECT * FROM stg_shopee
    UNION ALL SELECT * FROM stg_tiktok UNION ALL SELECT * FROM stg_web
)
SELECT u.channel, u.order_id, u.store_code, u.order_ts, CAST(u.order_ts AS DATE) AS dt,
       u.source_sku, k.master_sku, m.product_name, m.category,
       u.qty, u.gross, u.discount, u.gross - u.discount AS net, u.is_valid
FROM unioned u
LEFT JOIN sku_map k ON k.channel = u.channel AND k.source_sku = u.source_sku
LEFT JOIN sku_master m ON m.master_sku = k.master_sku;

CREATE OR REPLACE TABLE fct_inventory AS
SELECT i.location, CAST(i.snapshot_date AS DATE) AS dt, m.master_sku, i.product_code,
       i.on_hand::INT AS on_hand, i.avg_cost::BIGINT AS avg_cost
FROM read_csv('{hub}/lake/erp/*/*.csv', hive_partitioning = true, all_varchar = true) i
LEFT JOIN sku_master m ON i.product_code = m.erp_code
WHERE i.dt = '{end}';
