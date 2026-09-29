-- Cùng bảng fact nhưng trên BigQuery (môi trường production).
-- Chia phân vùng theo ngày + cluster theo các cột mà báo cáo nào cũng lọc,
-- nên truy vấn "30 ngày gần nhất, một kênh" chỉ quét ~30 phân vùng thay vì cả bảng.

CREATE TABLE IF NOT EXISTS retail_hub.fct_sales (
  channel       STRING  NOT NULL,
  order_id      STRING  NOT NULL,
  store_code    STRING,
  order_ts      DATETIME,
  dt            DATE    NOT NULL,
  source_sku    STRING,
  master_sku    STRING,
  product_name  STRING,
  category      STRING,
  qty           INT64,
  gross         INT64,
  discount      INT64,
  net           INT64,
  is_valid      BOOL
)
PARTITION BY dt
CLUSTER BY channel, store_code, master_sku
OPTIONS (require_partition_filter = TRUE,        -- không ai lỡ tay quét toàn bộ lịch sử
         partition_expiration_days = 730);

-- Nạp tăng dần: chỉ thay các ngày vừa lấy lại (idempotent, chạy lại an toàn)
-- MERGE retail_hub.fct_sales T USING staging.fct_sales_new S
--   ON T.dt = S.dt AND T.channel = S.channel AND T.order_id = S.order_id AND T.source_sku = S.source_sku
-- WHEN MATCHED THEN UPDATE SET qty = S.qty, gross = S.gross, discount = S.discount, net = S.net, is_valid = S.is_valid
-- WHEN NOT MATCHED THEN INSERT ROW;
