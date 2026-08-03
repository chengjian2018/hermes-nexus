"""
汽车经销商 (4S店) Resolver Wrapper
供 interactive-task-car-sales skill Phase 2 调用。
符合 resolver contract: input dict -> output [{object_id, name, address, phone, extra_info}]

依赖: chinese-poi-search/scripts/amap_poi_tool.py
环境变量: AMAP_API_KEY
"""

import os
import sys
from typing import Dict, List, Any, Optional

# ── 导入 amap_poi_tool（使用绝对路径，避免 ~ 展开问题）──
_AMAP_SCRIPT_DIR = os.path.expanduser(
    "~/.hermes/skills/productivity/chinese-poi-search/scripts"
)
if _AMAP_SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _AMAP_SCRIPT_DIR)

from amap_poi_tool import search_nearby, search_places, search_around, _safe_float


def resolve_car_dealers(
    brand: Optional[str] = None,
    area: Optional[str] = None,
    district: Optional[str] = None,
    place_name: Optional[str] = None,
    location: Optional[str] = None,
    radius: int = 5000,
    car_condition: Optional[str] = None,
    min_rating: Optional[float] = None,
    page: int = 1,
    key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Resolver interface for interactive-task-car-sales Phase 2.
    搜索汽车4S店/经销商，返回标准化对象。

    三种搜索模式 (优先级: place_name > location > area):
      1. 地点名模式(推荐): 用户说"望京""陆家嘴"，自动 geocode + 周边搜索
         - place_name: "望京"、"陆家嘴"
         - radius: 搜索半径(米)，默认 5000 (4S店比餐厅分散，半径更大)
         - area: 可选城市提示，提高 geocode 准确率
      2. 坐标周边模式: 已知经纬度
         - location: "lng,lat"
         - radius: 搜索半径(米)
      3. 城市区域模式: 城市/区县搜索
         - area: 城市名，如"上海"
         - district: 区县名，如"浦东"

    参数:
      brand         品牌名，如"比亚迪"、"丰田"、"宝马"。None=不限品牌
      area          城市名或 adcode
      district      区县名(客户端过滤)
      place_name    地点名(自动 geocode)
      location      坐标 "lng,lat"
      radius        搜索半径(米)，默认 5000
      car_condition "新车" / "二手车" / None。影响搜索关键词
      min_rating    最低评分过滤(客户端)
      page          页码
      key           API Key (默认从 AMAP_API_KEY 环境变量读取)

    返回: [{object_id, name, address, phone, extra_info:{rating, business_area, ...}}]
    失败返回空数组 []，不抛异常。
    """
    try:
        # ── 构建搜索关键词 ──
        keywords_parts = []
        if car_condition == "二手车":
            keywords_parts.append("二手车")
        else:
            # 新车或不限：优先搜 4S 店
            if brand:
                keywords_parts.append(f"{brand}4S店")
            else:
                keywords_parts.append("4S店")
                keywords_parts.append("汽车销售")
        keywords = "|".join(keywords_parts) if keywords_parts else "4S店"

        pois = []
        _geocode_meta = {}

        if place_name:
            # ── 模式1: 地点名周边搜索 ──
            nearby_result = search_nearby(
                place=place_name,
                keywords=keywords,
                radius=radius,
                city=area,
                page=page,
                key=key,
            )
            if not nearby_result.get("success"):
                return []
            pois = nearby_result.get("pois", [])
            _geocode_meta = nearby_result.get("geocode", {})

        elif location:
            # ── 模式2: 坐标周边搜索 ──
            result = search_around(
                location=location,
                keywords=keywords,
                radius=radius,
                page=page,
                key=key,
            )
            if not result.get("success"):
                return []
            pois = result.get("pois", [])

        else:
            # ── 模式3: 城市区域搜索 ──
            result = search_places(
                keywords=keywords,
                city=area or "",
                city_limit=True,
                page=page,
                key=key,
            )
            if not result.get("success"):
                return []
            pois = result.get("pois", [])

        # ── 客户端过滤 ──
        if min_rating is not None:
            pois = [
                p for p in pois
                if p.get("rating") and _safe_float(p["rating"]) >= min_rating
            ]
        if district:
            pois = [
                p for p in pois
                if district in (p.get("adname", "") + p.get("business_area", ""))
            ]

        # ── 标准化输出 ──
        objects = []
        for poi in pois:
            obj = {
                "object_id": poi.get("id", ""),
                "name": poi.get("name", ""),
                "address": poi.get("address", ""),
                "phone": poi.get("tel", ""),
                "extra_info": {
                    "rating": poi.get("rating", ""),
                    "business_area": poi.get("business_area", ""),
                    "opentime_today": poi.get("opentime_today", ""),
                    "opentime_week": poi.get("opentime_week", ""),
                    "tag": poi.get("tag", ""),
                    "location": poi.get("location", ""),
                    "type": poi.get("type", ""),
                    "photos": poi.get("photos", []),
                },
            }
            if brand:
                obj["extra_info"]["search_brand"] = brand
            if car_condition:
                obj["extra_info"]["car_condition"] = car_condition
            if place_name:
                obj["extra_info"]["search_mode"] = "nearby_by_name"
                obj["extra_info"]["search_place"] = place_name
                if _geocode_meta:
                    obj["extra_info"]["geocode"] = _geocode_meta
            elif location:
                obj["extra_info"]["search_mode"] = "nearby"
            objects.append(obj)

        return objects

    except Exception:
        return []


# ── CLI 入口 ──
if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="汽车经销商搜索 (高德 POI)")
    parser.add_argument("mode", choices=["nearby", "area", "around"],
                        help="nearby=地点名周边, area=城市区域, around=坐标周边")
    parser.add_argument("--brand", default=None, help="品牌名")
    parser.add_argument("--place", default=None, help="地点名 (nearby模式)")
    parser.add_argument("--city", default=None, help="城市")
    parser.add_argument("--district", default=None, help="区县")
    parser.add_argument("--location", default=None, help="坐标 lng,lat (around模式)")
    parser.add_argument("--radius", type=int, default=5000, help="搜索半径(米)")
    parser.add_argument("--condition", default=None, choices=["新车", "二手车"],
                        help="新车/二手车")
    parser.add_argument("--min-rating", type=float, default=None)
    parser.add_argument("--page", type=int, default=1)
    args = parser.parse_args()

    if args.mode == "nearby":
        results = resolve_car_dealers(
            brand=args.brand, place_name=args.place, area=args.city,
            radius=args.radius, car_condition=args.condition,
            min_rating=args.min_rating, page=args.page,
        )
    elif args.mode == "area":
        results = resolve_car_dealers(
            brand=args.brand, area=args.city, district=args.district,
            car_condition=args.condition, min_rating=args.min_rating,
            page=args.page,
        )
    else:
        results = resolve_car_dealers(
            brand=args.brand, location=args.location, radius=args.radius,
            car_condition=args.condition, min_rating=args.min_rating,
            page=args.page,
        )

    print(json.dumps(results, ensure_ascii=False, indent=2))
