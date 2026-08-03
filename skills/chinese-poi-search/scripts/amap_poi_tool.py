#!/usr/bin/env python3
"""
高德地图 POI 搜索工具
基于高德开放平台 POI 搜索 API，封装了关键词搜索、周边搜索、详情查询三个接口。

注意：搜索和周边搜索使用 v3 接口（extensions=all），因为 v5 的 show_fields
对个人 key 不返回 rating/cost/tel/business_area 等深度字段。
详情查询使用 v5→v3 两步查询绕过限制。

使用前：
  1. 在 https://lbs.amap.com 注册账号，创建"Web服务"类型应用，获取 API Key
  2. 将 Key 放入环境变量 AMAP_API_KEY，或直接修改下方 DEFAULT_KEY

依赖：
  pip install requests
"""

import os
import json
import requests
from typing import Optional, List, Dict, Any

# ── 配置 ──────────────────────────────────────────────
# 优先从环境变量读取，也可以直接写死在这里
DEFAULT_KEY = os.environ.get("AMAP_API_KEY", "")

# API 端点
# 注意：v5 的 show_fields 对个人 key 不返回 rating/cost 等深度字段，
# 而 v3 的 extensions=all 能返回 biz_ext{rating,cost,open_time,opentime2} + tel + tag + business_area
# 所以搜索和周边搜索走 v3，详情走 v5（v3 无独立详情接口）
URL_PLACE_TEXT   = "https://restapi.amap.com/v3/place/text"     # 关键词搜索 (v3)
URL_PLACE_AROUND = "https://restapi.amap.com/v3/place/around"   # 周边搜索 (v3)
URL_PLACE_DETAIL = "https://restapi.amap.com/v5/place/detail"   # ID查询详情 (v5)
URL_GEOCODE      = "https://restapi.amap.com/v3/geocode/geo"    # 地理编码 (v3)

# 请求超时（秒）
TIMEOUT = 10

# ── POI 类型码速查（常用） ───────────────────────────
# 完整分类码表见 https://lbs.amap.com/api/webservice/download
POI_TYPES = {
    "餐饮服务":     "050000",
    "中餐厅":       "050100",
    "外国餐厅":     "050200",
    "快餐厅":       "050300",
    "茶艺咖啡馆":   "050400",
    "冷饮店":       "050500",
    "购物服务":     "060000",
    "生活服务":     "070000",
    "体育休闲服务": "080000",
    "住宿服务":     "100000",
    "风景名胜":     "110200",
    "电影":         "080600",
    "KTV":          "080900",
    "美容美发":     "071000",
}


def _get_key(key: Optional[str] = None) -> str:
    """获取 API Key"""
    k = key or DEFAULT_KEY
    if not k:
        raise ValueError(
            "缺少高德 API Key。请设置环境变量 AMAP_API_KEY，或在代码中配置 DEFAULT_KEY。\n"
            "申请地址：https://lbs.amap.com -> 控制台 -> 应用管理 -> 创建「Web服务」类型 Key"
        )
    return k


def _parse_poi(poi: Dict[str, Any]) -> Dict[str, Any]:
    """从原始 POI 数据中提取关键字段，统一格式（兼容 v3/v5）"""
    # v3 的深度信息在 biz_ext 里，v5 的在顶层
    biz_ext = poi.get("biz_ext") or {}

    result = {
        "id":            poi.get("id", ""),
        "name":          poi.get("name", ""),
        "type":          poi.get("type", ""),
        "typecode":      poi.get("typecode", ""),
        "address":       poi.get("address", ""),
        "location":      poi.get("location", ""),       # "经度,纬度"
        "tel":           poi.get("tel", ""),
        "pname":         poi.get("pname", ""),           # 省名
        "cityname":      poi.get("cityname", ""),        # 市名
        "adname":        poi.get("adname", ""),          # 区县名
        "business_area": poi.get("business_area", ""),   # 商圈
        "tag":           poi.get("tag", ""),
        # 深度信息：优先 v3 biz_ext，回退 v5 顶层
        "rating":        biz_ext.get("rating", "") or poi.get("rating", ""),
        "cost":          biz_ext.get("cost", "") or poi.get("cost", ""),
        "opentime_today": biz_ext.get("open_time", "") or poi.get("opentime_today", ""),
        "opentime_week":  biz_ext.get("opentime2", "") or poi.get("opentime_week", ""),
        "alias":         poi.get("alias", ""),
        "keytag":        poi.get("keytag", ""),         # v3 特有，主搜索词
        "photos":        [],
    }
    # 处理空列表的情况（v3 经常返回 [] 而非字符串）
    for k in ("tel", "tag", "business_area", "alias", "keytag"):
        if isinstance(result[k], list):
            result[k] = ""

    # 图片列表
    photos = poi.get("photos", [])
    if photos:
        result["photos"] = [
            {"title": (p.get("title") if isinstance(p.get("title"), str) else "") , "url": p.get("url", "")}
            for p in photos if p.get("url")
        ]
    return result


