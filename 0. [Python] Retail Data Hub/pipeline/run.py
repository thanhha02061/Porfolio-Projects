"""Retail Data Hub - pipeline chạy hằng ngày.

    extract (tăng dần)      ->  lake/ (dữ liệu thô, chia phân vùng theo ngày)
    transform (SQL)         ->  staging views -> fct_sales / fct_inventory -> marts
    kiểm tra chất lượng     ->  dừng công bố nếu có phép kiểm tra không đạt
    publish                 ->  output/*.json, output/daily_net.svg, khối trạng thái trong README

Chạy: python pipeline/run.py            (chạy hằng ngày)
      python pipeline/run.py --rebuild  (xoá lake và nạp lại từ đầu)
"""
from __future__ import annotations

import argparse
import csv
import os
import json
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sources import SOURCES, control_total  # noqa: E402

HUB = Path(__file__).resolve().parents[1]
LAKE, OUT, STATE = HUB / "lake", HUB / "output", HUB / "state" / "watermark.json"
VN = timezone(timedelta(hours=7))
BACKFILL_DAYS = 120          # lần chạy đầu nạp ngần này ngày lịch sử
RETENTION_DAYS = 120         # phân vùng thô cũ hơn mốc này bị xoá
RECON_TOLERANCE = 0.005      # tổng ngày của hub và nguồn chỉ được lệch tối đa 0.5%


def today_vn() -> date:
    return datetime.now(VN).date()


