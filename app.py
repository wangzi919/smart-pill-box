import sqlite3
import os
import json
import requests
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from dotenv import load_dotenv
import datetime
from apscheduler.schedulers.background import BackgroundScheduler

# 載入環境變數
load_dotenv()
LINE_TOKEN = os.getenv('LINE_TOKEN')
LINE_USER_ID = os.getenv('LINE_USER_ID')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
LLM_MODEL = os.getenv('LLM_MODEL', 'google/gemini-2.5-flash-lite')

app = Flask(__name__)
# 啟用 CORS 避免跨域問題
CORS(app)

DB_FILE = 'pillbox.db'

def init_db():
    """初始化資料庫並建立資料表"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # 建立感測器資料表
    c.execute('''
        CREATE TABLE IF NOT EXISTS sensor_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT (datetime('now', 'localtime')),
            tremor_index REAL,
            duration_ms INTEGER,
            is_abnormal INTEGER,
            baseline REAL DEFAULT 0.0
        )
    ''')
    # 嘗試為現有資料表加入新欄位 (若已存在會拋出錯誤被忽略)
    try:
        c.execute('ALTER TABLE sensor_data ADD COLUMN baseline REAL DEFAULT 0.0')
    except sqlite3.OperationalError:
        pass
    # 建立排程資料表 (限制只有一筆資料 id=1)
    c.execute('''
        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            times TEXT,
            timeout_min INTEGER
        )
    ''')
    # 插入預設排程
    c.execute('''
        INSERT OR IGNORE INTO schedule (id, times, timeout_min)
        VALUES (1, '["08:00", "12:00", "21:00"]', 30)
    ''')
    conn.commit()
    conn.close()

# 啟動時確保資料庫與資料表存在
init_db()

def check_missed_doses():
    """背景排程：檢查是否超時未服藥"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # 1. 取得排程與超時時間
        c.execute('SELECT times, timeout_min FROM schedule WHERE id = 1')
        row = c.fetchone()
        if not row:
            conn.close()
            return
            
        times = json.loads(row[0])
        timeout_min = row[1]
        
        now = datetime.datetime.now()
        
        for t_str in times:
            # 取得預定服藥時間
            hour, minute = map(int, t_str.split(':'))
            scheduled_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            
            # 取得超時判定時間
            timeout_time = scheduled_time + datetime.timedelta(minutes=timeout_min)
            
            # 檢查目前時間是否剛好在「超時時間」的一分鐘內
            # 這樣每分鐘執行一次的排程，只會在此區間內觸發一次
            if timeout_time <= now < timeout_time + datetime.timedelta(minutes=1):
                # 檢查預定時間前2小時，到目前的這段時間，是否有實際開蓋服藥紀錄
                check_start = scheduled_time - datetime.timedelta(hours=2)
                
                c.execute('''
                    SELECT id FROM sensor_data
                    WHERE timestamp >= ? AND timestamp <= ? 
                    AND tremor_index != -1
                ''', (check_start.strftime('%Y-%m-%d %H:%M:%S'), now.strftime('%Y-%m-%d %H:%M:%S')))
                
                record = c.fetchone()
                
                if not record:
                    # 沒有服藥紀錄 -> 觸發漏吃通知並寫入資料庫
                    print(f"[{now.strftime('%H:%M:%S')}] 偵測到漏吃！排程時間：{t_str}")
                    send_line(f"⏰ 超時未服藥提醒！長者原訂於 {t_str} 服藥，已超過設定之等待時間，請確認長者狀況。")
                    
                    c.execute('''
                        INSERT INTO sensor_data (tremor_index, duration_ms, is_abnormal, baseline)
                        VALUES (?, ?, ?, ?)
                    ''', (-1, 0, 1, 0.0))
                    conn.commit()

        conn.close()
    except Exception as e:
        print(f"背景排程檢查漏吃發生錯誤: {e}")

# 啟動背景排程器
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(func=check_missed_doses, trigger="interval", minutes=1)
scheduler.start()

