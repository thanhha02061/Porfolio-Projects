/* =====================================================================================
   Shopee Customer Behavior & RFM  (BigQuery Standard SQL)

   Mỗi dòng kết quả = một dòng sản phẩm trong đơn Shopee, gắn thêm:
     1. Doanh thu thuần sau khi phân bổ voucher về từng dòng sản phẩm
     2. Loại khách new / active / return theo ngày, tháng, quý, năm
     3. Điểm RFM và nhóm khách (segment)
     4. Sản phẩm mua lần đầu hay mua lại, đơn một hay nhiều ngành hàng / thương hiệu
     5. Khách mua ở một shop hay nhiều shop

   Mọi chỉ số hành vi khách được tính riêng cho từng cặp (buyer_id, orgname):
   cùng một người mua ở 2 shop được xem là 2 hành trình khác nhau.
   Chỉ đơn COMPLETED được dùng để tính hành vi; đơn trạng thái khác vẫn giữ trong kết quả.
   ===================================================================================== */

WITH

-- 0. Bảng quy đổi tên shop -> mã ngắn (đổi ở đây khi mở thêm shop)
shop_map AS (
    SELECT * FROM UNNEST([
        STRUCT('Lumière Official (Hà Nội)' AS orgname, 'LUM HN'  AS shop_code),
        STRUCT('Lumière Official',                     'LUM HCM'),
        STRUCT('Aurora Official Store',                'AURORA'),
        STRUCT('Velvet Official Store',                'VELVET'),
        STRUCT('Nova by Velvet',                       'NOVA'),
        STRUCT('Mira Official Store',                  'MIRA')
    ])
),

-- 1. Dòng sản phẩm + thông tin đơn + voucher
order_lines AS (
    SELECT
        CAST(i.model_id AS STRING)                                  AS model_id,  -- ép về text để Power BI không đọc thành số thực
        i.* EXCEPT (model_id),
        o.create_time,
        DATE(o.create_time)                                         AS order_date,
        o.order_status,
        o.recipient_address_state,
        o.total_amount                                              AS order_total_amount,
        COALESCE(e.voucher_from_seller, 0)                          AS voucher_from_seller,
        COALESCE(e.voucher_from_shopee, 0)                          AS voucher_from_shopee,
        i.buyer_user_id                                             AS buyer_id,
        i.model_discounted_price                                    AS payment_total_amount,
        COALESCE(NULLIF(NULLIF(i.model_sku, '0'), ''), i.item_sku)  AS model_sku_adj,  -- SKU biến thể trống thì lấy SKU sản phẩm
        i.model_discounted_price * i.model_quantity_purchased       AS line_amount,
        LOWER(i.item_name) LIKE '%gift%'                            AS is_gift
    FROM `retail_dw.shopee_orderitems` i
    JOIN `retail_dw.shopee_orders` o
        ON o.order_sn = i.order_sn
    LEFT JOIN `retail_dw.shopee_escrows` e
        ON e.order_sn = i.order_sn
    WHERE DATE(o.create_time) >= '2024-01-01'
),

-- 2. Tỷ lệ phân bổ: mỗi dòng nhận phần voucher theo tỷ trọng giá trị trong đơn; quà tặng nhận 0
order_lines_ratio AS (
    SELECT
        *,
        IF(is_gift, 0,
           COALESCE(SAFE_DIVIDE(line_amount,
                                SUM(IF(is_gift, 0, line_amount)) OVER (PARTITION BY order_sn)), 0)) AS revenue_ratio
    FROM order_lines
),

raw_orderitem AS (
    SELECT
        * EXCEPT (line_amount, is_gift),
        voucher_from_seller * revenue_ratio                AS voucher_from_seller_allocated,
        voucher_from_shopee * revenue_ratio                AS voucher_from_shopee_allocated,
        order_total_amount  * revenue_ratio                AS order_total_amount_allocated,
        line_amount - voucher_from_seller * revenue_ratio  AS revenue_allocated  -- doanh thu thuần của dòng
    FROM order_lines_ratio
),

-- 3. Chỉ đơn hoàn thành mới dùng để tính hành vi khách
completed_orderitem AS (
    SELECT * FROM raw_orderitem WHERE order_status = 'COMPLETED'
),

buyer_first_order AS (
    SELECT buyer_id, orgname, MIN(order_date) AS first_ever_order_date
    FROM completed_orderitem
    GROUP BY buyer_id, orgname
),

-- 4a. Vòng đời khách theo THÁNG: so ngày mua cuối tháng này với ngày mua cuối tháng có mua trước đó
monthly_stats AS (
    SELECT
        buyer_id,
        orgname,
        FORMAT_DATE('%Y-%m', order_date)  AS buyer_month,
        COUNT(DISTINCT order_sn)          AS total_orders,
        MAX(order_date)                   AS month_last_order
    FROM completed_orderitem
    GROUP BY buyer_id, orgname, buyer_month
),

