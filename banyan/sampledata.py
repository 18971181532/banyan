"""内置示例数据：一个电商星型模型。

刻意做成**不完全独立**的分布，因为优化器最容易在这里翻车：

* `city` 与 `province` 天然相关（省份是城市的上卷），把两个条件 AND 起来时
  独立相乘会严重低估命中率；
* `segment` 与下单量相关（VIP 和企业客户下单更频繁），过滤客户表再连订单表
  时，`1/max(ndv)` 的均匀假设会低估连接结果；
* 商品类目分布倾斜，正好用来验证 MCV 有没有被用上。

数据由固定种子生成，同一份代码在任何机器上跑出来的行数与统计信息都一致。
"""

from __future__ import annotations

import csv
import os
import random
from typing import Dict, List, Sequence, Tuple

from .catalog import Column, Database, Table
from .values import FLOAT, INT, TEXT

# 城市 → 省份 → 大区，真实的上卷关系
CITIES: Tuple[Tuple[str, str, str], ...] = (
    ("上海", "上海", "华东"), ("北京", "北京", "华北"), ("广州", "广东", "华南"),
    ("深圳", "广东", "华南"), ("佛山", "广东", "华南"), ("杭州", "浙江", "华东"),
    ("宁波", "浙江", "华东"), ("温州", "浙江", "华东"), ("南京", "江苏", "华东"),
    ("苏州", "江苏", "华东"), ("无锡", "江苏", "华东"), ("成都", "四川", "西南"),
    ("重庆", "重庆", "西南"), ("武汉", "湖北", "华中"), ("长沙", "湖南", "华中"),
    ("西安", "陕西", "西北"), ("天津", "天津", "华北"), ("青岛", "山东", "华东"),
    ("济南", "山东", "华东"), ("郑州", "河南", "华中"), ("沈阳", "辽宁", "东北"),
    ("大连", "辽宁", "东北"), ("哈尔滨", "黑龙江", "东北"), ("长春", "吉林", "东北"),
    ("合肥", "安徽", "华东"), ("福州", "福建", "华南"), ("厦门", "福建", "华南"),
    ("南昌", "江西", "华中"), ("昆明", "云南", "西南"), ("贵阳", "贵州", "西南"),
    ("南宁", "广西", "华南"), ("石家庄", "河北", "华北"), ("太原", "山西", "华北"),
    ("呼和浩特", "内蒙古", "华北"), ("兰州", "甘肃", "西北"), ("西宁", "青海", "西北"),
    ("银川", "宁夏", "西北"), ("乌鲁木齐", "新疆", "西北"), ("海口", "海南", "华南"),
    ("拉萨", "西藏", "西南"),
)

# 类目占比：图书一家独大，乐器极少——倾斜的分布才检验得出 MCV
CATEGORIES: Tuple[Tuple[str, float], ...] = (
    ("图书", 0.34), ("数码", 0.20), ("服饰", 0.16), ("食品", 0.12),
    ("家居", 0.10), ("美妆", 0.06), ("乐器", 0.02),
)

SEGMENTS: Tuple[Tuple[str, float], ...] = (
    ("普通", 0.78), ("VIP", 0.12), ("企业", 0.10),
)

# 各客户类型的下单权重：VIP 下单频率约为普通客户的 3 倍
SEGMENT_WEIGHT: Dict[str, float] = {"普通": 1.0, "VIP": 3.0, "企业": 2.2}

STATUSES: Tuple[Tuple[str, float], ...] = (
    ("已完成", 0.72), ("已取消", 0.10), ("待付款", 0.12), ("退款中", 0.06),
)

CHANNELS: Tuple[str, ...] = ("App", "小程序", "Web", "门店")

SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
GIVEN = "伟芳娜秀英敏静丽强磊洋艳勇军杰娟涛明超秀霞平刚桂英"


def build(scale: float = 1.0, seed: int = 20240501) -> Database:
    """造一个可以直接查询的库。`scale` 用来整体放大规模。"""
    rng = random.Random(seed)
    customers_n = max(20, int(2000 * scale))
    products_n = max(10, int(2000 * scale))
    orders_n = max(20, int(20000 * scale))
    items_n = max(20, int(60000 * scale))

    regions = _regions()
    customers, weights = _customers(rng, customers_n)
    products = _products(rng, products_n)
    orders = _orders(rng, orders_n, customers, weights)
    items = _items(rng, items_n, orders, products)

    db = Database()
    db.add(regions)
    db.add(customers)
    db.add(products)
    db.add(orders)
    db.add(items)
    return db


# ---------------------------------------------------------------------------
# 各表
# ---------------------------------------------------------------------------
def _regions() -> Table:
    seen = {}
    for city, province, zone in CITIES:
        seen[city] = (city, province, zone)
    rows = [[city, province, zone] for city, province, zone in seen.values()]
    return Table("regions", [Column("city", TEXT), Column("province", TEXT),
                             Column("zone", TEXT)], rows)


