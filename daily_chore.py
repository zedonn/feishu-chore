#!/usr/bin/env python3
"""
每日家务任务推送脚本（每天5条滑动窗口版，纯飞书 OpenAPI）

逻辑：
- 每天保持最多5条"是否今日"=true的待办任务
- 智能补充：今天完成几条，明天就从后面补几条，没完成就原样保留
- 补充时跳过「完成」=true的任务（已完成的这一轮不再回来）
- 一轮完成判定提前：所有任务都完成时，先清空全部「完成」标记开启新一轮，再补充
- 序号原位重排：按【Grid View（全部记录）】的行顺序把序号改写成1,2,3...
  （你经常增删改移动任务，每天重排一次保持编号连续）
- 显示顺序以「今日任务」视图为准：图和推送消息里任务的排列顺序
  = 你在该视图里看到的行顺序，所见即所得；找不到该视图则回退为按序号排序
- 完成记录：检测到打钩完成的任务时，自动写入「完成记录」表（表不存在则自动创建）
- 支持 --check-only 高频检查模式（无新补充时静默退出）
- 支持 --reset 重置模式
- 防重复保险：推送成功后记录当天日期，同一天再次触发直接退出
- 墨水屏图片为表格式版面：具体描述居左、大区域和小区域靠右对齐，
  三列超宽自动截断加…；无照片、无花哨元素，5行任务+表头+时间戳

环境变量（必填）：
  LARK_APP_ID      飞书自建应用的 App ID
  LARK_APP_SECRET  飞书自建应用的 App Secret

环境变量（可选，有默认值）：
  LARK_BASE_TOKEN          多维表格 token
  LARK_TASK_TABLE_ID       家务任务表 ID
  LARK_CONFIG_TABLE_ID     系统配置表 ID
  LARK_DEFAULT_VIEW_ID     默认视图 ID（Grid View，序号重排和任务池扫描用）
  LARK_TODAY_VIEW_NAME     今日视图名字（显示顺序以它为准），默认「今日任务」
  LARK_USER_OPEN_ID        推送目标用户的 open_id
  CHORE_DAILY_COUNT        每天待办数量，默认5
"""

import io
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

# ============ 配置 ============
BASE_TOKEN = os.environ.get("LARK_BASE_TOKEN", "GIgLbeJDUadS17sreyNcX7jknoe")
TASK_TABLE_ID = os.environ.get("LARK_TASK_TABLE_ID", "tblOo1DKyKgs0CV4")
CONFIG_TABLE_ID = os.environ.get("LARK_CONFIG_TABLE_ID", "tbl5WaTKn591sLJ6")
DEFAULT_VIEW_ID = os.environ.get("LARK_DEFAULT_VIEW_ID", "vewATu0DaX")
TODAY_VIEW_NAME = os.environ.get("LARK_TODAY_VIEW_NAME", "今日任务")
USER_OPEN_ID = os.environ.get("LARK_USER_OPEN_ID", "ou_487b71f46f00d88bbaf1862a0ee1639d")
DAILY_COUNT = int(os.environ.get("CHORE_DAILY_COUNT", "5"))

APP_ID = os.environ.get("LARK_APP_ID", "")
APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
API_BASE = "https://open.feishu.cn/open-apis"
BEIJING_TZ = timezone(timedelta(hours=8))
LOG_TABLE_NAME = "完成记录"


# ============ 飞书 API 基础封装 ============
_tenant_token = None
_token_expire = 0


