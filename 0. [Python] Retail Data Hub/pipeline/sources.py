"""Simulated source systems.

Each function returns what a real connector would receive from that system for one
business day (Vietnam time): a list of rows in the system's *native* format plus the
system's own daily total. The formats differ on purpose - date formats, time zones,
SKU codes, discount fields - because that is exactly the mess a data hub has to absorb.

All data is fictional (brand "Lumière"). Output is deterministic: the same date always
produces the same rows, so the pipeline can be re-run safely.
"""
from __future__ import annotations

import csv
import hashlib
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HUB = Path(__file__).resolve().parents[1]
VN = timezone(timedelta(hours=7))

CHANNEL_SHARE = {"pos": 0.45, "shopee": 0.25, "tiktok": 0.20, "web": 0.10}
DAILY_UNITS = 900  # chain-wide units on a normal weekday


def _load(name: str) -> list[dict]:
    with open(HUB / "master" / name, encoding="utf-8") as f:
        return list(csv.DictReader(f))


SKUS = _load("sku_master.csv")
STORES = _load("store_master.csv")


def _rng(source: str, d: date) -> random.Random:
    seed = int(hashlib.md5(f"{source}|{d.isoformat()}".encode()).hexdigest()[:12], 16)
    return random.Random(seed)


def _demand_factor(d: date, online: bool) -> float:
    f = 1.0 + 0.0012 * (d - date(2026, 1, 1)).days          # slow growth
    if d.weekday() >= 5:
        f *= 1.18                                          # weekend
    if d.day >= 25 or d.day <= 3:
        f *= 1.08                                          # payday window
    if online and d.day == d.month:
        f *= 2.6                                           # 9.9, 10.10, 11.11 mega sale
    return f


def _sku_weight(sku: dict, d: date) -> float:
    if d < date.fromisoformat(sku["launch_date"]):
        return 0.0
    base = {"Serum": 1.4, "Sunscreen": 1.3, "Cleanser": 1.2, "Mask": 1.6}.get(sku["category"], 1.0)
    return base * (1.1 if sku["category"] == "Sunscreen" and d.month in (3, 4, 5, 6) else 1.0)


def _orders(source: str, d: date):
    """Yield (order_no, [(sku, qty, unit_price, discount)]) for one channel and day."""
    r = _rng(source, d)
    online = source != "pos"
    units_target = DAILY_UNITS * CHANNEL_SHARE[source] * _demand_factor(d, online) * r.uniform(0.9, 1.1)
    weights = [_sku_weight(s, d) for s in SKUS]
    units, n = 0, 0
    while units < units_target:
        n += 1
        lines = []
        for _ in range(r.choices([1, 2, 3], [0.6, 0.3, 0.1])[0]):
            sku = r.choices(SKUS, weights)[0]
            qty = r.choices([1, 2, 3], [0.8, 0.15, 0.05])[0]
            price = int(sku["list_price"])
            disc_rate = r.choice([0, 0, 0.05, 0.1, 0.15]) if not (online and d.day == d.month) else r.choice([0.15, 0.2, 0.25])
            lines.append((sku, qty, price, round(price * qty * disc_rate, -2)))
            units += qty
        yield n, lines


def _ts(d: date, r: random.Random) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=VN) + timedelta(minutes=r.randint(8 * 60, 22 * 60))


# ---------------------------------------------------------------- POS (KiotViet-style)
def pos_sales(d: date):
    r, rows = _rng("pos-ts", d), []
    for n, lines in _orders("pos", d):
        store = r.choice(STORES)["store_code"]
        ts = _ts(d, r)
        for sku, qty, price, disc in lines:
            rows.append({"MaHD": f"HD{d:%y%m%d}{n:05d}", "NgayBan": ts.strftime("%d/%m/%Y %H:%M"),
                         "MaCH": store, "MaHang": sku["pos_code"], "SL": qty, "DonGia": price, "GiamGia": int(disc)})
    return rows


