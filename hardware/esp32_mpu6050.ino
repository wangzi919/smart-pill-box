#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <Wire.h>
#include <arduinoFFT.h>


// ═══════════════════════════════════════
const char *WIFI_SSID = "TP-Link_A798";
const char *WIFI_PASSWORD = "43257294";
const char *FLASK_URL = "http://192.168.0.101:5000/api/sensor_data";
// ═══════════════════════════════════════

int REMIND_HOURS[10];
int REMIND_HOURS_MIN[10];
int REMIND_COUNT = 0;
int TIMEOUT_MIN = 30;

bool isAlerting = false;
int alertingIndex = -1;

#define SDA_PIN 27
#define SCL_PIN 14
#define HALL_PIN 13
#define BUZZER_PIN 25

#define SAMPLES 256
#define SAMPLING_FREQ 100
double vReal[SAMPLES], vImag[SAMPLES];
ArduinoFFT<double> FFT(vReal, vImag, SAMPLES, SAMPLING_FREQ);

Adafruit_MPU6050 mpu;

bool lidOpen = false;
bool lidWasOpen = false;
unsigned long lidOpenTime = 0;
bool reminded[10] = {};
bool missedSent[10] = {};

// ───────────────────────────────────────
void beep(int times) {
  for (int i = 0; i < times; i++) {
    ledcWriteTone(0, 2000);
    delay(600);
    ledcWriteTone(0, 0);
    delay(150);
  }
}

void alertBeep() {
  ledcWriteTone(0, 2500);
  delay(400);
  ledcWriteTone(0, 0);
  delay(100);
}

// ───────────────────────────────────────
float calcTremorIndex() {
  Serial.println("開始 FFT 採樣...");
  for (int i = 0; i < SAMPLES; i++) {
    unsigned long t = micros();
    sensors_event_t a, g, temp;
    mpu.getEvent(&a, &g, &temp);
    vReal[i] = sqrt(pow(a.acceleration.x, 2) + pow(a.acceleration.y, 2) +
                    pow(a.acceleration.z - 9.81, 2));
    vImag[i] = 0;
    while (micros() - t < 10000)
      ;
  }

  FFT.windowing(FFTWindow::Hann, FFTDirection::Forward);
  FFT.compute(FFTDirection::Forward);
  FFT.complexToMagnitude();

  float p_tremor = 0, p_total = 0;
  for (int i = 1; i < SAMPLES / 2; i++) {
    float freq = (i * SAMPLING_FREQ) / (float)SAMPLES;
    if (freq >= 0.5 && freq <= 20.0)
      p_total += vReal[i];
    if (freq >= 4.0 && freq <= 6.0)
      p_tremor += vReal[i];
  }

  float TI = (p_total > 0) ? (p_tremor / p_total * 100.0) : 0;
  Serial.print("震顫指數 TI = ");
  Serial.print(TI);
  Serial.println("%");
  return TI;
}

// ───────────────────────────────────────
void uploadData(float TI, unsigned long duration_ms) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Wi-Fi 未連線，略過上傳");
    return;
  }
  HTTPClient http;
  http.begin(FLASK_URL);
  http.addHeader("Content-Type", "application/json");

  String body = "{\"tremor_index\":";
  body += String(TI, 2);
  body += ",\"duration_ms\":";
  body += String(duration_ms);
  body += "}";

  Serial.println("上傳：" + body);
  int code = http.POST(body);
  Serial.println("HTTP 回應：" + String(code));
  http.end();
}

// ───────────────────────────────────────
void connectWiFi() {
  Serial.print("連接 Wi-Fi");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int retry = 0;
  while (WiFi.status() != WL_CONNECTED && retry < 20) {
    delay(500);
    Serial.print(".");
    retry++;
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWi-Fi 連線成功！IP: " + WiFi.localIP().toString());
    configTime(8 * 3600, 0, "pool.ntp.org");
    Serial.println("NTP 校時中...");
    delay(2000);
  } else {
    Serial.println("\nWi-Fi 連線失敗，繼續離線運作");
  }
}