def get_tenant_token():
    """获取 tenant_access_token，带缓存"""
    global _tenant_token, _token_expire
    if _tenant_token and time.time() < _token_expire - 60:
        return _tenant_token
    if not APP_ID or not APP_SECRET:
        print("错误：未设置 LARK_APP_ID / LARK_APP_SECRET 环境变量")
        sys.exit(1)
    resp = requests.post(
        f"{API_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        print(f"获取 token 失败: {data}")
        sys.exit(1)
    _tenant_token = data["tenant_access_token"]
    _token_expire = time.time() + data.get("expire", 7200)
    return _tenant_token


def api_request(method, path, params=None, json_body=None):
    """统一的飞书 API 请求，失败时打印详细信息"""
    token = get_tenant_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    url = f"{API_BASE}{path}"
    resp = requests.request(
        method, url, headers=headers, params=params, json=json_body, timeout=30
    )
    try:
        result = resp.json()
    except Exception:
        print(f"❌ API 返回非 JSON [{method} {path}]")
        print(f"   HTTP状态码: {resp.status_code}")
        print(f"   请求体: {json.dumps(json_body, ensure_ascii=False)[:300] if json_body else '无'}")
        print(f"   响应前500字: {resp.text[:500]}")
        return {"code": -1, "msg": "非JSON响应"}
    if result.get("code") != 0:
        print(f"❌ API错误 [{method} {path}]")
        print(f"   code: {result.get('code')}, msg: {result.get('msg')}")
        print(f"   请求体: {json.dumps(json_body, ensure_ascii=False)[:300] if json_body else '无'}")
    return result


# ============ 记录操作 ============
def list_records(table_id, view_id=None, page_size=200):
    """列出记录，自动分页，返回 [{record_id, fields}, ...]；给 view_id 时返回该视图的顺序"""
    all_items = []
    page_token = None
    while True:
        params = {"page_size": page_size}
        if view_id:
            params["view_id"] = view_id
        if page_token:
            params["page_token"] = page_token
        result = api_request(
            "GET",
            f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records",
            params=params,
        )
        if result.get("code") != 0:
            break
        data = result.get("data", {})
        all_items.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    return [{"record_id": item["record_id"], "fields": item.get("fields", {})}
            for item in all_items]


def update_record(table_id, record_id, fields):
    """更新记录的字段"""
    return api_request(
        "PUT",
        f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}",
        json_body={"fields": fields},
    )


def batch_update_records(table_id, updates):
    """批量更新：单包最多450条自动分包；整包被拒时降级逐行更新定位问题行"""
    if not updates:
        return
    CHUNK = 450
    for start in range(0, len(updates), CHUNK):
        chunk = updates[start:start + CHUNK]
        body = {"records": [{"record_id": rid, "fields": f} for rid, f in chunk]}
        result = api_request(
            "POST",
            f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/batch_update",
            json_body=body,
        )
        if result.get("code") == 0:
            continue
        print(f"⚠️ 批量更新被拒（{len(chunk)} 条），降级为逐行更新以定位问题行")
        for rid, f in chunk:
            r = update_record(table_id, rid, f)
            if r.get("code") != 0:
                print(f"   ❌ 问题行 record_id={rid}，已跳过，其余行不受影响")


def list_fields(table_id):
    """列出表的所有字段"""
    result = api_request(
        "GET",
        f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields",
        params={"page_size": 100},
    )
    if result.get("code") == 0:
        return result.get("data", {}).get("items", [])
    return []


def send_text_message(open_id, text):
    """给用户发送文本消息"""
    content = json.dumps({"text": text}, ensure_ascii=False)
    result = api_request(
        "POST",
        "/im/v1/messages",
        params={"receive_id_type": "open_id"},
        json_body={"receive_id": open_id, "msg_type": "text", "content": content},
    )
    return result.get("code") == 0


# ============ 业务逻辑 ============
def extract_field_value(fields, field_name):
    """从 fields 字典中提取字段值，兼容多种类型"""
    val = fields.get(field_name)
    if val is None:
        return ""
    if isinstance(val, list):
        if not val:
            return ""
        if isinstance(val[0], dict):
            return val[0].get("text", str(val[0]))
        return str(val[0])
    if isinstance(val, bool):
        return val
    return str(val)


def get_seq(t):
    """安全获取序号，返回int"""
    val = extract_field_value(t["fields"], "序号")
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def is_valid_task(fields):
    """有效任务判定：「小区域」「参考图片」「具体区域描述」任意一个有内容即算有效"""
    for name in ("小区域", "参考图片", "具体区域描述"):
        val = fields.get(name)
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        if isinstance(val, list) and not val:
            continue
        return True
    return False


