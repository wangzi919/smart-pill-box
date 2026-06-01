# 💊 智慧藥盒 (Smart Pill Box) Backend

這是一個 IoT 智慧藥盒專題的後端 API 伺服器，使用 Python Flask 建立，並搭配 SQLite 作為資料庫。此後端設計用於接收來自 ESP32 的感測器資料 (MPU-6050 震顫指數與開蓋時長)，並提供即時網頁儀表板供照顧者監控。支援異常震顫或超時未服藥的 LINE Notify 通知。

## ✨ 功能特色

- **RESTful API**: 接收 ESP32 傳送的震顫數據與服藥狀態。
- **LINE Notify 整合**: 偵測到異常震顫指數 (TI > 30) 或「超時未服藥」時，自動發送 LINE 通知。
- **排程管理**: 提供 API 供 ESP32 同步預定服藥時間與超時判定條件。
- **即時網頁儀表板**: 內建現代化設計的 Web Dashboard，免安裝前端框架，每 10 秒自動刷新最新服藥記錄。
- **歷史服藥日曆**: 提供月曆介面，以顏色視覺化標示每日服藥狀態（正常、漏吃、震顫異常），並可點擊查看詳細記錄。
- **自動化 SQLite 資料庫**: 自動建立所需的資料表 (`sensor_data` 與 `schedule`)。

## 🛠️ 技術堆疊

- **後端框架**: Python Flask
- **資料庫**: SQLite3
- **環境變數**: python-dotenv
- **跨域處理**: flask-cors
- **硬體端預設環境**: ESP32 (Node32S), MPU-6050, 霍爾感測器, 蜂鳴器

## 🚀 快速開始

### 1. 安裝依賴套件

請確認您的環境中已安裝 Python 3，接著執行：

```bash
pip install flask flask-cors python-dotenv requests
```

### 2. 設定環境變數

專案根目錄下有一個 `.env` 檔案，請在裡面填入您的 LINE Notify Token：

```env
LINE_TOKEN=your_line_notify_token_here
```

### 3. 啟動伺服器

```bash
python app.py
```

伺服器將預設運行於 `http://0.0.0.0:5000`。
- 本地開發者請前往瀏覽器開啟：[http://localhost:5000](http://localhost:5000) 即可查看儀表板。
- 區域網路內的 ESP32 請發送請求至 `http://<您的電腦IP>:5000`。

## 📡 API 文件

### 1. 接收感測器資料
- **Endpoint**: `POST /api/sensor_data`
- **Content-Type**: `application/json`
- **Payload 範例**:
  - 正常/異常紀錄: `{"tremor_index": 11.93, "duration_ms": 2100}`
  - 未服藥事件: `{"tremor_index": -1, "duration_ms": 0}`

### 2. 取得最近 30 筆歷史記錄
- **Endpoint**: `GET /api/logs`
- **回傳**: JSON 格式的歷史陣列資料。

### 3. 取得最近 7 天震顫趨勢
- **Endpoint**: `GET /api/tremor_report`
- **回傳**: `[{"date": "2024-01-01", "avg_ti": 12.3}, ...]`

### 4. 取得/更新排程設定
- **Endpoint**: `GET /api/schedule` 
  - ESP32 開機時同步使用。
- **Endpoint**: `POST /api/schedule`
  - 網頁儀表板更新排程使用，格式：`{"times": ["08:00", "12:00", "21:00"], "timeout_min": 30}`

### 5. 取得歷史日曆資料
- **Endpoint**: `GET /api/calendar_data?year=2024&month=1`
- **回傳**: 依日期分組的服藥狀態與詳細記錄 (供網頁端月曆渲染使用)。