monthly_with_calculation AS (
    SELECT
        m.*,
        f.first_ever_order_date,
        COALESCE(m.prev_month_last_order, f.first_ever_order_date)                      AS month_first_order,
        DATE_DIFF(m.month_last_order,
                  COALESCE(m.prev_month_last_order, f.first_ever_order_date), DAY)      AS days_diff,
        CASE
            WHEN m.prev_month_last_order IS NULL THEN 'new'
            WHEN DATE_DIFF(m.month_last_order, m.prev_month_last_order, DAY) < 90 THEN 'active'
            ELSE 'return'
        END                                                                             AS type_customer
    FROM (
        SELECT *,
               LAG(month_last_order) OVER (PARTITION BY buyer_id, orgname ORDER BY buyer_month) AS prev_month_last_order
        FROM monthly_stats
    ) m
    LEFT JOIN buyer_first_order f
        ON f.buyer_id = m.buyer_id AND f.orgname = m.orgname
),

-- 4b. Vòng đời khách theo NGÀY: new = lần đầu; active = quay lại trong < 90 ngày; return = quay lại sau >= 90 ngày
daily_stats AS (
    SELECT buyer_id, orgname, order_date, COUNT(DISTINCT order_sn) AS daily_total_orders
    FROM completed_orderitem
    GROUP BY buyer_id, orgname, order_date
),

daily_with_calculation AS (
    SELECT
        d.*,
        f.first_ever_order_date,
        COALESCE(d.prev_order_date, f.first_ever_order_date)                            AS daily_first_order,
        DATE_DIFF(d.order_date, COALESCE(d.prev_order_date, f.first_ever_order_date), DAY) AS daily_days_diff,
        CASE
            WHEN d.prev_order_date IS NULL THEN 'new'
            WHEN DATE_DIFF(d.order_date, d.prev_order_date, DAY) < 90 THEN 'active'
            ELSE 'return'
        END                                                                             AS daily_type_customer,
        FORMAT_DATE('%Y-%m', d.order_date)                                              AS buyer_month,
        FORMAT('%d-Q%d', EXTRACT(YEAR FROM d.order_date), EXTRACT(QUARTER FROM d.order_date)) AS buyer_quarter,
        CAST(EXTRACT(YEAR FROM d.order_date) AS STRING)                                 AS buyer_year
    FROM (
        SELECT *,
               LAG(order_date) OVER (PARTITION BY buyer_id, orgname ORDER BY order_date) AS prev_order_date
        FROM daily_stats
    ) d
    LEFT JOIN buyer_first_order f
        ON f.buyer_id = d.buyer_id AND f.orgname = d.orgname
),

-- 4c. Gộp lên tháng / quý / năm theo thứ tự ưu tiên: có ngày new -> new; không có new mà có return -> return; còn lại active
monthly_customer_type AS (
    SELECT buyer_id, orgname, buyer_month,
           CASE WHEN LOGICAL_OR(daily_type_customer = 'new')    THEN 'new'
                WHEN LOGICAL_OR(daily_type_customer = 'return') THEN 'return'
                ELSE 'active' END AS monthly_type_customer
    FROM daily_with_calculation
    GROUP BY buyer_id, orgname, buyer_month
),

quarterly_customer_type AS (
    SELECT buyer_id, orgname, buyer_quarter,
           CASE WHEN LOGICAL_OR(daily_type_customer = 'new')    THEN 'new'
                WHEN LOGICAL_OR(daily_type_customer = 'return') THEN 'return'
                ELSE 'active' END AS quarterly_type_customer
    FROM daily_with_calculation
    GROUP BY buyer_id, orgname, buyer_quarter
),

yearly_customer_type AS (
    SELECT buyer_id, orgname, buyer_year,
           CASE WHEN LOGICAL_OR(daily_type_customer = 'new')    THEN 'new'
                WHEN LOGICAL_OR(daily_type_customer = 'return') THEN 'return'
                ELSE 'active' END AS yearly_type_customer
    FROM daily_with_calculation
    GROUP BY buyer_id, orgname, buyer_year
),

-- 5. RFM, xếp hạng phần trăm trong từng shop; điểm 1 là tốt nhất, 4 là kém nhất
rfm_user AS (
    SELECT
        buyer_id,
        orgname,
        DATE_DIFF(CURRENT_DATE(), MAX(order_date), DAY) AS recency,    -- số ngày từ lần mua gần nhất
        COUNT(DISTINCT order_date)                      AS frequency,  -- số ngày có mua
        SUM(revenue_allocated)                          AS monetary    -- tổng doanh thu thuần
    FROM completed_orderitem
    GROUP BY buyer_id, orgname
),