def get_task_display_name(fields):
    """任务显示名：优先小区域，其次具体区域描述，都没有则提示看参考图片"""
    small = extract_field_value(fields, "小区域")
    if small:
        return small
    desc = extract_field_value(fields, "具体区域描述")
    if desc:
        return desc
    return "（见表格参考图片）"


def get_all_tasks_in_order():
    """通过默认视图（Grid View）获取所有任务，顺序 = 全部记录视图的行顺序"""
    return list_records(TASK_TABLE_ID, view_id=DEFAULT_VIEW_ID)


def find_view_id_by_name(table_id, target_name):
    """按名字查找视图 ID，找不到返回 None"""
    result = api_request(
        "GET",
        f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/views",
        params={"page_size": 100},
    )
    if result.get("code") == 0:
        for v in result.get("data", {}).get("items", []):
            if v.get("view_name") == target_name:
                return v.get("view_id")
    return None


def order_tasks_by_view(tasks, view_id):
    """把任务按指定视图的显示顺序排列（该视图里第几行就排第几）"""
    if not view_id:
        tasks.sort(key=get_seq)
        return
    ordered = list_records(TASK_TABLE_ID, view_id=view_id)
    rank = {r["record_id"]: i for i, r in enumerate(ordered)}
    tasks.sort(key=lambda t: rank.get(t["record_id"], 10_000))


def get_config():
    """读取系统配置表，返回 {配置项: 值}"""
    records = list_records(CONFIG_TABLE_ID)
    config = {}
    for item in records:
        key = extract_field_value(item["fields"], "配置项")
        val = extract_field_value(item["fields"], "值")
        config[str(key)] = str(val)
    return config


def set_config(key, value):
    """更新配置项；配置表里还没有这一项时，自动创建该行"""
    records = list_records(CONFIG_TABLE_ID)
    for item in records:
        k = extract_field_value(item["fields"], "配置项")
        if str(k) == key:
            update_record(CONFIG_TABLE_ID, item["record_id"], {"值": str(value)})
            return True
    result = api_request(
        "POST",
        f"/bitable/v1/apps/{BASE_TOKEN}/tables/{CONFIG_TABLE_ID}/records",
        json_body={"fields": {"配置项": key, "值": str(value)}},
    )
    return result.get("code") == 0


def reset_all_flags(tasks):
    """重置模式：清空所有记录的「是否今日」和「完成」标记"""
    updates = []
    for t in tasks:
        u = {}
        if extract_field_value(t["fields"], "是否今日") is True:
            u["是否今日"] = False
        if extract_field_value(t["fields"], "完成") is True:
            u["完成"] = False
        if u:
            updates.append((t["record_id"], u))
    batch_update_records(TASK_TABLE_ID, updates)
    print(f"🔄 重置完成：清除了 {len(updates)} 条记录的标记")


def renumber_by_row_order(tasks):
    """序号原位重排：按 Grid View 行顺序把序号改写成 1,2,3...（条目位置不变）"""
    seq_field = None
    for f in list_fields(TASK_TABLE_ID):
        if f.get("field_name") == "序号":
            seq_field = f
            break
    if seq_field and seq_field.get("ui_type") == "AutoNumber":
        print("⚠️ 「序号」是【自动编号】字段，API 无法修改，已跳过重排")
        return tasks

    updates = []
    changes = []
    for i, t in enumerate(tasks):
        expected_seq = i + 1
        current_seq = get_seq(t)
        if current_seq != expected_seq:
            updates.append((t["record_id"], {"序号": expected_seq}))
            changes.append(f"{current_seq or '空'}→{expected_seq}")
    if updates:
        batch_update_records(TASK_TABLE_ID, updates)
        print(f"序号原位重排完成：批量更新了 {len(updates)} 条（条目位置不变）")
        print(f"   变化明细: {', '.join(changes)}")
    else:
        print("序号已是连续排列，无需调整")
    return tasks


