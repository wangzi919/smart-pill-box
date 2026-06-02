import sqlite3
import os
import json
import requests
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
from dotenv import load_dotenv
import datetime
from apscheduler.schedulers.background import BackgroundScheduler

# 載入環境變數
load_dotenv()
LINE_TOKEN = os.getenv('LINE_TOKEN')
LINE_USER_ID = os.getenv('LINE_USER_ID')

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
            is_abnormal INTEGER
        )
    ''')
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
                        INSERT INTO sensor_data (tremor_index, duration_ms, is_abnormal)
                        VALUES (?, ?, ?)
                    ''', (-1, 0, 1))
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
            
        # 判斷狀態
        if tremor_index == -1:
            send_line("⏰ 收到藥盒通知：超時未服藥提醒！請確認長者狀況。")
            is_abnormal = 1 # 視為異常狀況記錄
        else:
            is_abnormal = 1 if tremor_index > 30 else 0
            if is_abnormal:
                send_line(f"⚠️ 警告！偵測到長者服藥時手部震顫異常！(TI={tremor_index:.1f}%，開蓋時間={duration_ms}ms)，請留意長者身體狀況。")
            else:
                now_str = datetime.datetime.now().strftime("%H:%M")
                send_line(f"✅ 長者已於 {now_str} 完成服藥，震顫指數正常。")
                
        # 存入資料庫
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            INSERT INTO sensor_data (tremor_index, duration_ms, is_abnormal)
            VALUES (?, ?, ?)
        ''', (tremor_index, duration_ms, is_abnormal))
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
            SELECT id, timestamp, tremor_index, duration_ms, is_abnormal 
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
            SELECT date(timestamp) as date, AVG(tremor_index) as avg_ti
            FROM sensor_data
            WHERE timestamp >= date('now', 'localtime', '-7 days') 
              AND tremor_index != -1
            GROUP BY date(timestamp)
            ORDER BY date(timestamp) ASC
        ''')
        rows = c.fetchall()
        conn.close()
        
        report = [{"date": row[0], "avg_ti": round(row[1], 2) if row[1] is not None else 0} for row in rows]
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
    html_template = """
    <!DOCTYPE html>
    <html lang="zh-TW">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>智慧藥盒監控面板</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; background-color: #f0f2f5; color: #333; }
            h1, h2 { color: #1a202c; text-align: center; margin-bottom: 5px; }
            h2 { font-size: 1.5em; margin-top: 40px; text-align: left; padding-bottom: 10px; border-bottom: 2px solid #e2e8f0; }
            .container { max-width: 900px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }
            .refresh-note { text-align: center; color: #718096; font-size: 0.9em; margin-bottom: 25px; }
            table { width: 100%; border-collapse: collapse; margin-bottom: 30px;}
            th, td { padding: 15px 20px; text-align: left; border-bottom: 1px solid #e2e8f0; }
            th { background-color: #2d3748; color: white; font-weight: 600; }
            tr:hover { background-color: #f7fafc; }
            tr:last-child td { border-bottom: none; }
            .status-normal { color: #38a169; font-weight: bold; background: #e6fffa; padding: 4px 10px; border-radius: 999px; font-size: 0.85em;}
            .status-abnormal { color: #e53e3e; font-weight: bold; background: #fff5f5; padding: 4px 10px; border-radius: 999px; font-size: 0.85em;}
            .status-missed { color: #d69e2e; font-weight: bold; background: #fffff0; padding: 4px 10px; border-radius: 999px; font-size: 0.85em;}
            
            /* 排程表單樣式 */
            .schedule-form { background: #f8fafc; padding: 25px; border-radius: 8px; border: 1px solid #e2e8f0; }
            .form-group { margin-bottom: 20px; }
            .form-group label { display: block; margin-bottom: 8px; font-weight: bold; color: #4a5568;}
            .form-group input[type="number"], .form-group input[type="time"] { padding: 10px 15px; border: 1px solid #cbd5e0; border-radius: 5px; font-size: 1em; width: 150px; }
            .time-list { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 15px; }
            .time-item { background: #e2e8f0; padding: 8px 15px; border-radius: 5px; display: flex; align-items: center; gap: 10px; border: 1px solid #cbd5e0; font-weight: 500;}
            .btn-remove { background: #e53e3e; color: white; border: none; border-radius: 50%; width: 22px; height: 22px; display: flex; align-items: center; justify-content: center; cursor: pointer; font-size: 12px;}
            .btn-remove:hover { background: #c53030; }
            .btn { background: #3182ce; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-weight: bold; font-size: 1em; transition: background 0.2s;}
            .btn:hover { background: #2b6cb0; }
            .btn-add { background: #48bb78; }
            .btn-add:hover { background: #38a169; }
            .flex-row { display: flex; align-items: center; gap: 10px;}
            
            /* 導覽列 */
            .navbar { display: flex; justify-content: center; gap: 20px; margin-bottom: 30px; }
            .navbar a { text-decoration: none; padding: 10px 20px; border-radius: 8px; font-weight: bold; color: #4a5568; background-color: #e2e8f0; transition: 0.2s;}
            .navbar a:hover { background-color: #cbd5e0; }
            .navbar a.active { background-color: #3182ce; color: white; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="navbar">
                <a href="/" class="active">儀表板 (即時監控)</a>
                <a href="/calendar">歷史日曆 (服藥與震顫)</a>
            </div>
            <h1>💊 智慧藥盒即時監控</h1>
            <!-- 每 10 秒自動刷新，但如果使用者在操作表單則暫停 -->
            <p class="refresh-note">即時數據將每 10 秒自動重新整理 (表單操作時暫停)</p>
            
            <h2>即時服藥記錄</h2>
            <table>
                <thead>
                    <tr>
                        <th>記錄時間</th>
                        <th>震顫指數 (TI)</th>
                        <th>開蓋時長 (ms)</th>
                        <th>狀態判定</th>
                    </tr>
                </thead>
                <tbody>
                    {% for log in logs %}
                    <tr>
                        <td>{{ log.timestamp }}</td>
                        <td>
                            {% if log.tremor_index == -1 %}
                                <span style="color: #a0aec0;">N/A</span>
                            {% else %}
                                {{ log.tremor_index }}
                            {% endif %}
                        </td>
                        <td>{{ log.duration_ms }}</td>
                        <td>
                            {% if log.tremor_index == -1 %}
                                <span class="status-missed">超時未服藥</span>
                            {% elif log.is_abnormal == 1 %}
                                <span class="status-abnormal">異常 (TI > 30)</span>
                            {% else %}
                                <span class="status-normal">正常</span>
                            {% endif %}
                        </td>
                    </tr>
                    {% else %}
                    <tr>
                        <td colspan="4" style="text-align: center; color: #a0aec0;">目前暫無任何服藥記錄</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>

            <h2>🕒 排程與超時設定</h2>
            <div class="schedule-form">
                <div class="form-group">
                    <label>服藥超時判定時間 (分鐘)</label>
                    <input type="number" id="timeout_min" value="{{ schedule.timeout_min }}" min="1">
                    <p style="margin-top: 5px; font-size: 0.85em; color: #718096;">超過此時間未開蓋，將視為「超時未服藥」並觸發通知。</p>
                </div>
                <div class="form-group">
                    <label>預定服藥時間列表</label>
                    <div class="time-list" id="timeList">
                        {% for t in schedule.times %}
                        <div class="time-item" data-time="{{ t }}">
                            <span>{{ t }}</span>
                            <button class="btn-remove" onclick="removeTime(this)" title="移除此時間">×</button>
                        </div>
                        {% endfor %}
                    </div>
                    <div class="flex-row">
                        <input type="time" id="newTime">
                        <button class="btn btn-add" onclick="addTime()">+ 新增時間</button>
                    </div>
                </div>
                <button class="btn" onclick="saveSchedule()" style="width: 100%; margin-top: 10px; font-size: 1.1em; padding: 12px;">💾 儲存排程設定並同步至 ESP32</button>
            </div>
        </div>

        <script>
            // 新增時間
            function addTime() {
                const newTime = document.getElementById('newTime').value;
                if (!newTime) return alert("請先選擇時間！");
                
                const timeList = document.getElementById('timeList');
                // 檢查是否重複
                const existing = Array.from(timeList.children).map(el => el.dataset.time);
                if (existing.includes(newTime)) return alert("此時間已存在排程中！");

                const div = document.createElement('div');
                div.className = 'time-item';
                div.dataset.time = newTime;
                div.innerHTML = `<span>${newTime}</span><button class="btn-remove" onclick="removeTime(this)" title="移除此時間">×</button>`;
                timeList.appendChild(div);
                document.getElementById('newTime').value = '';
                
                // 依照時間排序 (字串排序，因為格式都是 HH:MM)
                const items = Array.from(timeList.children);
                items.sort((a, b) => a.dataset.time.localeCompare(b.dataset.time));
                items.forEach(item => timeList.appendChild(item));
            }

            // 移除時間
            function removeTime(btn) {
                btn.parentElement.remove();
            }

            // 儲存排程 (打 API POST 請求)
            function saveSchedule() {
                const timeout_min = parseInt(document.getElementById('timeout_min').value);
                const times = Array.from(document.getElementById('timeList').children).map(el => el.dataset.time);
                
                if (!timeout_min || timeout_min < 1) return alert("請輸入有效的超時時間！");

                // 呼叫更新 API
                fetch('/api/schedule', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ times: times, timeout_min: timeout_min })
                })
                .then(res => res.json())
                .then(data => {
                    if(data.error) alert("更新失敗: " + data.error);
                    else alert("排程更新成功！ESP32 將會在下次請求時同步新排程。");
                })
                .catch(err => alert("網路錯誤，無法更新排程！"));
            }

            // 自動重新整理邏輯
            let refreshTimer;
            function startTimer() {
                refreshTimer = setTimeout(() => {
                    window.location.reload();
                }, 10000);
            }
            // 頁面載入啟動計時
            startTimer();

            // 若使用者在輸入欄位、按鈕上進行操作，則暫停重新整理，避免干擾
            const interactables = document.querySelectorAll('input, button');
            interactables.forEach(el => {
                el.addEventListener('focus', () => clearTimeout(refreshTimer));
                el.addEventListener('mouseenter', () => clearTimeout(refreshTimer));
                
                el.addEventListener('blur', startTimer);
                el.addEventListener('mouseleave', () => {
                    // 若焦點不在任何輸入框上才重啟計時
                    if(document.activeElement.tagName !== 'INPUT') {
                        startTimer();
                    }
                });
            });
        </script>
    </body>
    </html>
    """
    
    # 讀取日誌記錄
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('''
            SELECT id, timestamp, tremor_index, duration_ms, is_abnormal 
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
        
    return render_template_string(html_template, logs=logs, schedule=schedule)

@app.route('/calendar')
def calendar_view():
    """日曆介面，顯示服藥與震顫紀錄"""
    html_template = """
    <!DOCTYPE html>
    <html lang="zh-TW">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>智慧藥盒 - 歷史日曆</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; background-color: #f0f2f5; color: #333; }
            h1, h2 { color: #1a202c; text-align: center; margin-bottom: 5px; }
            .container { max-width: 900px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }
            
            /* 導覽列 */
            .navbar { display: flex; justify-content: center; gap: 20px; margin-bottom: 30px; }
            .navbar a { text-decoration: none; padding: 10px 20px; border-radius: 8px; font-weight: bold; color: #4a5568; background-color: #e2e8f0; transition: 0.2s;}
            .navbar a:hover { background-color: #cbd5e0; }
            .navbar a.active { background-color: #3182ce; color: white; }
            
            /* 日曆控制區 */
            .calendar-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
            .calendar-header button { background: #3182ce; color: white; border: none; padding: 8px 15px; border-radius: 5px; cursor: pointer; font-weight: bold; }
            .calendar-header button:hover { background: #2b6cb0; }
            .calendar-header h2 { margin: 0; }
            
            /* 日曆網格 */
            .calendar-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 10px; }
            .day-name { text-align: center; font-weight: bold; color: #4a5568; padding: 10px 0; border-bottom: 2px solid #e2e8f0;}
            .day-cell { border: 1px solid #e2e8f0; border-radius: 8px; min-height: 100px; padding: 10px; cursor: pointer; transition: 0.2s; background: white; display: flex; flex-direction: column; }
            .day-cell:hover { box-shadow: 0 4px 6px rgba(0,0,0,0.1); transform: translateY(-2px); }
            .day-cell.empty { background: transparent; border: none; cursor: default; box-shadow: none; transform: none;}
            .date-num { font-weight: bold; font-size: 1.1em; margin-bottom: 10px; color: #2d3748;}
            
            /* 狀態標示 */
            .status-indicator { margin-top: auto; font-size: 0.85em; font-weight: bold; text-align: center; padding: 4px; border-radius: 4px;}
            
            .status-normal { background-color: #c6f6d5; color: #22543d; border: 1px solid #9ae6b4; }
            .status-missed { background-color: #feebc8; color: #7b341e; border: 1px solid #fbd38d; }
            .status-abnormal { background-color: #fed7d7; color: #822727; border: 1px solid #feb2b2; }
            
            /* 詳細資訊區塊 */
            .details-panel { margin-top: 30px; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0; background: #f8fafc; display: none; }
            .details-panel h3 { margin-top: 0; border-bottom: 2px solid #e2e8f0; padding-bottom: 10px; color: #2d3748;}
            .log-list { list-style: none; padding: 0; margin: 0; }
            .log-item { display: flex; justify-content: space-between; align-items: center; padding: 10px; border-bottom: 1px solid #e2e8f0; }
            .log-item:last-child { border-bottom: none; }
            .log-time { font-weight: bold; color: #4a5568; }
            
            /* 狀態標籤 (同 Dashboard) */
            .tag-normal { color: #38a169; font-weight: bold; background: #e6fffa; padding: 4px 10px; border-radius: 999px; font-size: 0.85em;}
            .tag-abnormal { color: #e53e3e; font-weight: bold; background: #fff5f5; padding: 4px 10px; border-radius: 999px; font-size: 0.85em;}
            .tag-missed { color: #d69e2e; font-weight: bold; background: #fffff0; padding: 4px 10px; border-radius: 999px; font-size: 0.85em;}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="navbar">
                <a href="/">儀表板 (即時監控)</a>
                <a href="/calendar" class="active">歷史日曆 (服藥與震顫)</a>
            </div>
            
            <h1>📅 歷史服藥日曆</h1>
            
            <div class="calendar-header">
                <button onclick="changeMonth(-1)">&#9664; 上個月</button>
                <h2 id="currentMonthYear">載入中...</h2>
                <button onclick="changeMonth(1)">下個月 &#9654;</button>
            </div>
            
            <div class="calendar-grid" id="calendarGrid">
                <!-- Days will be generated by JS -->
            </div>
            
            <div class="details-panel" id="detailsPanel">
                <h3 id="detailsTitle">詳細紀錄</h3>
                <ul class="log-list" id="detailsList">
                    <!-- Logs will be generated by JS -->
                </ul>
            </div>
        </div>

        <script>
            let currentDate = new Date();
            let calendarData = {};
            
            const monthNames = ["一月", "二月", "三月", "四月", "五月", "六月", "七月", "八月", "九月", "十月", "十一月", "十二月"];
            
            function changeMonth(offset) {
                currentDate.setMonth(currentDate.getMonth() + offset);
                renderCalendar();
            }
            
            async function fetchCalendarData(year, month) {
                try {
                    const res = await fetch(`/api/calendar_data?year=${year}&month=${month}`);
                    if (res.ok) {
                        return await res.json();
                    }
                } catch (e) {
                    console.error("Failed to fetch calendar data", e);
                }
                return {};
            }
            
            async function renderCalendar() {
                const year = currentDate.getFullYear();
                const month = currentDate.getMonth(); // 0-indexed
                const displayMonth = month + 1;
                
                document.getElementById('currentMonthYear').textContent = `${year}年 ${monthNames[month]}`;
                
                // Fetch data for this month
                calendarData = await fetchCalendarData(year, displayMonth);
                
                const grid = document.getElementById('calendarGrid');
                grid.innerHTML = '';
                
                // Add day names
                const daysOfWeek = ['日', '一', '二', '三', '四', '五', '六'];
                daysOfWeek.forEach(d => {
                    const el = document.createElement('div');
                    el.className = 'day-name';
                    el.textContent = d;
                    grid.appendChild(el);
                });
                
                const firstDayOfMonth = new Date(year, month, 1).getDay();
                const daysInMonth = new Date(year, month + 1, 0).getDate();
                
                // Empty cells before the first day
                for (let i = 0; i < firstDayOfMonth; i++) {
                    const el = document.createElement('div');
                    el.className = 'day-cell empty';
                    grid.appendChild(el);
                }
                
                // Days of the month
                for (let i = 1; i <= daysInMonth; i++) {
                    const dateStr = `${year}-${String(displayMonth).padStart(2, '0')}-${String(i).padStart(2, '0')}`;
                    const el = document.createElement('div');
                    el.className = 'day-cell';
                    el.onclick = () => showDetails(dateStr);
                    
                    const num = document.createElement('div');
                    num.className = 'date-num';
                    num.textContent = i;
                    el.appendChild(num);
                    
                    if (calendarData[dateStr]) {
                        const status = calendarData[dateStr].status;
                        const indicator = document.createElement('div');
                        indicator.className = `status-indicator status-${status}`;
                        
                        if (status === 'abnormal') {
                            indicator.innerHTML = '⚠️ 異常';
                        } else if (status === 'missed') {
                            indicator.innerHTML = '⏰ 漏吃';
                        } else {
                            indicator.innerHTML = '✅ 正常';
                        }
                        
                        el.appendChild(indicator);
                    }
                    
                    grid.appendChild(el);
                }
                
                // Hide details panel when month changes
                document.getElementById('detailsPanel').style.display = 'none';
            }
            
            function showDetails(dateStr) {
                const panel = document.getElementById('detailsPanel');
                const title = document.getElementById('detailsTitle');
                const list = document.getElementById('detailsList');
                
                title.textContent = `${dateStr} 服藥與震顫紀錄`;
                list.innerHTML = '';
                
                const data = calendarData[dateStr];
                
                if (!data || data.records.length === 0) {
                    list.innerHTML = '<li class="log-item" style="justify-content:center; color:#a0aec0;">當日無紀錄</li>';
                } else {
                    data.records.forEach(record => {
                        const li = document.createElement('li');
                        li.className = 'log-item';
                        
                        let statusTag = '';
                        let tiInfo = `TI: ${record.tremor_index}`;
                        
                        if (record.tremor_index === -1) {
                            statusTag = '<span class="tag-missed">超時未服藥</span>';
                            tiInfo = 'TI: N/A';
                        } else if (record.is_abnormal === 1) {
                            statusTag = '<span class="tag-abnormal">震顫異常</span>';
                        } else {
                            statusTag = '<span class="tag-normal">正常</span>';
                        }
                        
                        li.innerHTML = `
                            <span class="log-time">${record.time}</span>
                            <span>${tiInfo} | 開蓋: ${record.duration_ms}ms</span>
                            ${statusTag}
                        `;
                        list.appendChild(li);
                    });
                }
                
                panel.style.display = 'block';
                panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
            
            // Initialize
            renderCalendar();
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template)

if __name__ == '__main__':
    # 監聽 0.0.0.0 以便區域網路內的其他設備 (ESP32) 能夠連線
    app.run(host='0.0.0.0', port=5000, debug=True)