rfm_score AS (
    SELECT
        *,
        CASE WHEN r_rank > 0.75 THEN 4 WHEN r_rank > 0.5 THEN 3 WHEN r_rank > 0.25 THEN 2 ELSE 1 END AS r_score,
        CASE WHEN f_rank > 0.75 THEN 4 WHEN f_rank > 0.5 THEN 3 WHEN f_rank > 0.25 THEN 2 ELSE 1 END AS f_score,
        CASE WHEN m_rank > 0.75 THEN 4 WHEN m_rank > 0.5 THEN 3 WHEN m_rank > 0.25 THEN 2 ELSE 1 END AS m_score
    FROM (
        SELECT *,
               PERCENT_RANK() OVER (PARTITION BY orgname ORDER BY recency   ASC)  AS r_rank,
               PERCENT_RANK() OVER (PARTITION BY orgname ORDER BY frequency DESC) AS f_rank,
               PERCENT_RANK() OVER (PARTITION BY orgname ORDER BY monetary  DESC) AS m_rank
        FROM rfm_user
    )
),

segment_table AS (
    SELECT
        *,
        CASE
            WHEN rfm_code = '111' THEN 'Best Customers'
            WHEN r_score >= 3 AND f_score >= 3 THEN 'Lost Bad Customers'
            WHEN r_score >= 3 AND f_score = 2  THEN 'Lost Customers'
            WHEN r_score >= 3 AND f_score = 1  THEN 'Hibernating'
            WHEN r_score = 2  AND f_score = 1  THEN 'Almost Lost'
            WHEN r_score = 1  AND f_score = 1  THEN 'Loyal Customers'   -- 112, 113, 114
            WHEN f_score = 4                   THEN 'New Customers'     -- r = 1 hoặc 2
            WHEN m_score = 1                   THEN 'Big Spenders'      -- 121, 131, 221, 231
            ELSE 'Potential Loyalists'                                  -- r 1-2, f 2-3, m 2-4
        END AS segment
    FROM (
        SELECT *, CONCAT(CAST(r_score AS STRING), CAST(f_score AS STRING), CAST(m_score AS STRING)) AS rfm_code
        FROM rfm_score
    )
),

-- 6. Sản phẩm: lần đầu khách mua ngành hàng (category_lv3) này; đơn gồm những ngành hàng / thương hiệu nào
completed_with_product AS (
    SELECT c.buyer_id, c.orgname, c.order_sn, c.create_time, p.category_lv3, p.brand
    FROM completed_orderitem c
    JOIN `retail_dw.dim_product` p
        ON p.item_code = c.model_sku_adj
    WHERE p.category_lv3 IS NOT NULL
),

buyer_first_purchase AS (
    SELECT buyer_id, orgname, category_lv3, MIN(create_time) AS first_purchase_time
    FROM completed_with_product
    GROUP BY buyer_id, orgname, category_lv3
),

order_combined_data AS (
    SELECT
        order_sn,
        orgname,
        STRING_AGG(DISTINCT category_lv3, '+' ORDER BY category_lv3) AS combine_product,
        STRING_AGG(DISTINCT brand,        '+' ORDER BY brand)        AS combine_brand
    FROM completed_with_product
    GROUP BY order_sn, orgname
),

-- 7. Khách đã mua ở những shop nào
buyer_orgname_data AS (
    SELECT
        c.buyer_id,
        STRING_AGG(DISTINCT COALESCE(s.shop_code, c.orgname), '+'
                   ORDER BY COALESCE(s.shop_code, c.orgname)) AS combine_orgname
    FROM completed_orderitem c
    LEFT JOIN shop_map s
        ON s.orgname = TRIM(c.orgname)
    GROUP BY c.buyer_id
),

