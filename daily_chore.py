#!/usr/bin/env python3
import requests
import json
import os

APP_ID = os.environ['FEISHU_APP_ID']
APP_SECRET = os.environ['FEISHU_APP_SECRET']
APP_TOKEN = os.environ['FEISHU_APP_TOKEN']
CHORE_TABLE_ID = os.environ['CHORE_TABLE_ID']
CONFIG_TABLE_ID = os.environ['CONFIG_TABLE_ID']
VIEW_ID = os.environ['VIEW_ID']

def get_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET})
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"获取token失败: {data}")
    return data["tenant_access_token"]

def get_records(token, table_id, view_id=None, page_size=500):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records"
    params = {"page_size": page_size}
    if view_id:
        params["view_id"] = view_id
    resp = requests.get(url, headers=headers, params=params)
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"读取记录失败: {data}")
    return data["data"]["items"]

def update_record(token, table_id, record_id, fields):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records/{record_id}"
    resp = requests.put(url, headers=headers, json={"fields": fields})
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"更新记录失败: {data}")

def batch_update(token, table_id, records):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records/batch_update"
    resp = requests.post(url, headers=headers, json={"records": records})
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"批量更新失败: {data}")

def send_msg(token, user_id, text):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    body = {
        "receive_id": user_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False)
    }
    resp = requests.post(url, headers=headers, json=body)
    if resp.json().get("code") != 0:
        print(f"发消息失败: {resp.json()}")
    else:
        print("消息已发送")

def main():
    token = get_token()
    
    # 1. 按视图顺序读取任务（就是你拖拽排好的顺序）
    chores = get_records(token, CHORE_TABLE_ID)
    if not chores:
        print("任务表为空"); return
    
    # 自动从表格创建者信息提取 USER_ID（你完全不用手动找）
    USER_ID = chores[0].get('created_by', {}).get('id')
    if not USER_ID:
        print("无法获取USER_ID"); return
    print(f"USER_ID: {USER_ID}")
    
    # 2. 读取配置表（豆包的键值对格式：配置项→值）
    configs = get_records(token, CONFIG_TABLE_ID)
    config_map = {}      # 配置项→值
    record_id_map = {}   # 配置项→记录ID（用于更新）
    for rec in configs:
        key = rec['fields'].get('配置项', '')
        val = rec['fields'].get('值', '')
        config_map[key] = val
        record_id_map[key] = rec['record_id']
    
    current_id = config_map.get('当前任务ID', '')
    total = int(config_map.get('总任务数', 0) or len(chores))
    
    # 3. 找到当前任务在视图中的位置
    current_index = -1
    for i, rec in enumerate(chores):
        if rec['record_id'] == current_id:
            current_index = i; break
    
    if current_index == -1:
        # 当前ID找不到了（被删了），回到第一个
        current_index = 0
        current_id = chores[0]['record_id']
        update_record(token, CONFIG_TABLE_ID, record_id_map['当前任务ID'], {'值': current_id})
    
    # 4. 获取当前任务信息
    rec = chores[current_index]
    fields = rec['fields']
    record_id = rec['record_id']
    
    # 拼接任务内容（大区域 + 小区域 + 具体描述）
    parts = [str(fields.get(k,'')) for k in ['大区域','小区域','具体区域描述'] if fields.get(k)]
    content = ' - '.join(parts) if parts else '未知任务'
    
    is_done = fields.get('是否今日', False)
    print(f"任务({current_index+1}/{total}): {content}, 完成: {is_done}")
    
    # 5. 推送逻辑
    if is_done:
        next_index = current_index + 1
        if next_index >= total:
            # 一轮完成，回到开头，清空所有复选框
            next_index = 0
            next_rec = chores[0]
            next_id = next_rec['record_id']
            
            # 批量清空「是否今日」
            resets = [{"record_id": r['record_id'], "fields": {"是否今日": False}} for r in chores]
            for i in range(0, len(resets), 500):
                batch_update(token, CHORE_TABLE_ID, resets[i:i+500])
            
            update_record(token, CONFIG_TABLE_ID, record_id_map['当前任务ID'], {'值': next_id})
            
            np = [str(next_rec['fields'].get(k,'')) for k in ['大区域','小区域','具体区域描述'] if next_rec['fields'].get(k)]
            nc = ' - '.join(np) if np else '未知任务'
            msg = f"🎉 一轮全部完成！已自动重置。\n\n🏠 明日任务（1/{total}）：{nc}"
        else:
            next_rec = chores[next_index]
            next_id = next_rec['record_id']
            update_record(token, CONFIG_TABLE_ID, record_id_map['当前任务ID'], {'值': next_id})
            
            np = [str(next_rec['fields'].get(k,'')) for k in ['大区域','小区域','具体区域描述'] if next_rec['fields'].get(k)]
            nc = ' - '.join(np) if np else '未知任务'
            msg = f"⏭️ 上一项「{content}」已完成\n\n🏠 明日任务（{next_index+1}/{total}）：{nc}"
    else:
        msg = f"🏠 今日任务（{current_index+1}/{total}）：{content}\n\n⏳ 完成后请在表格勾选「是否今日」"
    
    send_msg(token, USER_ID, msg)
    print("完毕")

if __name__ == '__main__':
    main()
