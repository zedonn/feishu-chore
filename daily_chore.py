#!/usr/bin/env python3
"""
每日家务任务推送脚本（每天5条滑动窗口版，纯飞书 OpenAPI）

逻辑：
- 每天保持最多5条"是否今日"=true的待办任务
- 运行时检查：已完成的移出待办，按顺序补充新任务使总数回到5条
- 所有任务完成一轮后，清空所有"完成"状态，重新开始
- 支持 --check-only 高频检查模式（无新补充时静默退出）

环境变量（必填）：
  LARK_APP_ID      飞书自建应用的 App ID
  LARK_APP_SECRET  飞书自建应用的 App Secret

环境变量（可选，有默认值）：
  LARK_BASE_TOKEN     多维表格 token
  LARK_TASK_TABLE_ID  家务任务表 ID
  LARK_CONFIG_TABLE_ID 系统配置表 ID
  LARK_DEFAULT_VIEW_ID 默认视图 ID
  LARK_AREA_FIELD_ID  大区域字段 ID
  LARK_USER_OPEN_ID   推送目标用户的 open_id
  CHORE_DAILY_COUNT   每天待办数量，默认5
"""
import json
import os
import sys
import time
import requests

# ============ 配置 ============
BASE_TOKEN = os.environ.get("LARK_BASE_TOKEN", "GIgLbeJDUadS17sreyNcX7jknoe")
TASK_TABLE_ID = os.environ.get("LARK_TASK_TABLE_ID", "tblOo1DKyKgs0CV4")
CONFIG_TABLE_ID = os.environ.get("LARK_CONFIG_TABLE_ID", "tbl5WaTKn591sLJ6")
DEFAULT_VIEW_ID = os.environ.get("LARK_DEFAULT_VIEW_ID", "vewATu0DaX")
AREA_FIELD_ID = os.environ.get("LARK_AREA_FIELD_ID", "fldYh8CiMt")
USER_OPEN_ID = os.environ.get("LARK_USER_OPEN_ID", "ou_487b71f46f00d88bbaf1862a0ee1639d")
DAILY_COUNT = int(os.environ.get("CHORE_DAILY_COUNT", "5"))

APP_ID = os.environ.get("LARK_APP_ID", "")
APP_SECRET = os.environ.get("LARK_APP_SECRET", "")

API_BASE = "https://open.feishu.cn/open-apis"

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
        print(f"  HTTP状态码: {resp.status_code}")
        print(f"  请求体: {json.dumps(json_body, ensure_ascii=False)[:300] if json_body else '无'}")
        print(f"  响应前500字: {resp.text[:500]}")
        return {"code": -1, "msg": "非JSON响应"}
    if result.get("code") != 0:
        print(f"❌ API错误 [{method} {path}]")
        print(f"  code: {result.get('code')}, msg: {result.get('msg')}")
        print(f"  请求体: {json.dumps(json_body, ensure_ascii=False)[:300] if json_body else '无'}")
    return result


# ============ 记录操作 ============
def list_records(table_id, view_id=None, page_size=200):
    """列出记录，自动分页，返回 [{record_id, fields}, ...]"""
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
    return [{"record_id": item["record_id"], "fields": item.get("fields", {})} for item in all_items]


def update_record(table_id, record_id, fields):
    """更新记录的字段"""
    return api_request(
        "PUT",
        f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}",
        json_body={"fields": fields},
    )


def get_field(table_id, field_id):
    """获取字段详情（含单选选项）。飞书没有单字段获取API，用列出所有字段再筛选。"""
    result = api_request(
        "GET",
        f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields",
        params={"page_size": 100},
    )
    if result.get("code") == 0:
        items = result.get("data", {}).get("items", [])
        for item in items:
            if item.get("field_id") == field_id:
                return item
    return {}


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


def get_all_tasks_in_order():
    """通过默认视图获取所有任务，返回顺序 = 表格视觉行顺序"""
    return list_records(TASK_TABLE_ID, view_id=DEFAULT_VIEW_ID)


def get_config():
    """读取系统配置表，返回 {配置项: 值}"""
    records = list_records(CONFIG_TABLE_ID)
    config = {}
    for item in records:
        key = extract_field_value(item["fields"], "配置项")
        val = extract_field_value(item["fields"], "值")
        config[str(key)] = str(val)
    return config


def update_config(key, value):
    """更新系统配置表中的某一项"""
    records = list_records(CONFIG_TABLE_ID)
    for item in records:
        k = extract_field_value(item["fields"], "配置项")
        if str(k) == key:
            update_record(CONFIG_TABLE_ID, item["record_id"], {"值": str(value)})
            return True
    return False


def update_task(record_id, fields):
    """更新家务任务记录"""
    return update_record(TASK_TABLE_ID, record_id, fields)


def get_area_order():
    """动态读取「大区域」单选字段的选项顺序"""
    field = get_field(TASK_TABLE_ID, AREA_FIELD_ID)
    options = field.get("property", {}).get("options", [])
    if not options:
        options = field.get("options", [])
    return [opt.get("name", "") for opt in options if opt.get("name")]