-- 8. Ghép tất cả về từng dòng sản phẩm (giữ cả đơn chưa hoàn thành, chỉ gắn nhãn cho đơn COMPLETED)
main_query AS (
    SELECT
        o.*,
        o.buyer_id AS c_buyer_id,

        -- theo tháng
        m.buyer_month,
        m.total_orders,
        m.month_first_order,
        m.month_last_order,
        m.days_diff,
        m.type_customer,
        mt.monthly_type_customer,

        -- theo quý, năm
        qt.buyer_quarter,
        qt.quarterly_type_customer,
        yt.buyer_year,
        yt.yearly_type_customer,

        -- theo ngày
        d.daily_total_orders,
        d.daily_first_order,
        d.daily_days_diff,
        d.daily_type_customer,

        -- RFM
        s.recency,
        s.frequency,
        s.monetary,
        s.r_score,
        s.f_score,
        s.m_score,
        s.rfm_code AS rfm_score,
        s.segment  AS rfm_segment,

        -- sản phẩm
        p.category_lv1,
        p.category_lv2,
        p.category_lv3,
        p.brand,
        ocd.combine_product,
        ocd.combine_brand,
        IF(fp.first_purchase_time < o.create_time, 'old', 'new')                            AS type_product,
        IF(CONTAINS_SUBSTR(ocd.combine_product, '+'), 'Cross-Selling', 'Single Product')    AS type_combine_product,
        IF(CONTAINS_SUBSTR(ocd.combine_brand, '+'),   'Multi Brand',   'Single Brand')      AS type_combine_brand,

        -- khoảng cách quay lại trong tháng: tháng có cả ngày new/return lẫn active thì dòng active = 0
        IF(COUNT(DISTINCT d.daily_type_customer)
               OVER (PARTITION BY o.buyer_id, o.orgname, FORMAT_DATE('%Y-%m', o.order_date)) > 1
           AND d.daily_type_customer = 'active',
           0, d.daily_days_diff)                                                            AS monthly_days_diff,

        -- đa kênh
        bod.combine_orgname,
        IF(CONTAINS_SUBSTR(bod.combine_orgname, '+'), 'Multi Channel', 'Single Channel')    AS type_combine_orgname

    FROM raw_orderitem o
    LEFT JOIN monthly_with_calculation m
        ON  m.buyer_id = o.buyer_id AND m.orgname = o.orgname
        AND m.buyer_month = FORMAT_DATE('%Y-%m', o.order_date)
        AND o.order_status = 'COMPLETED'
    LEFT JOIN daily_with_calculation d
        ON  d.buyer_id = o.buyer_id AND d.orgname = o.orgname
        AND d.order_date = o.order_date
        AND o.order_status = 'COMPLETED'
    LEFT JOIN monthly_customer_type mt
        ON  mt.buyer_id = o.buyer_id AND mt.orgname = o.orgname
        AND mt.buyer_month = FORMAT_DATE('%Y-%m', o.order_date)
        AND o.order_status = 'COMPLETED'
    LEFT JOIN quarterly_customer_type qt
        ON  qt.buyer_id = o.buyer_id AND qt.orgname = o.orgname
        AND qt.buyer_quarter = FORMAT('%d-Q%d', EXTRACT(YEAR FROM o.order_date), EXTRACT(QUARTER FROM o.order_date))
        AND o.order_status = 'COMPLETED'
    LEFT JOIN yearly_customer_type yt
        ON  yt.buyer_id = o.buyer_id AND yt.orgname = o.orgname
        AND yt.buyer_year = CAST(EXTRACT(YEAR FROM o.order_date) AS STRING)
        AND o.order_status = 'COMPLETED'
    LEFT JOIN segment_table s
        ON  s.buyer_id = o.buyer_id AND s.orgname = o.orgname
        AND o.order_status = 'COMPLETED'
    LEFT JOIN `retail_dw.dim_product` p
        ON  p.item_code = o.model_sku_adj
    LEFT JOIN buyer_first_purchase fp
        ON  fp.buyer_id = o.buyer_id AND fp.orgname = o.orgname
        AND fp.category_lv3 = p.category_lv3
        AND o.order_status = 'COMPLETED'
    LEFT JOIN order_combined_data ocd
        ON  ocd.order_sn = o.order_sn AND ocd.orgname = o.orgname
        AND o.order_status = 'COMPLETED'
    LEFT JOIN buyer_orgname_data bod
        ON  bod.buyer_id = o.buyer_id
)

-- 9. Nhóm hiển thị cho báo cáo
SELECT
    * EXCEPT (n_category),
    CASE
        WHEN monthly_days_diff > 186 THEN '>6 tháng'
        WHEN monthly_days_diff > 155 THEN '6 tháng'
        WHEN monthly_days_diff > 124 THEN '5 tháng'
        WHEN monthly_days_diff > 93  THEN '4 tháng'
        WHEN monthly_days_diff > 62  THEN '3 tháng'
        WHEN monthly_days_diff > 31  THEN '2 tháng'
        WHEN monthly_days_diff >= 0  THEN '1 tháng'
        ELSE 'Không xác định'
    END AS range_monthly_days_diff,
    CASE
        WHEN n_category = 1 THEN '1 món'
        WHEN n_category = 2 THEN '2 món'
        WHEN n_category > 2 THEN '>2 món'
        ELSE 'Không xác định'
    END AS range_combine_product
FROM (
    SELECT *,
           (SELECT COUNTIF(LOWER(TRIM(x)) != 'gift') FROM UNNEST(SPLIT(combine_product, '+')) x) AS n_category
    FROM main_query
)