def get_or_create_log_table():
    """查找「完成记录」表，不存在则自动创建，返回 table_id"""
    result = api_request(
        "GET",
        f"/bitable/v1/apps/{BASE_TOKEN}/tables",
        params={"page_size": 100},
    )
    if result.get("code") == 0:
        for tb in result.get("data", {}).get("items", []):
            if tb.get("name") == LOG_TABLE_NAME:
                return tb.get("table_id")

    body = {
        "table": {
            "name": LOG_TABLE_NAME,
            "default_view_name": "全部记录",
            "fields": [
                {"field_name": "完成日期", "type": 5, "property": {"date_formatter": "yyyy/MM/dd"}},
                {"field_name": "大区域", "type": 1},
                {"field_name": "小区域", "type": 1},
                {"field_name": "具体区域描述", "type": 1},
            ],
        }
    }
    result = api_request("POST", f"/bitable/v1/apps/{BASE_TOKEN}/tables", json_body=body)
    if result.get("code") == 0:
        table_id = result.get("data", {}).get("table_id")
        print(f"📝 已自动创建「{LOG_TABLE_NAME}」表: {table_id}")
        return table_id
    return None


def log_completed_tasks(done_tasks):
    """把本次检测到完成的任务写入「完成记录」表"""
    if not done_tasks:
        return
    table_id = get_or_create_log_table()
    if not table_id:
        print("⚠️ 完成记录表不可用，跳过记录")
        return
    now = datetime.now(BEIJING_TZ)
    day_start = datetime(now.year, now.month, now.day, tzinfo=BEIJING_TZ)
    date_ts = int(day_start.timestamp() * 1000)
    records = []
    for t in done_tasks:
        small = extract_field_value(t["fields"], "小区域")
        desc = extract_field_value(t["fields"], "具体区域描述")
        if not small and not desc:
            small = "（见表格参考图片）"
        records.append({
            "fields": {
                "完成日期": date_ts,
                "大区域": extract_field_value(t["fields"], "大区域"),
                "小区域": small,
                "具体区域描述": desc,
            }
        })
    result = api_request(
        "POST",
        f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/batch_create",
        json_body={"records": records},
    )
    if result.get("code") == 0:
        print(f"📝 已写入 {len(records)} 条完成记录（{now.strftime('%Y-%m-%d')}）")
    else:
        print("⚠️ 完成记录写入失败")


def send_message(task_info):
    """组装并发送今日任务消息（顺序与墨水屏图片一致）"""
    total_today = task_info.get("total_today", 0)
    new_count = task_info.get("new_count", 0)
    remaining_count = task_info.get("remaining_count", 0)
    tasks = task_info.get("tasks", [])
    new_task_ids = task_info.get("new_task_ids", set())

    lines = [
        "🧹 今日家务任务",
        "━━━━━━━━━━━━━━━",
        f"📋 共 {total_today} 条待办（新增 {new_count} 条，延续 {remaining_count} 条）",
        "",
    ]
    new_tasks = [t for t in tasks if t["record_id"] in new_task_ids]
    if new_tasks:
        lines.append("🆕 新增：")
        for t in new_tasks:
            area = extract_field_value(t["fields"], "大区域")
            name = get_task_display_name(t["fields"])
            lines.append(f"  • [{area}] {name}")
        lines.append("")

    remaining_tasks = [t for t in tasks if t["record_id"] not in new_task_ids]
    if remaining_tasks:
        lines.append("🔄 延续（昨日未完成）：")
        for t in remaining_tasks:
            area = extract_field_value(t["fields"], "大区域")
            name = get_task_display_name(t["fields"])
            lines.append(f"  • [{area}] {name}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━")
    lines.append("完成后请在多维表格中勾选「完成」✅")
    lines.append(f"多维表格链接：https://my.feishu.cn/base/{BASE_TOKEN}")

    return send_text_message(USER_OPEN_ID, "\n".join(lines))


# ============ 墨水屏图片渲染（表格式版面） ============

SCREEN_W, SCREEN_H = 400, 300
EINK_OUTPUT_DIR = "docs"
EINK_OUTPUT_FILE = os.path.join(EINK_OUTPUT_DIR, "today.png")