def reorganize_and_renumber(tasks):
    """
    按「大区域」单选字段的选项顺序重新排列任务，并重排序号为 1,2,3...
    """
    area_order = get_area_order()
    area_priority = {name: i for i, name in enumerate(area_order)}
    print(f"读取到区域选项顺序: {' → '.join(area_order) if area_order else '(获取失败)'}")

    def sort_key(t):
        area = extract_field_value(t["fields"], "大区域")
        priority = area_priority.get(area, len(area_order) + 1)
        return (priority, area)

    tasks.sort(key=sort_key)

    updated = 0
    for i, t in enumerate(tasks):
        expected_seq = i + 1
        current_seq = get_seq(t)
        if current_seq != expected_seq:
            update_task(t["record_id"], {"序号": expected_seq})
            updated += 1

    if updated > 0:
        print(f"按区域重排并更新序号：调整了 {updated} 条")
    else:
        print("序号已是按区域排列，无需调整")
    return tasks


def send_message(task_info):
    """组装并发送今日任务消息（支持多条滑动窗口）"""
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
            small = extract_field_value(t["fields"], "小区域") or "(未填写)"
            lines.append(f"  • [{area}] {small}")
        lines.append("")

    remaining_tasks = [t for t in tasks if t["record_id"] not in new_task_ids]
    if remaining_tasks:
        lines.append("🔄 延续（昨日未完成）：")
        for t in remaining_tasks:
            area = extract_field_value(t["fields"], "大区域")
            small = extract_field_value(t["fields"], "小区域") or "(未填写)"
            lines.append(f"  • [{area}] {small}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━")
    lines.append("完成后请在多维表格中勾选「完成」✅")
    lines.append(f"多维表格链接：https://my.feishu.cn/base/{BASE_TOKEN}")

    message = "\n".join(lines)
    return send_text_message(USER_OPEN_ID, message)


def main():
    check_only = "--check-only" in sys.argv
    mode = "高频检查" if check_only else "每日推送"
    print(f"=== 每日家务任务推送（{mode}模式，每天{DAILY_COUNT}条滑动窗口）===")

    # 1. 获取所有任务
    tasks = get_all_tasks_in_order()
    total = len(tasks)
    print(f"当前任务总数: {total}")

    if total == 0:
        print("任务表为空，无法推送")
        return

    # 2. 每日推送模式：按区域重排并更新序号
    if not check_only:
        tasks = reorganize_and_renumber(tasks)
        tasks = get_all_tasks_in_order()

    # 3. 找出当前待办（是否今日=true）
    today_tasks = [t for t in tasks if extract_field_value(t["fields"], "是否今日") is True]
    print(f"当前待办数: {len(today_tasks)}")

    # 4. 如果待办超过DAILY_COUNT条，清理多余的（历史残留）
    if len(today_tasks) > DAILY_COUNT:
        print(f"⚠️ 待办数超过{DAILY_COUNT}条，清理多余的")
        today_tasks.sort(key=get_seq)
        for t in today_tasks[DAILY_COUNT:]:
            update_task(t["record_id"], {"是否今日": False})
        today_tasks = today_tasks[:DAILY_COUNT]

    # 5. 统计已完成的，移出待办
    done_tasks = [t for t in today_tasks if extract_field_value(t["fields"], "完成") is True]
    remaining_tasks = [t for t in today_tasks if extract_field_value(t["fields"], "完成") is not True]
    print(f"已完成: {len(done_tasks)}条，延续: {len(remaining_tasks)}条")

    for t in done_tasks:
        update_task(t["record_id"], {"是否今日": False})

    # 6. 计算需要补充多少条
    need_to_add = DAILY_COUNT - len(remaining_tasks)
    if need_to_add < 0:
        need_to_add = 0
    print(f"需要补充: {need_to_add}条")

    # 7. 按顺序补充新任务
    new_tasks = []
    if need_to_add > 0:
        if today_tasks:
            max_seq = max(get_seq(t) for t in today_tasks)
            # 先从序号 > max_seq 的任务中选
            candidates = [t for t in tasks if get_seq(t) > max_seq]
            # 不够的话从开头补充（循环）
            candidates += [t for t in tasks if get_seq(t) <= max_seq]
        else:
            candidates = tasks

        today_ids = {t["record_id"] for t in today_tasks}
        candidates = [t for t in candidates if t["record_id"] not in today_ids]

        new_tasks = candidates[:need_to_add]
        for t in new_tasks:
            update_task(t["record_id"], {"是否今日": True})
        print(f"已补充: {len(new_tasks)}条")

    # 8. 检查是否一轮完成（所有任务的完成都是true）
    all_done = all(extract_field_value(t["fields"], "完成") is True for t in tasks)
    if all_done:
        print("🎉 完成一轮循环，清空所有任务的完成状态")
        for t in tasks:
            update_task(t["record_id"], {"完成": False})

    # 9. 重新获取当前待办，发送消息
    tasks = get_all_tasks_in_order()
    final_today = [t for t in tasks if extract_field_value(t["fields"], "是否今日") is True]
    final_today.sort(key=get_seq)

    task_info = {
        "total_today": len(final_today),
        "new_count": len(new_tasks),
        "remaining_count": len(remaining_tasks),
        "tasks": final_today,
        "new_task_ids": {t["record_id"] for t in new_tasks},
    }

    print(f"最终待办: {len(final_today)}条（新增{len(new_tasks)}条，延续{len(remaining_tasks)}条）")

    # 高频检查模式：如果没有新补充的任务，静默退出
    if check_only and len(new_tasks) == 0:
        print("高频检查：无新补充任务，静默退出")
        return

    success = send_message(task_info)
    print(f"消息推送: {'成功' if success else '失败'}")
    print("=== 执行完成 ===")


if __name__ == "__main__":
    main()