# ------------------------------------------------------------------ 1. EXTRACT
def extract(end: date) -> dict:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    loaded = {}
    for name, fetch in SOURCES.items():
        last = date.fromisoformat(state[name]) if name in state else end - timedelta(days=BACKFILL_DAYS)
        days, d = [], last + timedelta(days=1)
        while d <= end:                                   # chỉ lấy ngày chưa nạp
            rows = fetch(d)
            part = LAKE / name / f"dt={d.isoformat()}"
            part.mkdir(parents=True, exist_ok=True)
            with open(part / "data.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
            if name != "erp":
                ctl = LAKE / "_control" / f"dt={d.isoformat()}"
                ctl.mkdir(parents=True, exist_ok=True)
                with open(ctl / f"{name}.json", "w", encoding="utf-8") as f:
                    json.dump(control_total(name, rows), f)
            days.append(d.isoformat())
            d += timedelta(days=1)
        loaded[name] = {"days": len(days), "from": days[0] if days else None, "to": days[-1] if days else None}
        state[name] = end.isoformat()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return loaded


def prune(end: date) -> int:
    cutoff, removed = end - timedelta(days=RETENTION_DAYS), 0
    for part in LAKE.glob("*/dt=*"):
        if date.fromisoformat(part.name[3:]) < cutoff:
            shutil.rmtree(part)
            removed += 1
    return removed


# ------------------------------------------------------------------ 2. TRANSFORM
def run_sql(con, file: str, **params):
    sql = (HUB / "sql" / file).read_text(encoding="utf-8")
    for k, v in params.items():
        sql = sql.replace("{" + k + "}", str(v))
    con.execute(sql)


# ------------------------------------------------------------------ 3. KIỂM TRA CHẤT LƯỢNG
def quality(con, start: date, end: date) -> list[dict]:
    checks = []

    def add(name, ok, detail, severity="fail"):
        checks.append({"check": name, "status": "pass" if ok else severity, "detail": detail})

    # độ mới: nguồn nào cũng đã gửi dữ liệu của hôm qua
    for s in SOURCES:
        ok = (LAKE / s / f"dt={end.isoformat()}" / "data.csv").exists()
        add(f"freshness_{s}", ok, f"phân vùng {end:%d/%m/%Y} {'đã có' if ok else 'bị thiếu'}")

    # không có số lượng bằng 0 hoặc âm
    bad = con.sql("SELECT count(*) FROM fct_sales WHERE qty <= 0").fetchone()[0]
    add("valid_quantity", bad == 0, f"{bad} dòng có số lượng <= 0")

    # mã sản phẩm chưa map: cảnh báo nếu có, thất bại nếu chiếm > 1% doanh thu
    unm = con.sql("""SELECT count(*), coalesce(sum(net),0),
                            (SELECT sum(net) FROM fct_sales WHERE is_valid),
                            string_agg(DISTINCT channel || ':' || source_sku, ', ')
                     FROM fct_sales WHERE is_valid AND master_sku IS NULL""").fetchone()
    share = (unm[1] / unm[2]) if unm[2] else 0
    add("sku_mapping", share <= 0.01,
        f"{unm[0]} dòng ({share:.2%} doanh thu) chưa map mã" + (f": {unm[3]}" if unm[0] else ""),
        severity="warn" if share <= 0.01 else "fail")
    if unm[0] and share <= 0.01:
        checks[-1]["status"] = "warn"

    # đối soát: doanh thu thuần của hub (đã map + chưa map) = báo cáo ngày của từng hệ thống
    hub = {(r[0], str(r[1])): r[2] for r in con.sql(
        "SELECT channel, dt, sum(net) FROM fct_sales WHERE is_valid GROUP BY ALL").fetchall()}
    worst, fails = 0.0, 0
    for f in (LAKE / "_control").glob("dt=*/*.json"):
        d = f.parent.name[3:]
        if not (start.isoformat() <= d <= end.isoformat()):
            continue
        ctl = json.loads(f.read_text(encoding="utf-8"))
        h = hub.get((ctl["source"], d), 0)
        diff = abs(h - ctl["net_revenue"]) / ctl["net_revenue"] if ctl["net_revenue"] else 0
        worst = max(worst, diff)
        fails += diff > RECON_TOLERANCE
    add("reconciliation", fails == 0, f"lệch lớn nhất trong ngày {worst:.3%} so với báo cáo nguồn; {fails} ngày vượt {RECON_TOLERANCE:.1%}")

    # tồn kho đủ dòng: mọi địa điểm x mọi SKU đang bán
    got, exp = con.sql("""SELECT count(*), (SELECT count(DISTINCT location) FROM fct_inventory) *
                          (SELECT count(*) FROM sku_master WHERE launch_date::DATE <= DATE '{e}')
                          FROM fct_inventory""".replace("{e}", end.isoformat())).fetchone()
    add("inventory_complete", got == exp, f"{got}/{exp} dòng địa điểm-SKU")
    return checks


# ------------------------------------------------------------------ 4. PUBLISH
def svg_daily(rows: list[tuple]) -> str:
    """Biểu đồ vùng xếp chồng: doanh thu thuần theo ngày, theo kênh, nhúng vào README."""
    chans = [("pos", "#2563eb", "Cửa hàng"), ("shopee", "#f97316", "Shopee"), ("tiktok", "#111827", "TikTok"), ("web", "#10b981", "Website")]
    days = sorted({r[0] for r in rows})
    val = {(str(r[0]), r[1]): r[2] for r in rows}
    W, H, L, B, T = 760, 240, 56, 28, 16
    tot = [sum(val.get((str(d), c), 0) for c, *_ in chans) for d in days]
    top = max(tot) * 1.1 or 1
    x = lambda i: L + i * (W - L - 10) / max(1, len(days) - 1)
    y = lambda v: T + (1 - v / top) * (H - T - B)
    base = [0] * len(days)
    paths = []
    for c, col, _ in chans:
        upper = [base[i] + val.get((str(d), c), 0) for i, d in enumerate(days)]
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(upper))
        back = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in reversed(list(enumerate(base))))
        paths.append(f'<polygon points="{pts} {back}" fill="{col}" fill-opacity="0.85"/>')
        base = upper
    ticks = "".join(f'<text x="{L-6}" y="{y(top*k/4)+4:.1f}" text-anchor="end">{top*k/4/1e6:.0f}M</text>'
                    f'<line x1="{L}" x2="{W-10}" y1="{y(top*k/4):.1f}" y2="{y(top*k/4):.1f}" stroke="#e5e7eb"/>' for k in range(5))
    months = "".join(f'<text x="{x(i):.1f}" y="{H-8}" text-anchor="middle">{d:%m/%Y}</text>' for i, d in enumerate(days) if d.day == 1)
    legend = "".join(f'<rect x="{L+i*110}" y="{H+6}" width="10" height="10" fill="{col}"/><text x="{L+i*110+14}" y="{H+15}">{lab}</text>'
                     for i, (_, col, lab) in enumerate(chans))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H+24}" font-family="Segoe UI,Arial,sans-serif" font-size="11" fill="#374151">'
            f'<rect width="{W}" height="{H+24}" fill="#ffffff"/>{ticks}{"".join(paths)}{months}{legend}</svg>')