# ---------------------------------------------------------------- Shopee (epoch UTC, cancellations, API page overlap)
def shopee_orders(d: date):
    r, rows = _rng("shopee-ts", d), []
    for n, lines in _orders("shopee", d):
        ts = _ts(d, r).astimezone(timezone.utc)
        status = "CANCELLED" if r.random() < 0.06 else "COMPLETED"
        for sku, qty, price, disc in lines:
            rows.append({"order_sn": f"26{d:%m%d}SP{n:05d}", "create_time": int(ts.timestamp()),
                         "item_sku": sku["shopee_sku"], "quantity": qty, "original_price": price,
                         "seller_discount": int(disc), "order_status": status})
    if rows and r.random() < 0.5:                  # API pagination returns a few rows twice
        rows += rows[-3:]
    return rows


# ---------------------------------------------------------------- TikTok Shop (ISO UTC, inconsistent SKU casing, bundles)
def tiktok_orders(d: date):
    r, rows = _rng("tiktok-ts", d), []
    for n, lines in _orders("tiktok", d):
        ts = _ts(d, r).astimezone(timezone.utc)
        status = "CANCELLED" if r.random() < 0.08 else "DELIVERED"
        for sku, qty, price, disc in lines:
            code = sku["tiktok_sku"]
            code = code.upper() if r.random() < 0.15 else code
            platform = round(price * qty * 0.05, -2) if r.random() < 0.3 else 0
            rows.append({"order_id": f"57{d:%m%d}{n:06d}", "created_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "seller_sku": code, "qty": qty, "sku_unit_original_price": price,
                         "seller_discount": int(disc), "platform_discount": int(platform), "order_status": status})
    if r.random() < 0.25:                          # livestream bundle not yet in master data
        rows.append({"order_id": f"57{d:%m%d}999999", "created_at": _ts(d, r).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "seller_sku": "lum_live_combo_09", "qty": 1, "sku_unit_original_price": 499000,
                     "seller_discount": 50000, "platform_discount": 0, "order_status": "DELIVERED"})
    return rows


# ---------------------------------------------------------------- Website (ISO +07:00, barcode as SKU)
def web_orders(d: date):
    r, rows = _rng("web-ts", d), []
    for n, lines in _orders("web", d):
        ts = _ts(d, r)
        paid = "voided" if r.random() < 0.04 else "paid"
        for sku, qty, price, disc in lines:
            rows.append({"id": f"W{d:%y%m%d}{n:04d}", "created_on": ts.isoformat(), "variant_code": sku["barcode"],
                         "qty": qty, "price": price, "discount_amount": int(disc), "financial_status": paid})
    return rows


# ---------------------------------------------------------------- ERP inventory snapshot (end of day)
def erp_inventory(d: date):
    rows = []
    for loc in [s["store_code"] for s in STORES] + ["WH-ONLINE"]:
        for i, sku in enumerate(SKUS):
            if d < date.fromisoformat(sku["launch_date"]):
                continue
            cycle = 21 if loc == "WH-ONLINE" else 14
            age = (d.toordinal() + i * 3 + len(loc)) % cycle
            cap = 900 if loc == "WH-ONLINE" else 60
            on_hand = max(0, int(cap * (1 - age / (cycle * (0.85 + (i % 5) * 0.08)))))
            rows.append({"product_code": sku["erp_code"], "location": loc, "snapshot_date": d.isoformat(),
                         "on_hand": on_hand, "avg_cost": sku["unit_cost"]})
    return rows


# ---------------------------------------------------------------- each system's own daily report (for reconciliation)
def control_total(source: str, rows: list[dict]) -> dict:
    if source == "pos":
        net = sum(x["SL"] * x["DonGia"] - x["GiamGia"] for x in rows)
    elif source == "shopee":
        seen, net = set(), 0
        for x in rows:
            k = tuple(sorted(x.items()))  # the system's own report counts each unique line once
            if x["order_status"] == "COMPLETED" and k not in seen:
                net += x["quantity"] * x["original_price"] - x["seller_discount"]
            seen.add(k)
    elif source == "tiktok":
        net = sum(x["qty"] * x["sku_unit_original_price"] - x["seller_discount"] for x in rows if x["order_status"] == "DELIVERED")
    elif source == "web":
        net = sum(x["qty"] * x["price"] - x["discount_amount"] for x in rows if x["financial_status"] == "paid")
    else:
        net = 0
    return {"source": source, "net_revenue": int(net)}


SOURCES = {"pos": pos_sales, "shopee": shopee_orders, "tiktok": tiktok_orders, "web": web_orders, "erp": erp_inventory}