// ───────────────────────────────────────
void syncSchedule() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("離線，使用預設時間 08:00 / 12:00 / 21:00");
    REMIND_COUNT = 3;
    REMIND_HOURS[0] = 8;
    REMIND_HOURS_MIN[0] = 0;
    REMIND_HOURS[1] = 12;
    REMIND_HOURS_MIN[1] = 0;
    REMIND_HOURS[2] = 21;
    REMIND_HOURS_MIN[2] = 0;
    TIMEOUT_MIN = 30;
    return;
  }

  HTTPClient http;
  String url = String(FLASK_URL);
  url.replace("/api/sensor_data", "/api/schedule");
  Serial.println("同步排程：" + url);
  http.begin(url);
  int code = http.GET();

  if (code == 200) {
    String payload = http.getString();
    Serial.println("排程同步成功：" + payload);

    REMIND_COUNT = 0;
    int pos = payload.indexOf("\"times\"");
    if (pos != -1) {
      int start = payload.indexOf('[', pos);
      int end = payload.indexOf(']', start);
      String timesStr = payload.substring(start + 1, end);
      while (timesStr.length() > 0 && REMIND_COUNT < 10) {
        int q1 = timesStr.indexOf('"');
        if (q1 == -1)
          break;
        int q2 = timesStr.indexOf('"', q1 + 1);
        if (q2 == -1)
          break;
        String t = timesStr.substring(q1 + 1, q2);
        int colon = t.indexOf(':');
        REMIND_HOURS[REMIND_COUNT] = t.substring(0, colon).toInt();
        REMIND_HOURS_MIN[REMIND_COUNT] = t.substring(colon + 1).toInt();
        REMIND_COUNT++;
        timesStr = timesStr.substring(q2 + 1);
      }
    }
    int tp = payload.indexOf("\"timeout_min\"");
    if (tp != -1) {
      int colon = payload.indexOf(':', tp);
      TIMEOUT_MIN = payload.substring(colon + 1).toInt();
    }
    Serial.print("載入 ");
    Serial.print(REMIND_COUNT);
    Serial.print(" 個時段，超時 ");
    Serial.print(TIMEOUT_MIN);
    Serial.println(" 分鐘");

    for (int i = 0; i < REMIND_COUNT; i++) {
      reminded[i] = false;
      missedSent[i] = false;
    }
  } else {
    Serial.println("排程同步失敗，使用預設時間 08:00 / 12:00 / 21:00");
    REMIND_COUNT = 3;
    REMIND_HOURS[0] = 8;
    REMIND_HOURS_MIN[0] = 0;
    REMIND_HOURS[1] = 12;
    REMIND_HOURS_MIN[1] = 0;
    REMIND_HOURS[2] = 21;
    REMIND_HOURS_MIN[2] = 0;
    TIMEOUT_MIN = 30;
  }
  http.end();
}

// ───────────────────────────────────────
void checkSchedule() {
  struct tm t;
  if (!getLocalTime(&t))
    return;

  int h = t.tm_hour;
  int m = t.tm_min;

  for (int i = 0; i < REMIND_COUNT; i++) {
    int nowTotal = h * 60 + m;
    int scheduleTotal = REMIND_HOURS[i] * 60 + REMIND_HOURS_MIN[i];
    int elapsedMin = nowTotal - scheduleTotal;

    // 時間到：叫三聲
    if (elapsedMin >= 0 && elapsedMin <= 1 && !reminded[i]) {
      Serial.printf("服藥時間到！%02d:%02d\n", REMIND_HOURS[i],
                    REMIND_HOURS_MIN[i]);
      beep(3);
      isAlerting = false;
      reminded[i] = true;
      missedSent[i] = false;
    }

    // 超時未服藥：開始持續叫 (不再上傳 -1，改由伺服器判斷)
    if (elapsedMin >= TIMEOUT_MIN && elapsedMin < TIMEOUT_MIN + 2 &&
        reminded[i] && !missedSent[i]) {
      Serial.printf("時段 %d 超時未服藥，開始持續提醒\n", i);
      missedSent[i] = true;
      isAlerting = true;
      alertingIndex = i;
    }

    // 超過 2 小時重置
    if (elapsedMin > 120) {
      reminded[i] = false;
      missedSent[i] = false;
    }
  }
}

// ───────────────────────────────────────
void setup() {
  Serial.begin(115200);

  ledcSetup(0, 2000, 8);
  ledcAttachPin(BUZZER_PIN, 0);

  pinMode(HALL_PIN, INPUT_PULLUP);

  Wire.begin(SDA_PIN, SCL_PIN);
  if (!mpu.begin()) {
    Serial.println("MPU6050 找不到！檢查接線");
    while (1)
      delay(10);
  }
  Serial.println("MPU6050 OK");
  mpu.setAccelerometerRange(MPU6050_RANGE_8_G);
  mpu.setGyroRange(MPU6050_RANGE_500_DEG);
  mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);

  connectWiFi();
  syncSchedule();
  Serial.println("系統就緒，等待開蓋...");
}

// ───────────────────────────────────────
void loop() {
  lidOpen = (digitalRead(HALL_PIN) == HIGH);

  // 開蓋 → 停止持續催藥
  if (lidOpen && !lidWasOpen) {
    lidOpenTime = millis();
    lidWasOpen = true;
    Serial.println("偵測到開蓋！");
    if (isAlerting) {
      isAlerting = false;
      alertingIndex = -1;
      ledcWriteTone(0, 0);
      Serial.println("已服藥，停止提醒");
    }
  }

  // 關蓋 → 分析並上傳
  if (!lidOpen && lidWasOpen) {
    unsigned long duration = millis() - lidOpenTime;
    Serial.print("開蓋時長：");
    Serial.print(duration);
    Serial.println("ms");
    float TI = calcTremorIndex();
    uploadData(TI, duration);
    lidWasOpen = false;
  }

  // 超時催藥：每 2 秒持續響
  if (isAlerting) {
    static unsigned long lastAlert = 0;
    if (millis() - lastAlert > 2000) {
      alertBeep();
      lastAlert = millis();
    }
  }

  // 每 30 秒檢查排程
  static unsigned long lastCheck = 0;
  if (millis() - lastCheck > 30000) {
    checkSchedule();
    lastCheck = millis();
  }

  delay(50);
}