# 各客户类型的授信额度分布：类型和额度相关，正好用来演示"谓词相关"
SEGMENT_CREDIT: Dict[str, Tuple[float, float]] = {
    "普通": (8000, 1800), "VIP": (20000, 4500), "企业": (42000, 9000),
}


def _customers(rng: random.Random, count: int):
    columns = [
        Column("id", INT), Column("name", TEXT), Column("city", TEXT),
        Column("segment", TEXT), Column("signup_date", TEXT), Column("credit_limit", INT),
    ]
    rows: List[List[object]] = []
    weights: List[float] = []
    for index in range(1, count + 1):
        city = rng.choice(CITIES)[0]
        segment = _weighted(rng, SEGMENTS)
        name = rng.choice(SURNAMES) + "".join(rng.choice(GIVEN) for _ in range(rng.randint(1, 2)))
        year = rng.randint(2019, 2024)
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        mean, spread = SEGMENT_CREDIT[segment]
        limit = int(rng.gauss(mean, spread))
        rows.append([index, name, city, segment, f"{year:04d}-{month:02d}-{day:02d}",
                     max(1000, limit)])
        weights.append(SEGMENT_WEIGHT[segment])
    return Table("customers", columns, rows), weights


def _products(rng: random.Random, count: int):
    columns = [
        Column("id", INT), Column("name", TEXT), Column("category", TEXT),
        Column("cost", FLOAT), Column("list_price", FLOAT),
    ]
    rows: List[List[object]] = []
    for index in range(1, count + 1):
        category = _weighted(rng, CATEGORIES)
        cost = round(rng.uniform(8, 900), 2)
        rows.append([index, f"{category}-{index:05d}", category, cost,
                     round(cost * rng.uniform(1.3, 2.2), 2)])
    return Table("products", columns, rows)


def _orders(rng: random.Random, count: int, customers: Table, weights: Sequence[float]):
    columns = [
        Column("id", INT), Column("customer_id", INT), Column("order_date", TEXT),
        Column("status", TEXT), Column("channel", TEXT), Column("amount", FLOAT),
    ]
    ids = [row[0] for row in customers.rows]
    picks = rng.choices(range(len(ids)), weights=weights, k=count)
    rows: List[List[object]] = []
    for index, pick in enumerate(picks, start=1):
        year = rng.choices((2023, 2024), weights=(0.45, 0.55))[0]
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        rows.append([
            index, ids[pick], f"{year:04d}-{month:02d}-{day:02d}",
            _weighted(rng, STATUSES), rng.choice(CHANNELS),
            round(rng.uniform(20, 6000), 2),
        ])
    return Table("orders", columns, rows)


def _items(rng: random.Random, count: int, orders: Table, products: Table):
    columns = [
        Column("id", INT), Column("order_id", INT), Column("product_id", INT),
        Column("quantity", INT), Column("unit_price", FLOAT), Column("discount", FLOAT),
    ]
    order_ids = [row[0] for row in orders.rows]
    prices = {row[0]: row[4] for row in products.rows}
    product_ids = list(prices)
    # 商品被买到的概率按价格反向加权：便宜的书卖得更多
    weights = [1.0 / (1.0 + prices[pid] / 120.0) for pid in product_ids]
    rows: List[List[object]] = []
    for index in range(1, count + 1):
        order_id = order_ids[min(int(rng.expovariate(1.0 / max(len(order_ids) / 3, 1))),
                                 len(order_ids) - 1)]
        product_id = rng.choices(product_ids, weights=weights, k=1)[0]
        quantity = rng.choices((1, 2, 3, 5), weights=(0.6, 0.25, 0.1, 0.05))[0]
        discount = rng.choice((1.0, 1.0, 1.0, 0.95, 0.9, 0.85))
        rows.append([index, order_id, product_id, quantity,
                     round(prices[product_id] * discount, 2), round(1 - discount, 2)])
    return Table("order_items", columns, rows)


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def write_csvs(directory: str, scale: float = 1.0, seed: int = 20240501) -> List[str]:
    """把示例数据写成 CSV，供 `--data` 加载。"""
    os.makedirs(directory, exist_ok=True)
    db = build(scale=scale, seed=seed)
    written = []
    for table in db.tables.values():
        path = os.path.join(directory, f"{table.name}.csv")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([column.name for column in table.columns])
            for row in table.rows:
                writer.writerow(["" if value is None else value for value in row])
        written.append(path)
    return written


def _weighted(rng: random.Random, choices: Sequence[Tuple[str, float]]) -> str:
    return rng.choices([item[0] for item in choices],
                       weights=[item[1] for item in choices], k=1)[0]
