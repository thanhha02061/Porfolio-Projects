"""Retail Data Hub - daily pipeline.

    extract (incremental)  ->  lake/ (raw, partitioned by day)
    transform (SQL)        ->  staging views -> fct_sales / fct_inventory -> marts
    quality gate           ->  stop publishing if any check fails
    publish                ->  output/*.json, output/daily_net.svg, README status block

Run:  python pipeline/run.py            (normal daily run)
      python pipeline/run.py --rebuild  (drop the lake and backfill from scratch)
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
BACKFILL_DAYS = 120          # first run loads this much history
RETENTION_DAYS = 120         # raw partitions older than this are removed
RECON_TOLERANCE = 0.005      # hub vs source daily total may differ by at most 0.5%


def today_vn() -> date:
    return datetime.now(VN).date()


# ------------------------------------------------------------------ 1. EXTRACT
def extract(end: date) -> dict:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    loaded = {}
    for name, fetch in SOURCES.items():
        last = date.fromisoformat(state[name]) if name in state else end - timedelta(days=BACKFILL_DAYS)
        days, d = [], last + timedelta(days=1)
        while d <= end:                                   # only days not loaded yet
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


# ------------------------------------------------------------------ 3. QUALITY GATE
def quality(con, start: date, end: date) -> list[dict]:
    checks = []

    def add(name, ok, detail, severity="fail"):
        checks.append({"check": name, "status": "pass" if ok else severity, "detail": detail})

    # freshness: every source delivered yesterday's data
    for s in SOURCES:
        ok = (LAKE / s / f"dt={end.isoformat()}" / "data.csv").exists()
        add(f"freshness_{s}", ok, f"partition {end} {'found' if ok else 'missing'}")

    # no zero / negative quantities
    bad = con.sql("SELECT count(*) FROM fct_sales WHERE qty <= 0").fetchone()[0]
    add("valid_quantity", bad == 0, f"{bad} rows with qty <= 0")

    # unmapped product codes: warn if any, fail if they carry > 1% of revenue
    unm = con.sql("""SELECT count(*), coalesce(sum(net),0),
                            (SELECT sum(net) FROM fct_sales WHERE is_valid),
                            string_agg(DISTINCT channel || ':' || source_sku, ', ')
                     FROM fct_sales WHERE is_valid AND master_sku IS NULL""").fetchone()
    share = (unm[1] / unm[2]) if unm[2] else 0
    add("sku_mapping", share <= 0.01,
        f"{unm[0]} rows ({share:.2%} of revenue) unmapped" + (f": {unm[3]}" if unm[0] else ""),
        severity="warn" if share <= 0.01 else "fail")
    if unm[0] and share <= 0.01:
        checks[-1]["status"] = "warn"

    # reconciliation: hub net (mapped + unmapped) = each system's own daily report
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
    add("reconciliation", fails == 0, f"worst daily gap {worst:.3%} vs source reports; {fails} day(s) over {RECON_TOLERANCE:.1%}")

    # inventory snapshot complete: every location x active sku
    got, exp = con.sql("""SELECT count(*), (SELECT count(DISTINCT location) FROM fct_inventory) *
                          (SELECT count(*) FROM sku_master WHERE launch_date::DATE <= DATE '{e}')
                          FROM fct_inventory""".replace("{e}", end.isoformat())).fetchone()
    add("inventory_complete", got == exp, f"{got}/{exp} location-SKU rows")
    return checks


# ------------------------------------------------------------------ 4. PUBLISH
def svg_daily(rows: list[tuple]) -> str:
    """Small stacked-area chart of daily net revenue by channel, embedded in the README."""
    chans = [("pos", "#2563eb", "Stores"), ("shopee", "#f97316", "Shopee"), ("tiktok", "#111827", "TikTok"), ("web", "#10b981", "Website")]
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
    if status == "failed":                       # quality gate: keep last good numbers
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
    lines = [
        "<!-- STATUS:START -->",
        f"**Last run:** {run['run_at']} (Vietnam time) · **Data through:** {run['data_through']} · "
        f"**Status:** {'✅ success' if run['status'] == 'success' else '❌ failed'} · "
        f"**Checks:** {run['checks_passed']}/{run['checks_total']} passed · **Runtime:** {run['seconds']} s",
        "",
        "| Last 30 days | Value |", "|---|---|",
        f"| Net revenue | {kpi['net_30d']:,.0f} VND |",
        f"| vs previous 30 days | {kpi['growth']:+.1%} |" if kpi["growth"] is not None else "| vs previous 30 days | n/a |",
        f"| Orders | {kpi['orders_30d']:,} |",
        f"| Average order value | {kpi['aov_30d']:,} VND |",
        "| Channel mix | " + " · ".join(f"{m['channel']} {m['net'] / kpi['net_30d']:.0%}" for m in mix) + " |",
        "",
        "| Quality check | Result | Detail |", "|---|---|---|",
        *[f"| `{c['check']}` | {icon[c['status']]} | {c['detail']} |" for c in checks],
        "",
        "Low stock (under 14 days of cover): " + (", ".join(f"{s['product_name']} ({s['days_cover']} d)" for s in low) or "none"),
        "<!-- STATUS:END -->",
    ]
    txt = readme.read_text(encoding="utf-8")
    txt = re.sub(r"<!-- STATUS:START -->.*?<!-- STATUS:END -->", "\n".join(lines).replace("\\", "\\\\"), txt, flags=re.S)
    readme.write_text(txt, encoding="utf-8", newline="\n")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="delete lake and state, backfill again")
    args = ap.parse_args()
    t0 = time.time()
    if args.rebuild:
        shutil.rmtree(LAKE, ignore_errors=True)
        STATE.unlink(missing_ok=True)
    end = today_vn() - timedelta(days=1)           # yesterday is the last complete day
    loaded = extract(end)
    removed = prune(end)
    start = end - timedelta(days=90)               # marts only need 90 days -> read only those partitions
    files_total = sum(1 for _ in LAKE.glob("*/dt=*/data.csv"))
    files_read = sum(1 for p in LAKE.glob("*/dt=*/data.csv") if start.isoformat() <= p.parent.name[3:] <= end.isoformat())

    os.chdir(HUB)                                  # relative paths: folder names like "[Python]" would break glob patterns
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