def publish(con, end: date, checks, loaded, removed, seconds, files_read, files_total):
    OUT.mkdir(exist_ok=True)
    q = lambda sql: [dict(zip([d[0] for d in con.description], r)) for r in con.execute(sql).fetchall()]
    kpi = q(f"""SELECT sum(net_revenue) FILTER (WHERE dt >  DATE '{end}' - INTERVAL 30 DAY) AS net_30d,
                       sum(net_revenue) FILTER (WHERE dt <= DATE '{end}' - INTERVAL 30 DAY
                                                  AND dt >  DATE '{end}' - INTERVAL 60 DAY) AS net_prev_30d,
                       sum(orders) FILTER (WHERE dt > DATE '{end}' - INTERVAL 30 DAY) AS orders_30d
                FROM mart_daily_channel""")[0]
    kpi["aov_30d"] = round(kpi["net_30d"] / kpi["orders_30d"]) if kpi["orders_30d"] else None
    kpi["growth"] = round(kpi["net_30d"] / kpi["net_prev_30d"] - 1, 4) if kpi["net_prev_30d"] else None
    mix = q(f"SELECT channel, sum(net_revenue) AS net FROM mart_daily_channel WHERE dt > DATE '{end}' - INTERVAL 30 DAY GROUP BY 1 ORDER BY 2 DESC")
    sku = q("SELECT * FROM mart_sku_30d ORDER BY net_30d DESC")
    store = q("SELECT * FROM mart_store_30d ORDER BY net_30d DESC")
    lineage = q("""SELECT channel, count(*) AS rows_in_hub,
                          count(*) FILTER (WHERE is_valid) AS valid_rows,
                          count(*) FILTER (WHERE is_valid AND master_sku IS NOT NULL) AS mapped_rows
                   FROM fct_sales GROUP BY 1 ORDER BY 1""")
    daily = con.execute("SELECT dt, channel, net_revenue FROM mart_daily_channel ORDER BY dt").fetchall()

    status = "failed" if any(c["status"] == "fail" for c in checks) else "success"
    run = {"run_at": datetime.now(VN).isoformat(timespec="seconds"), "data_through": end.isoformat(), "status": status,
           "seconds": round(seconds, 1), "loaded": loaded, "partitions_pruned": removed,
           "files_read": files_read, "files_in_lake": files_total,
           "checks_passed": sum(c["status"] == "pass" for c in checks), "checks_total": len(checks)}
    runs_file = OUT / "runs.json"
    runs = json.loads(runs_file.read_text(encoding="utf-8")) if runs_file.exists() else []
    runs = (runs + [run])[-60:]
    runs_file.write_text(json.dumps(runs, indent=1, default=str), encoding="utf-8")
    (OUT / "quality.json").write_text(json.dumps(checks, indent=1, ensure_ascii=False), encoding="utf-8")
    if status == "failed":                       # không đạt kiểm tra: giữ số đúng gần nhất
        return run

    dump = lambda name, obj: (OUT / name).write_text(json.dumps(obj, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    dump("summary.json", {"data_through": end.isoformat(), "kpi": kpi, "channel_mix": mix, "lineage": lineage})
    dump("sku_30d.json", sku)
    dump("store_30d.json", store)
    dump("daily_channel.json", [{"dt": str(d), "channel": c, "net": n} for d, c, n in daily])
    (OUT / "daily_net.svg").write_text(svg_daily(daily), encoding="utf-8")
    update_readme(run, kpi, mix, checks, sku)
    return run


def update_readme(run, kpi, mix, checks, sku):
    readme = HUB / "README.md"
    if not readme.exists():
        return
    icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
    low = [s for s in sku if s["days_cover"] is not None and s["days_cover"] < 14][:5]
    kenh = {"pos": "Cửa hàng", "shopee": "Shopee", "tiktok": "TikTok", "web": "Website"}
    run_at = datetime.fromisoformat(run["run_at"]).strftime("%d/%m/%Y %H:%M")
    thru = date.fromisoformat(run["data_through"]).strftime("%d/%m/%Y")
    lines = [
        "<!-- STATUS:START -->",
        f"**Lần chạy gần nhất:** {run_at} (giờ Việt Nam) · **Dữ liệu đến ngày:** {thru} · "
        f"**Trạng thái:** {'✅ thành công' if run['status'] == 'success' else '❌ thất bại'} · "
        f"**Kiểm tra:** đạt {run['checks_passed']}/{run['checks_total']} · **Thời gian chạy:** {run['seconds']} giây",
        "",
        "| 30 ngày gần nhất | Giá trị |", "|---|---|",
        f"| Doanh thu thuần | {kpi['net_30d']:,.0f} đ |",
        f"| So với 30 ngày trước đó | {kpi['growth']:+.1%} |" if kpi["growth"] is not None else "| So với 30 ngày trước đó | chưa có |",
        f"| Số đơn hàng | {kpi['orders_30d']:,} |",
        f"| Giá trị trung bình mỗi đơn | {kpi['aov_30d']:,} đ |",
        "| Tỷ trọng theo kênh | " + " · ".join(f"{kenh.get(m['channel'], m['channel'])} {m['net'] / kpi['net_30d']:.0%}" for m in mix) + " |",
        "",
        "| Phép kiểm tra | Kết quả | Chi tiết |", "|---|---|---|",
        *[f"| `{c['check']}` | {icon[c['status']]} | {c['detail']} |" for c in checks],
        "",
        "Sắp hết hàng (đủ bán dưới 14 ngày): " + (", ".join(f"{s['product_name']} ({s['days_cover']} ngày)" for s in low) or "không có"),
        "<!-- STATUS:END -->",
    ]
    txt = readme.read_text(encoding="utf-8")
    txt = re.sub(r"<!-- STATUS:START -->.*?<!-- STATUS:END -->", "\n".join(lines).replace("\\", "\\\\"), txt, flags=re.S)
    readme.write_text(txt, encoding="utf-8", newline="\n")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="xoá lake và state, nạp lại từ đầu")
    args = ap.parse_args()
    t0 = time.time()
    if args.rebuild:
        shutil.rmtree(LAKE, ignore_errors=True)
        STATE.unlink(missing_ok=True)
    end = today_vn() - timedelta(days=1)           # hôm qua là ngày cuối cùng đã đủ dữ liệu
    loaded = extract(end)
    removed = prune(end)
    start = end - timedelta(days=90)               # marts chỉ cần 90 ngày -> chỉ đọc các phân vùng đó
    files_total = sum(1 for _ in LAKE.glob("*/dt=*/data.csv"))
    files_read = sum(1 for p in LAKE.glob("*/dt=*/data.csv") if start.isoformat() <= p.parent.name[3:] <= end.isoformat())

    os.chdir(HUB)                                  # dùng đường dẫn tương đối: tên thư mục có "[Python]" làm hỏng mẫu glob
    con = duckdb.connect()
    run_sql(con, "staging.sql", hub=".", start=start, end=end)
    run_sql(con, "marts.sql", end=end)
    checks = quality(con, start, end)
    run = publish(con, end, checks, loaded, removed, time.time() - t0, files_read, files_total)
    print(json.dumps(run, indent=1, default=str))
    for c in checks:
        print(f"  [{c['status']:>4}] {c['check']}: {c['detail']}")
    sys.exit(1 if run["status"] == "failed" else 0)


if __name__ == "__main__":
    main()