def _format_results(data: Dict[str, Any], raw: bool = False) -> Dict[str, Any]:
    """统一格式化 API 返回"""
    if data.get("status") != "1":
        return {
            "success": False,
            "info": data.get("info", "未知错误"),
            "infocode": data.get("infocode", ""),
        }

    pois = data.get("pois", [])
    result = {
        "success": True,
        "count": data.get("count", "0"),
        "total_pois": len(pois),
        "pois": [_parse_poi(p) for p in pois],
    }
    if raw:
        result["raw"] = data
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  三个核心接口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def search_places(
    keywords: str,
    city: Optional[str] = None,
    city_limit: bool = False,
    types: Optional[str] = None,
    sortrule: str = "weight",
    page: int = 1,
    page_size: int = 20,
    show_fields: Optional[str] = None,
    key: Optional[str] = None,
    raw: bool = False,
) -> Dict[str, Any]:
    """
    关键词搜索 POI（商家/地点）

    使用 v3 接口 + extensions=all 获取深度字段（rating/cost/tel/business_area）。

    参数:
      keywords      搜索关键词，多个用 | 分割，如 "火锅|烤肉"
      city          城市名/citycode/adcode，如 "上海" 或 "021"
      city_limit    True=严格限制在 city 范围内，False=全国搜索（仅加权）
      types         POI类型码，如 "050000"=餐饮，"050100"=中餐厅；多个用 | 分割
      sortrule      排序: "weight"=综合排序(默认), "distance"=按距离
      page          页码，从1开始
      page_size     每页条数，建议<=25
      show_fields   （已废弃，v3 用 extensions=all 自动返回深度字段）
      key           高德API Key（不传则用环境变量）
      raw           True=附带原始API响应

    返回:
      {"success": True, "count": "N", "pois": [...]}
      或 {"success": False, "info": "...", "infocode": "..."}

    示例:
      search_places("火锅", city="上海", city_limit=True, types="050000")
    """
    k = _get_key(key)

    # v3 用 extensions=all 返回深度字段（biz_ext, tel, tag, business_area 等）
    params = {
        "key": k,
        "keywords": keywords,
        "sortrule": sortrule,
        "page": page,
        "offset": page_size,          # v3 用 offset 而非 page_size
        "extensions": "all",
        "output": "json",
    }
    if city:
        params["city"] = city
        params["city_limit"] = "true" if city_limit else "false"
    if types:
        params["types"] = types

    resp = requests.get(URL_PLACE_TEXT, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return _format_results(resp.json(), raw=raw)


def search_around(
    location: str,
    keywords: Optional[str] = None,
    types: Optional[str] = None,
    radius: int = 3000,
    sortrule: str = "distance",
    page: int = 1,
    page_size: int = 20,
    show_fields: Optional[str] = None,
    key: Optional[str] = None,
    raw: bool = False,
) -> Dict[str, Any]:
    """
    周边搜索 POI（以指定经纬度为中心，搜索半径内的商家）

    使用 v3 接口 + extensions=all 获取深度字段。

    参数:
      location      中心点经纬度，格式 "经度,纬度"，如 "121.4737,31.2304"
      keywords      搜索关键词（可选，与 types 二选一）
      types         POI类型码（可选）
      radius        搜索半径（米），0-50000
      sortrule      "distance"=按距离(默认), "weight"=综合排序
      page          页码
      page_size     每页条数
      show_fields   （已废弃，v3 用 extensions=all 自动返回深度字段）
      key           API Key
      raw           附带原始响应

    示例:
      search_around("121.4737,31.2304", keywords="咖啡", radius=1000)
    """
    k = _get_key(key)

    # v3 用 extensions=all
    params = {
        "key": k,
        "location": location,
        "radius": radius,
        "sortrule": sortrule,
        "page": page,
        "offset": page_size,
        "extensions": "all",
        "output": "json",
    }
    if keywords:
        params["keywords"] = keywords
    if types:
        params["types"] = types

    resp = requests.get(URL_PLACE_AROUND, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return _format_results(resp.json(), raw=raw)


def geocode(
    address: str,
    city: Optional[str] = None,
    key: Optional[str] = None,
    raw: bool = False,
) -> Dict[str, Any]:
    """
    地理编码：将结构化地址/地标名称转换为经纬度坐标

    使用高德 v3 地理编码 API。

    参数:
      address       地址或地标名称，如 "北京市朝阳区阜通东大街6号"、"人民广场"、"陆家嘴"
      city          可选，指定城市缩小搜索范围，提高准确率。
                    支持中文名/citycode/adcode。如不传则全国搜索。
      key           API Key
      raw           True=附带原始API响应

    返回:
      成功 -> {"success": True, "count": N, "geocodes": [{location, formatted_address, ...}]}
      失败 -> {"success": False, "info": "...", "infocode": "..."}

    示例:
      geocode("陆家嘴", city="上海")
      geocode("人民广场", city="上海")
      geocode("天安门广场")
    """
    k = _get_key(key)

    params: Dict[str, Any] = {
        "key": k,
        "address": address,
        "output": "json",
    }
    if city:
        params["city"] = city

    try:
        resp = requests.get(URL_GEOCODE, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "1":
            return {
                "success": False,
                "info": data.get("info", "未知错误"),
                "infocode": data.get("infocode", ""),
            }

        geocodes_raw = data.get("geocodes", [])
        geocodes = []
        for g in geocodes_raw:
            geocodes.append({
                "country":           g.get("country", ""),
                "province":          g.get("province", ""),
                "city":              g.get("city", ""),
                "citycode":          g.get("citycode", ""),
                "district":          g.get("district", ""),
                "adcode":            g.get("adcode", ""),
                "street":            g.get("street", ""),
                "number":            g.get("number", ""),
                "location":          g.get("location", ""),        # "经度,纬度"
                "level":             g.get("level", ""),
                "formatted_address": g.get("formatted_address", ""),
            })

        result: Dict[str, Any] = {
            "success": True,
            "count": data.get("count", "0"),
            "geocodes": geocodes,
        }
        if raw:
            result["raw"] = data
        return result
    except Exception as e:
        return {
            "success": False,
            "info": str(e),
            "infocode": "",
        }


def search_nearby(
    place: str,
    keywords: Optional[str] = None,
    types: Optional[str] = None,
    radius: int = 3000,
    city: Optional[str] = None,
    sortrule: str = "distance",
    page: int = 1,
    page_size: int = 20,
    key: Optional[str] = None,
    raw: bool = False,
) -> Dict[str, Any]:
    """
    便捷周边搜索：输入地点名称，自动地理编码 -> 周边搜索

    将 "说一个地点名 + 关键词" 的常见场景简化为一键搜索。
    内部链式调用 geocode() -> search_around()。

    参数:
      place         地点名称/地址，如 "陆家嘴"、"人民广场"、"北京西站"
      keywords      搜索关键词，如 "火锅"、"咖啡"（可选，与 types 二选一）
      types         POI类型码，如 "050000"=餐饮（可选）
      radius        搜索半径（米），默认 3000（3km）
      city          可选，指定城市提高地理编码准确率。可从地址推断。
      sortrule      "distance"=按距离（默认）, "weight"=综合
      page/page_size 分页参数
      key           API Key
      raw           附带原始响应

    返回:
      成功 -> {
        "success": True,
        "geocode": {...},          # 地理编码结果（解析到的坐标和地址）
        "search_center": "lng,lat",# 实际搜索中心点
        "radius": 3000,
        "count": "N",
        "pois": [...]
      }
      地理编码失败 -> {"success": False, "info": "地点解析失败: ..."}

    示例:
      search_nearby("陆家嘴", keywords="火锅", city="上海")             # 默认3km
      search_nearby("人民广场", keywords="咖啡", radius=1000)         # 1km
      search_nearby("北京西站", types="050000")                      # 周边所有餐饮
    """
    # Step 1: 地理编码
    geo_result = geocode(address=place, city=city, key=key)
    if not geo_result.get("success") or not geo_result.get("geocodes"):
        return {
            "success": False,
            "info": f"地点解析失败: {geo_result.get('info', '未找到该地点')}",
            "geocode": geo_result,
        }

    # 取第一个匹配结果
    top = geo_result["geocodes"][0]
    location = top.get("location", "")
    if not location:
        return {
            "success": False,
            "info": f"地点 '{place}' 已找到，但未返回坐标",
            "geocode": geo_result,
        }

    # Step 2: 周边搜索
    search_result = search_around(
        location=location,
        keywords=keywords,
        types=types,
        radius=radius,
        sortrule=sortrule,
        page=page,
        page_size=page_size,
        key=key,
        raw=raw,
    )

    # 附加地理编码信息
    result: Dict[str, Any] = dict(search_result)
    result["geocode"] = {
        "location": location,
        "formatted_address": top.get("formatted_address", ""),
        "province": top.get("province", ""),
        "city": top.get("city", ""),
        "district": top.get("district", ""),
        "adcode": top.get("adcode", ""),
        "level": top.get("level", ""),
        "matches": len(geo_result["geocodes"]),
    }
    result["search_center"] = location
    result["radius"] = radius
    return result


def get_place_detail(
    poi_id: str,
    show_fields: Optional[str] = None,
    key: Optional[str] = None,
    raw: bool = False,
) -> Dict[str, Any]:
    """
    按 POI ID 查询单个商家详情

    实现策略：v5 detail 拿基础信息(name/location) -> v3 around 精确搜回深度字段(rating/cost/tel)
    （原因：v5 的 show_fields 对个人 key 不返回 rating/cost 等深度字段）

    参数:
      poi_id        高德 POI ID（从搜索结果中获得）
      show_fields   额外返回字段（v5，目前对个人key效果有限）
      key           API Key

    示例:
      get_place_detail("B0FFKKRXAZ")
    """
    k = _get_key(key)

    # Step 1: v5 detail 拿基础信息
    v5_fields = "business_area,rating,cost,opentime_today,opentime_week,tel,photos,alias"
    if show_fields:
        v5_fields = show_fields

    params_v5 = {
        "key": k,
        "id": poi_id,
        "show_fields": v5_fields,
    }

    resp = requests.get(URL_PLACE_DETAIL, params=params_v5, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "1":
        return {
            "success": False,
            "info": data.get("info", "未知错误"),
            "infocode": data.get("infocode", ""),
        }

    pois = data.get("pois", [])
    if not pois:
        return {"success": True, "count": "0", "poi": None}

    poi_v5 = pois[0]
    name     = poi_v5.get("name", "")
    location = poi_v5.get("location", "")

    # Step 2: 如果有 location，用 v3 around 精确搜回深度字段
    poi_parsed = _parse_poi(poi_v5)

    if location:
        params_v3 = {
            "key": k,
            "location": location,
            "keywords": name,
            "radius": 50,
            "extensions": "all",
            "output": "json",
            "offset": 1,
        }
        try:
            resp3 = requests.get(URL_PLACE_AROUND, params=params_v3, timeout=TIMEOUT)
            resp3.raise_for_status()
            data3 = resp3.json()
            if data3.get("status") == "1" and data3.get("pois"):
                # v3 结果优先（有完整的 biz_ext/tel/business_area）
                poi_parsed = _parse_poi(data3["pois"][0])
        except Exception:
            pass  # v3 失败就回退用 v5 的数据

    result = {
        "success": True,
        "count": "1",
        "poi": poi_parsed,
    }
    if raw:
        result["raw"] = {"v5": poi_v5}
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  便捷筛选函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def filter_restaurants(
    keywords: str,
    city: str,
    min_rating: Optional[float] = None,
    max_cost: Optional[int] = None,
    district: Optional[str] = None,
    page: int = 1,
    key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    便捷筛选餐厅：搜索后按评分/人均过滤

    参数:
      keywords      关键词如 "火锅"
      city          城市如 "上海"
      min_rating    最低评分（如 4.0）
      max_cost      最高人均（如 100）
      district      区县名如 "浦东新区"（进一步过滤 adname）
      page          页码（注意：客户端过滤会导致每页结果可能不足 page_size）
      key           API Key

    返回:
      {"success": True, "count": N, "pois": [...], "filters": {...}}
    """
    result = search_places(
        keywords=keywords,
        city=city,
        city_limit=True,
        types="050000",
        page=page,
        key=key,
    )

    if not result.get("success"):
        return result

    pois = result["pois"]

    # 客户端过滤
    if min_rating is not None:
        pois = [
            p for p in pois
            if p.get("rating") and _safe_float(p["rating"]) >= min_rating
        ]
    if max_cost is not None:
        pois = [
            p for p in pois
            if p.get("cost") and _safe_float(p["cost"]) <= max_cost
        ]
    if district:
        pois = [p for p in pois if district in (p.get("adname", "") + p.get("business_area", ""))]

    result["pois"] = pois
    result["total_pois"] = len(pois)
    result["filters"] = {
        "min_rating": min_rating,
        "max_cost": max_cost,
        "district": district,
    }
    return result


def _safe_float(val: str) -> float:
    """安全转 float"""
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI 入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _print_table(pois: List[Dict[str, Any]]):
    """终端友好格式输出"""
    if not pois:
        print("  (无结果)")
        return

    for i, p in enumerate(pois, 1):
        rating = p.get("rating", "-")
        cost   = p.get("cost", "-")
        name   = p.get("name", "?")
        addr   = p.get("address", "")
        area   = p.get("business_area", "") or p.get("adname", "")
        tel    = p.get("tel", "")

        print(f"  [{i}] {name}")
        print(f"      评分: {rating}  人均: ¥{cost}  商圈: {area}")
        if addr:
            print(f"      地址: {addr}")
        if tel:
            print(f"      电话: {tel}")
        print()


def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(
        description="高德地图 POI 搜索工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 关键词搜索上海的火锅店
  python %(prog)s search "火锅" --city 上海

  # 以地点名搜周边（自动地理编码，默认3km）
  python %(prog)s nearby "陆家嘴" --keywords "火锅" --city 上海
  python %(prog)s nearby "人民广场" --keywords "咖啡" --radius 1000

  # 搜索浦东评分>=4.5人均<=150的火锅店
  python %(prog)s filter "火锅" --city 上海 --min-rating 4.5 --max-cost 150 --district 浦东

  # 以经纬度搜索周边1km的咖啡店
  python %(prog)s around "121.4752,31.2297" --keywords "咖啡" --radius 1000

  # 查看某个POI详情
  python %(prog)s detail B0FFKKRXAZ
        """,
    )

    sub = parser.add_subparsers(dest="command")

    # search
    p_search = sub.add_parser("search", help="关键词搜索 POI")
    p_search.add_argument("keywords", help="搜索关键词")
    p_search.add_argument("--city", help="城市名")
    p_search.add_argument("--city-limit", action="store_true", help="严格限制在城市内")
    p_search.add_argument("--types", help="POI类型码")
    p_search.add_argument("--page", type=int, default=1)
    p_search.add_argument("--raw", action="store_true")

    # around
    p_around = sub.add_parser("around", help="周边搜索（需提供经纬度）")
    p_around.add_argument("location", help='中心点 "经度,纬度"')
    p_around.add_argument("--keywords", help="搜索关键词")
    p_around.add_argument("--types", help="POI类型码")
    p_around.add_argument("--radius", type=int, default=3000)
    p_around.add_argument("--page", type=int, default=1)

    # nearby (新增)
    p_nearby = sub.add_parser("nearby", help="以地点名搜索周边（自动地理编码，默认3km范围）")
    p_nearby.add_argument("place", help="地点名称，如 '陆家嘴'、'人民广场'")
    p_nearby.add_argument("--keywords", help="搜索关键词，如 '火锅'、'咖啡'")
    p_nearby.add_argument("--types", help="POI类型码，如 050000=餐饮")
    p_nearby.add_argument("--radius", type=int, default=3000, help="搜索半径（米），默认 3000")
    p_nearby.add_argument("--city", help="城市名（可选，提高地点解析准确率）")
    p_nearby.add_argument("--page", type=int, default=1)
    p_nearby.add_argument("--raw", action="store_true")

    # geocode (新增)
    p_geocode = sub.add_parser("geocode", help="地理编码：地址/地名 → 经纬度")
    p_geocode.add_argument("address", help="地址或地标名称，如 '陆家嘴'、'北京市朝阳区阜通东大街6号'")
    p_geocode.add_argument("--city", help="城市名（可选，缩小搜索范围）")
    p_geocode.add_argument("--raw", action="store_true")

    # detail
    p_detail = sub.add_parser("detail", help="POI详情")
    p_detail.add_argument("poi_id", help="POI ID")

    # filter
    p_filter = sub.add_parser("filter", help="筛选餐厅")
    p_filter.add_argument("keywords", help="搜索关键词")
    p_filter.add_argument("--city", required=True)
    p_filter.add_argument("--min-rating", type=float)
    p_filter.add_argument("--max-cost", type=int)
    p_filter.add_argument("--district", help="区县名")
    p_filter.add_argument("--page", type=int, default=1)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "search":
        result = search_places(
            keywords=args.keywords,
            city=args.city,
            city_limit=args.city_limit,
            types=args.types,
            page=args.page,
            raw=args.raw,
        )
    elif args.command == "around":
        result = search_around(
            location=args.location,
            keywords=args.keywords,
            types=args.types,
            radius=args.radius,
            page=args.page,
        )
    elif args.command == "detail":
        result = get_place_detail(poi_id=args.poi_id)
    elif args.command == "geocode":
        result = geocode(
            address=args.address,
            city=args.city,
            raw=args.raw,
        )
    elif args.command == "nearby":
        result = search_nearby(
            place=args.place,
            keywords=args.keywords,
            types=args.types,
            radius=args.radius,
            city=args.city,
            page=args.page,
            raw=args.raw,
        )
    elif args.command == "filter":
        result = filter_restaurants(
            keywords=args.keywords,
            city=args.city,
            min_rating=args.min_rating,
            max_cost=args.max_cost,
            district=args.district,
            page=args.page,
        )
    else:
        parser.print_help()
        return

    if args.command == "detail":
        if result.get("success"):
            poi = result["poi"]
            _print_table([poi])
        else:
            print(f"错误: {result.get('info')}")
    elif args.command == "geocode":
        if result.get("success"):
            for i, g in enumerate(result["geocodes"], 1):
                print(f"  [{i}] {g.get('formatted_address', '?')}")
                print(f"      坐标: {g.get('location', '?')}  |  级别: {g.get('level', '?')}")
                print(f"      省: {g.get('province', '')}  市: {g.get('city', '')}")
                print(f"      区: {g.get('district', '')}  adcode: {g.get('adcode', '')}")
                print()
        else:
            print(f"错误: {result.get('info')}")
    elif result.get("success"):
        # 如果是 nearby 搜索，展示地理编码信息
        geocode_info = result.get("geocode")
        if geocode_info:
            print(f"📍 {geocode_info.get('formatted_address', '?')}"
                  f"  [{geocode_info.get('location', '?')}，{result.get('radius', '?')}m 范围]")
            if geocode_info.get("matches", 1) > 1:
                print(f"   (已自动选择第 1 个匹配项，共 {geocode_info['matches']} 个)\n")
            else:
                print()
        print(f"\n共 {result.get('count', '?')} 条结果，当前页 {len(result['pois'])} 条:\n")
        _print_table(result["pois"])
    else:
        print(f"错误: {result.get('info')} (code: {result.get('infocode')})")

    if args.raw:
        print("\n--- raw ---")
        print(json.dumps(result.get("raw", result), indent=2, ensure_ascii=False))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Resolver Wrapper
#  供 interactive-task / interactive-task-food skill Phase 2 调用。
#  符合 resolver contract: input dict -> output [{object_id, name, ...}]
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def resolve_restaurants(
    cuisine: str,
    area: Optional[str] = None,
    party_size: Optional[int] = None,
    min_rating: Optional[float] = None,
    max_cost: Optional[int] = None,
    district: Optional[str] = None,
    location: Optional[str] = None,
    radius: Optional[int] = None,
    place_name: Optional[str] = None,
    page: int = 1,
    key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Resolver interface for interactive-task skill Phase 2.
    Searches restaurants and returns standardized objects.

    Three search modes (priority: place_name > location > area):
      1. Place name mode (recommended): user says a place, auto geocode + nearby search
         - place_name: "左家庄"、"陆家嘴"、"人民广场"
         - radius: search radius in meters (default 3000)
         - area: optional city hint for more accurate geocoding
      2. Nearby mode: search by coordinates + radius
         - location: "lng,lat" e.g. "121.4752,31.2297"
         - radius: search radius in meters (0-50000)
      3. Area mode (default): search by city/district name
         - area: city name or adcode, e.g. "上海" or "310115"
         - district: optional district name for client-side filtering

    Returns empty list [] on failure (never raises).
    """
    try:
        if place_name:
            # 地点名模式：geocode -> around（最简单）
            nearby_result = search_nearby(
                place=place_name,
                keywords=cuisine,
                radius=radius or 3000,
                city=area,          # area 作为 geocode 的城市提示
                types="050000",
                page=page,
                key=key,
            )
            if not nearby_result.get("success"):
                return []
            pois = nearby_result.get("pois", [])
            # 客户端过滤 rating/cost
            if min_rating is not None:
                pois = [p for p in pois if p.get("rating") and _safe_float(p["rating"]) >= min_rating]
            if max_cost is not None:
                pois = [p for p in pois if p.get("cost") and _safe_float(p["cost"]) <= max_cost]
            if district:
                pois = [p for p in pois if district in (p.get("adname", "") + p.get("business_area", ""))]
            # 附加 geocode 信息
            _geocode_meta = nearby_result.get("geocode", {})
        elif location:
            # 周边搜索模式
            result = search_around(
                location=location,
                keywords=cuisine,
                radius=radius or 3000,
                types="050000",
                page=page,
                key=key,
            )
            if not result.get("success"):
                return []
            pois = result.get("pois", [])
            # 客户端过滤 rating/cost（around 不支持服务端过滤）
            if min_rating is not None:
                pois = [p for p in pois if p.get("rating") and _safe_float(p["rating"]) >= min_rating]
            if max_cost is not None:
                pois = [p for p in pois if p.get("cost") and _safe_float(p["cost"]) <= max_cost]
            if district:
                pois = [p for p in pois if district in (p.get("adname", "") + p.get("business_area", ""))]
            _geocode_meta = {}
        else:
            # 区域搜索模式
            result = filter_restaurants(
                keywords=cuisine,
                city=area or "",
                min_rating=min_rating,
                max_cost=max_cost,
                district=district,
                page=page,
                key=key,
            )
            if not result.get("success"):
                return []
            pois = result.get("pois", [])

        objects = []
        for poi in pois:
            obj = {
                "object_id": poi.get("id", ""),
                "name":      poi.get("name", ""),
                "address":   poi.get("address", ""),
                "phone":     poi.get("tel", ""),
                "extra_info": {
                    "rating":        poi.get("rating", ""),
                    "cost":          poi.get("cost", ""),
                    "business_area": poi.get("business_area", ""),
                    "opentime_today": poi.get("opentime_today", ""),
                    "opentime_week":  poi.get("opentime_week", ""),
                    "tag":           poi.get("tag", ""),
                    "location":      poi.get("location", ""),
                    "type":          poi.get("type", ""),
                    "photos":        poi.get("photos", []),
                },
            }
            if party_size is not None:
                obj["extra_info"]["party_size"] = party_size
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


if __name__ == "__main__":
    main()