def send_line(msg):
    """發送 LINE Messaging API 通知"""
    if not LINE_TOKEN or not LINE_USER_ID:
        print("未設定 LINE_TOKEN 或 LINE_USER_ID，無法發送通知")
        return
    
    try:
        response = requests.post(
            'https://api.line.me/v2/bot/message/push',
            headers={
                'Authorization': f'Bearer {LINE_TOKEN}',
                'Content-Type': 'application/json'
            },
            json={
                'to': LINE_USER_ID,
                'messages': [{'type': 'text', 'text': msg}]
            }
        )
        response.raise_for_status()
    except Exception as e:
        print(f"發送 LINE Message 失敗: {e}")

def get_recent_tremor_records(limit=5):
    """查詢最近幾筆有效震顫紀錄，排除漏吃紀錄供 LLM 摘要使用"""
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('''
            SELECT timestamp, tremor_index, duration_ms, is_abnormal, COALESCE(baseline, 0.0) as baseline
            FROM sensor_data
            WHERE tremor_index != -1
            ORDER BY id DESC LIMIT ?
        ''', (limit,))
        rows = c.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"查詢近期震顫紀錄失敗: {e}")
        return []

def generate_ai_tremor_message(context):
    """呼叫 OpenRouter，將震顫異常資料轉成適合 LINE 的人性化提醒"""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("未設定 OPENROUTER_API_KEY")

    system_prompt = (
        "你是一個智慧藥盒照護提醒助手。"
        "請根據震顫指數資料，用繁體中文寫出簡短、溫和、人性化的 LINE 通知。"
        "不要做醫療診斷，不要誇大風險。"
    )
    user_prompt = (
        "請依照以下資料產生一則可直接傳送到 LINE 的提醒文字。\n"
        "只輸出最終通知文字，禁止解釋你的思考過程，禁止使用英文。\n"
        "輸出限制：繁體中文、請寫兩句、約 80～120 個中文字、可以使用 emoji 但不要過多、"
        "語氣像照護提醒不要像警報、不要輸出 JSON。\n"
        "不可使用「病情惡化」、「罹患疾病」等絕對醫療判斷；"
        "只能說「建議觀察」、「可留意」、「若持續發生可考慮諮詢專業人員」。\n\n"
        f"event_type: {context.get('event_type')}\n"
        f"current tremor_index: {context.get('tremor_index')}\n"
        f"baseline: {context.get('baseline')}\n"
        f"ratio: {context.get('ratio')}\n"
        f"duration_ms: {context.get('duration_ms')}\n"
        f"recent_records: {json.dumps(context.get('recent_records', []), ensure_ascii=False)}"
    )

    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': user_prompt}
    ]
    invalid_markers = ['We need', 'LINE notification', 'JSON', 'system prompt', 'user prompt']

    for attempt in range(2):
        response = requests.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {OPENROUTER_API_KEY}',
                'Content-Type': 'application/json'
            },
            json={
                'model': LLM_MODEL,
                'messages': messages,
                'temperature': 0.4,
                'max_tokens': 180
            },
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        message = data['choices'][0]['message']['content'].strip()

        if not message or len(message) > 200 or any(marker in message for marker in invalid_markers):
            raise ValueError("LLM 回傳內容不適合直接發送")
        if len(message) >= 70 or attempt == 1:
            return message

        messages.append({'role': 'assistant', 'content': message})
        messages.append({
            'role': 'user',
            'content': '上一則太短，請保留同樣語氣，改寫成兩句、約 80～120 個中文字，只輸出 LINE 通知文字。'
        })

    raise ValueError("LLM 未產生可用提醒")