def _find_font():
    """找中文字体文件（按优先级依次尝试）"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "NotoSansCJKsc-Regular.otf"),
        os.path.join(script_dir, "assets", "NotoSansCJKsc-Regular.otf"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _truncate(draw, text, font, max_w):
    """把文字限制在 max_w 像素内，超宽截断加…，返回处理后的文字"""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _draw_right(draw, text, font, col_left, col_w, y, fill):
    """在列内右对齐绘制文字（超宽先截断），列右缘 = col_left + col_w"""
    text = _truncate(draw, text, font, col_w)
    x = col_left + col_w - draw.textlength(text, font=font)
    draw.text((x, y), text, font=font, fill=fill)


def render_today_image(today_tasks):
    """400x300 墨水屏图：具体描述居左，大区域/小区域靠右对齐，超宽截断"""
    from PIL import Image, ImageDraw, ImageFont

    font_path = _find_font()
    if not font_path:
        print("⚠️ 找不到中文字体文件，跳过墨水屏图片生成（不影响飞书推送）")
        return

    index = 2 if font_path.endswith(".ttc") else 0
    img = Image.new("RGB", (SCREEN_W, SCREEN_H), "white")
    draw = ImageDraw.Draw(img)

    f_title = ImageFont.truetype(font_path, 24, index=index)
    f_head = ImageFont.truetype(font_path, 16, index=index)
    f_row = ImageFont.truetype(font_path, 16, index=index)
    f_small = ImageFont.truetype(font_path, 12, index=index)

    RED = (190, 30, 30)
    BLACK = (30, 30, 30)
    GRAY = (150, 150, 150)
    LIGHT = (210, 210, 210)

    # ---- 列几何：按原型图比例，小区域列贴屏幕右缘 ----
    m = 5        # 两侧边距
    gap = 6      # 列间距
    col_w1 = 216  # 具体描述
    col_w2 = 70   # 大区域
    col_w3 = 92   # 小区域
    x1 = m
    x2 = x1 + col_w1 + gap
    x3 = x2 + col_w2 + gap   # x3 + col_w3 = 395 = 右缘

    # ---- 标题条（红色）----
    now = datetime.now(BEIJING_TZ)
    draw.rectangle([0, 0, SCREEN_W, 44], fill=RED)
    draw.text((12, 8), f"今日家务 {now.month}月{now.day}日",
              font=f_title, fill="white")

    # ---- 表头（灰字，与数据列对齐）----
    hy = 52
    draw.text((x1, hy), "具体描述", font=f_head, fill=GRAY)
    _draw_right(draw, "大区域", f_head, x2, col_w2, hy, GRAY)
    _draw_right(draw, "小区域", f_head, x3, col_w3, hy, GRAY)
    draw.line([m, 74, SCREEN_W - m, 74], fill=LIGHT, width=1)

    # ---- 任务行：均分剩余空间，行间浅灰分隔线 ----
    top, bottom = 78, SCREEN_H - 20
    rows = today_tasks[:5]
    n = len(rows)
    if n == 0:
        draw.text((x1, top + 20), "今天没有待办任务，好好休息！",
                  font=f_row, fill=BLACK)
    else:
        row_h = (bottom - top) / n
        for i, t in enumerate(rows):
            y = round(top + i * row_h)
            cy = y + round(row_h / 2) - 10
            fields = t["fields"]

            desc = extract_field_value(fields, "具体区域描述")
            if not desc:
                desc = get_task_display_name(fields)
            draw.text((x1, cy), _truncate(draw, desc, f_row, col_w1),
                      font=f_row, fill=BLACK)

            area = extract_field_value(fields, "大区域")
            if area:
                _draw_right(draw, area, f_row, x2, col_w2, cy, BLACK)

            small = extract_field_value(fields, "小区域")
            if small:
                _draw_right(draw, small, f_row, x3, col_w3, cy, BLACK)

            if i < n - 1:
                draw.line([m, y + round(row_h), SCREEN_W - m, y + round(row_h)],
                          fill=LIGHT, width=1)

    # ---- 右下角时间戳（排查趣联缓存用）----
    ts = "更新于 " + now.strftime("%m-%d %H:%M")
    draw.text((SCREEN_W - m - draw.textlength(ts, font=f_small), SCREEN_H - 16),
              ts, font=f_small, fill=GRAY)

    os.makedirs(EINK_OUTPUT_DIR, exist_ok=True)
    img.save(EINK_OUTPUT_FILE, "PNG")
    print(f"🖼️ 已生成墨水屏图片: {EINK_OUTPUT_FILE}（共 {len(today_tasks)} 条任务）")


def main():
    check_only = "--check-only" in sys.argv
    reset_mode = "--reset" in sys.argv
    mode = "高频检查" if check_only else "每日推送"
    if reset_mode:
        mode += "+重置"
    print(f"=== 每日家务任务推送（{mode}模式，每天{DAILY_COUNT}条滑动窗口）===")

    # 0. 找「今日任务」视图：显示顺序（图+推送消息）以它为准
    today_view_id = find_view_id_by_name(TASK_TABLE_ID, TODAY_VIEW_NAME)
    if today_view_id:
        print(f"显示顺序按「{TODAY_VIEW_NAME}」视图排列（所见即所得）")
    else:
        print(f"⚠️ 找不到名为「{TODAY_VIEW_NAME}」的视图，回退为按序号排序")

    # 0.5 防重复保险：今天已推送过就直接退出（图片照样刷新，方便白天测试）
    today_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    if not check_only and not reset_mode:
        config = get_config()
        if config.get("上次推送日期") == today_str:
            print(f"✅ 今天（{today_str}）已经推送过了，本次跳过推送")
            try:
                tasks_now = get_all_tasks_in_order()
                today_now = [t for t in tasks_now
                             if is_valid_task(t["fields"])
                             and extract_field_value(t["fields"], "是否今日") is True]
                order_tasks_by_view(today_now, today_view_id)
                render_today_image(today_now)
            except Exception as e:
                print(f"⚠️ 墨水屏图片生成失败: {e}")
            return

    # 1. 获取所有任务（Grid View 行顺序），过滤空记录
    tasks = get_all_tasks_in_order()
    total = len(tasks)
    valid_tasks = [t for t in tasks if is_valid_task(t["fields"])]
    print(f"当前任务总数: {total}，有效任务: {len(valid_tasks)}")
    if len(valid_tasks) == 0:
        print("没有有效任务，无法推送")
        return

    # 2. 重置模式
    if reset_mode:
        reset_all_flags(tasks)
        tasks = get_all_tasks_in_order()
        valid_tasks = [t for t in tasks if is_valid_task(t["fields"])]

    # 3. 每日推送模式：序号按 Grid View 行顺序原位重排
    if not check_only:
        renumber_by_row_order(valid_tasks)
        tasks = get_all_tasks_in_order()
        valid_tasks = [t for t in tasks if is_valid_task(t["fields"])]

    # 4. 清理空记录残留标记 + 统计当前待办
    valid_ids = {t["record_id"] for t in valid_tasks}
    all_today = [t for t in tasks if extract_field_value(t["fields"], "是否今日") is True]
    stale_today = [t for t in all_today if t["record_id"] not in valid_ids]
    if stale_today:
        print(f"⚠️ 发现 {len(stale_today)} 条空记录残留「是否今日」标记，自动清理")
        batch_update_records(
            TASK_TABLE_ID,
            [(t["record_id"], {"是否今日": False}) for t in stale_today]
        )
    today_tasks = [t for t in all_today if t["record_id"] in valid_ids]
    print(f"当前待办数: {len(today_tasks)}")

    # 5. 待办超过 DAILY_COUNT 条时清理多余
    if len(today_tasks) > DAILY_COUNT:
        print(f"⚠️ 待办数超过{DAILY_COUNT}条，清理多余的")
        today_tasks.sort(key=get_seq)
        batch_update_records(
            TASK_TABLE_ID,
            [(t["record_id"], {"是否今日": False}) for t in today_tasks[DAILY_COUNT:]],
        )
        today_tasks = today_tasks[:DAILY_COUNT]

    # 6. 统计已完成的，移出待办
    done_tasks = [t for t in today_tasks if extract_field_value(t["fields"], "完成") is True]
    remaining_tasks = [t for t in today_tasks if extract_field_value(t["fields"], "完成") is not True]
    print(f"已完成: {len(done_tasks)}条，延续: {len(remaining_tasks)}条")

    # 6.1 写入完成记录
    log_completed_tasks(done_tasks)
    if done_tasks:
        batch_update_records(
            TASK_TABLE_ID,
            [(t["record_id"], {"是否今日": False}) for t in done_tasks]
        )

    # 7. 一轮完成判定（提前到补充之前）
    if all(extract_field_value(t["fields"], "完成") is True for t in valid_tasks):
        print("🎉 完成一轮循环，清空所有任务的完成状态，开启新一轮")
        batch_update_records(
            TASK_TABLE_ID,
            [(t["record_id"], {"完成": False}) for t in valid_tasks]
        )
        tasks = get_all_tasks_in_order()
        valid_tasks = [t for t in tasks if is_valid_task(t["fields"])]

    # 8. 计算补充数量
    need_to_add = max(0, DAILY_COUNT - len(remaining_tasks))
    print(f"需要补充: {need_to_add}条")

    # 9. 按 Grid View 行顺序补充新任务（任务池顺序）
    new_tasks = []
    if need_to_add > 0:
        today_ids = {t["record_id"] for t in today_tasks}
        remaining_ids = {t["record_id"] for t in remaining_tasks}
        if remaining_ids:
            max_index = max(i for i, t in enumerate(valid_tasks) if t["record_id"] in remaining_ids)
        elif today_tasks:
            max_index = max(i for i, t in enumerate(valid_tasks) if t["record_id"] in today_ids)
        else:
            max_index = -1

        candidates = []
        n = len(valid_tasks)
        for offset in range(1, n + 1):
            idx = (max_index + offset) % n
            t = valid_tasks[idx]
            if t["record_id"] in today_ids:
                continue
            if extract_field_value(t["fields"], "完成") is True:
                continue
            candidates.append(t)
            if len(candidates) >= need_to_add:
                break
        new_tasks = candidates[:need_to_add]
        batch_update_records(
            TASK_TABLE_ID,
            [(t["record_id"], {"是否今日": True}) for t in new_tasks]
        )
        print(f"已补充: {len(new_tasks)}条")
        if len(new_tasks) < need_to_add:
            print(f"⚠️ 未完成的候选任务不足，本次只补了 {len(new_tasks)} 条")

    # 10. 重新获取最终待办，按「今日任务」视图顺序排列（所见即所得）
    tasks = get_all_tasks_in_order()
    valid_tasks = [t for t in tasks if is_valid_task(t["fields"])]
    final_today = [t for t in valid_tasks if extract_field_value(t["fields"], "是否今日") is True]
    order_tasks_by_view(final_today, today_view_id)
    task_info = {
        "total_today": len(final_today),
        "new_count": len(new_tasks),
        "remaining_count": len(remaining_tasks),
        "tasks": final_today,
        "new_task_ids": {t["record_id"] for t in new_tasks},
    }
    print(f"最终待办: {len(final_today)}条（新增{len(new_tasks)}条，延续{len(remaining_tasks)}条）")
    print("显示顺序: " + " → ".join(
        f"{i+1}.{extract_field_value(t['fields'], '具体区域描述') or extract_field_value(t['fields'], '小区域') or '（见图）'}"
        for i, t in enumerate(final_today)
    ))

    # 10.1 渲染墨水屏图片（失败不影响推送）
    try:
        render_today_image(final_today)
    except Exception as e:
        print(f"⚠️ 墨水屏图片生成失败（不影响飞书推送）: {e}")

    # 高频检查模式：无新补充则静默退出
    if check_only and len(new_tasks) == 0:
        print("高频检查：无新补充任务，静默退出")
        return

    success = send_message(task_info)
    print(f"消息推送: {'成功' if success else '失败'}")
    if success and not check_only:
        if set_config("上次推送日期", today_str):
            print(f"📌 已记录推送日期: {today_str}（今天再触发将自动跳过）")
        else:
            print("⚠️ 推送日期写入配置表失败（不影响本次推送）")
    print("=== 执行完成 ===")


if __name__ == "__main__":
    main()