@app.route('/api/sensor_data', methods=['POST'])
def receive_sensor_data():
    """接收 ESP32 傳送的感測器資料"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "請提供 JSON 格式的資料"}), 400
        
        tremor_index = data.get('tremor_index')
        duration_ms = data.get('duration_ms')
        
        if tremor_index is None or duration_ms is None:
            return jsonify({"error": "缺少必要欄位 (tremor_index, duration_ms)"}), 400
            
        # 存入資料庫與異常判斷
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # 取得過去 7 天的 baseline (排除 -1)
        c.execute('''
            SELECT AVG(tremor_index), COUNT(id)
            FROM sensor_data
            WHERE timestamp >= datetime('now', 'localtime', '-7 days')
              AND tremor_index != -1
        ''')
        row = c.fetchone()
        baseline = row[0] if row[0] is not None else 0.0
        count = row[1] if row[1] is not None else 0
        
        # 判斷狀態
        is_abnormal = 0
        if tremor_index == -1:
            send_line("⏰ 收到藥盒通知：超時未服藥提醒！請確認長者狀況。")
            is_abnormal = 1 # 視為異常狀況記錄
        else:
            if count >= 5 and tremor_index > baseline * 1.5:
                is_abnormal = 1
                
            if is_abnormal == 1:
                # 檢查過去兩次有效紀錄是否也是異常
                c.execute('''
                    SELECT is_abnormal 
                    FROM sensor_data 
                    WHERE tremor_index != -1
                    ORDER BY id DESC LIMIT 2
                ''')
                last_two = c.fetchall()
                if len(last_two) == 2 and last_two[0][0] == 1 and last_two[1][0] == 1:
                    ratio = tremor_index / baseline if baseline > 0 else 0
                    fallback_msg = f"⚠️ 震顫趨勢異常！近期 TI={tremor_index:.1f}%，基準線={baseline:.1f}%，為平均的 {ratio:.1f} 倍，建議觀察。"
                    try:
                        recent_records = get_recent_tremor_records()
                        context = {
                            "event_type": "tremor_abnormal",
                            "tremor_index": tremor_index,
                            "baseline": baseline,
                            "ratio": ratio,
                            "duration_ms": duration_ms,
                            "recent_records": recent_records
                        }
                        ai_msg = generate_ai_tremor_message(context)
                        send_line(ai_msg if ai_msg else fallback_msg)
                    except Exception as e:
                        print(f"產生 AI 震顫提醒失敗，改用固定格式通知: {e}")
                        send_line(fallback_msg)
            else:
                now_str = datetime.datetime.now().strftime("%H:%M")
                send_line(f"✅ 長者已於 {now_str} 完成服藥，震顫指數正常。")
                
        # 存入資料庫
        c.execute('''
            INSERT INTO sensor_data (tremor_index, duration_ms, is_abnormal, baseline)
            VALUES (?, ?, ?, ?)
        ''', (tremor_index, duration_ms, is_abnormal, baseline))
        conn.commit()
        conn.close()
        
        return jsonify({"message": "資料接收成功"}), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/logs', methods=['GET'])
def get_logs():
    """查詢最近 30 筆服藥記錄"""
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('''
            SELECT id, timestamp, tremor_index, duration_ms, is_abnormal, COALESCE(baseline, 0.0) as baseline 
            FROM sensor_data 
            ORDER BY id DESC LIMIT 30
        ''')
        rows = c.fetchall()
        conn.close()
        
        logs = [dict(row) for row in rows]
        return jsonify(logs), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/tremor_report', methods=['GET'])
def get_tremor_report():
    """查詢最近 7 天的震顫趨勢（平均值）"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            SELECT date(timestamp) as date, AVG(tremor_index) as avg_ti, AVG(COALESCE(baseline, 0.0)) as avg_baseline
            FROM sensor_data
            WHERE timestamp >= date('now', 'localtime', '-7 days') 
              AND tremor_index != -1
            GROUP BY date(timestamp)
            ORDER BY date(timestamp) ASC
        ''')
        rows = c.fetchall()
        conn.close()
        
        report = [{"date": row[0], "avg_ti": round(row[1], 2) if row[1] is not None else 0, "avg_baseline": round(row[2], 2) if row[2] is not None else 0} for row in rows]
        return jsonify(report), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/schedule', methods=['GET', 'POST'])
def manage_schedule():
    """讀取或更新排程設定"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        if request.method == 'GET':
            c.execute('SELECT times, timeout_min FROM schedule WHERE id = 1')
            row = c.fetchone()
            conn.close()
            
            if row:
                times = json.loads(row[0])
                timeout_min = row[1]
                return jsonify({"times": times, "timeout_min": timeout_min}), 200
            else:
                return jsonify({"times": ["08:00", "12:00", "21:00"], "timeout_min": 30}), 200
                
        elif request.method == 'POST':
            data = request.get_json()
            if not data or 'times' not in data or 'timeout_min' not in data:
                return jsonify({"error": "無效的格式，需要 times 陣列與 timeout_min"}), 400
                
            times = data['times']
            timeout_min = data['timeout_min']
            
            # 確保寫入前轉換為 JSON 字串
            times_json = json.dumps(times)
            c.execute('''
                UPDATE schedule 
                SET times = ?, timeout_min = ?
                WHERE id = 1
            ''', (times_json, timeout_min))
            conn.commit()
            conn.close()
            
            return jsonify({"message": "排程更新成功"}), 200
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/calendar_data', methods=['GET'])
def get_calendar_data():
    """查詢指定月份的服藥記錄，用於日曆顯示"""
    year = request.args.get('year')
    month = request.args.get('month')
    
    if not year or not month:
        return jsonify({"error": "請提供 year 與 month 參數"}), 400
        
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # 取得該月份的所有記錄
        month_str = f"{year}-{month.zfill(2)}"
        c.execute('''
            SELECT timestamp, tremor_index, duration_ms, is_abnormal 
            FROM sensor_data 
            WHERE strftime('%Y-%m', timestamp) = ?
            ORDER BY timestamp ASC
        ''', (month_str,))
        
        rows = c.fetchall()
        conn.close()
        
        # 處理資料
        data = {}
        for row in rows:
            ts = row['timestamp']
            date_str = ts.split(' ')[0] # 'YYYY-MM-DD'
            time_str = ts.split(' ')[1] # 'HH:MM:SS'
            
            if date_str not in data:
                data[date_str] = {
                    "status": "normal", # normal, missed, abnormal
                    "records": []
                }
                
            record = {
                "time": time_str,
                "tremor_index": row['tremor_index'],
                "duration_ms": row['duration_ms'],
                "is_abnormal": row['is_abnormal']
            }
            data[date_str]['records'].append(record)
            
            # 更新當天整體狀態 (如果已經是 abnormal 就不再覆蓋)
            if data[date_str]['status'] != 'abnormal':
                if row['is_abnormal'] == 1:
                    data[date_str]['status'] = 'abnormal'
                elif row['tremor_index'] == -1:
                    data[date_str]['status'] = 'missed'
                    
        return jsonify(data), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/')
def dashboard():
    """Web 儀表板，顯示最近記錄與排程設定"""
    # 讀取日誌記錄
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('''
            SELECT id, timestamp, tremor_index, duration_ms, is_abnormal, COALESCE(baseline, 0.0) as baseline
            FROM sensor_data 
            ORDER BY id DESC LIMIT 10
        ''')
        rows = c.fetchall()
        logs = [dict(row) for row in rows]
    except Exception as e:
        print(f"讀取日誌資料庫錯誤: {e}")
        logs = []
        
    # 讀取排程設定
    try:
        # conn 已經在上面開啟
        c.execute('SELECT times, timeout_min FROM schedule WHERE id = 1')
        row = c.fetchone()
        if row:
            schedule = {"times": json.loads(row['times']), "timeout_min": row['timeout_min']}
        else:
            schedule = {"times": ["08:00", "12:00", "21:00"], "timeout_min": 30}
    except Exception as e:
        print(f"讀取排程資料庫錯誤: {e}")
        schedule = {"times": ["08:00", "12:00", "21:00"], "timeout_min": 30}
    finally:
        if 'conn' in locals():
            conn.close()
        
    return render_template('dashboard.html', logs=logs, schedule=schedule, active_page='dashboard')

@app.route('/calendar')
def calendar_view():
    """日曆介面，顯示服藥與震顫紀錄"""
    return render_template('calendar.html', active_page='calendar')

@app.route('/trend')
def trend_view():
    """趨勢分析介面"""
    return render_template('trend.html', active_page='trend')

if __name__ == '__main__':
    # 監聽 0.0.0.0 以便區域網路內的其他設備 (ESP32) 能夠連線
    app.run(host='0.0.0.0', port=5000, debug=True)
