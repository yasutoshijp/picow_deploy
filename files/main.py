from machine import Pin, I2C
import machine
import time
import math
import network
import ujson
import os

try:
    import urequests as requests
except ImportError:
    import requests

# =========================================================
# 動作モード・デバッグ設定
# =========================================================
MODE = "pi3"         # "pi3" または "alexa"
DEBUG_MODE = True    # True: 詳細ログ表示 / False: 重要なイベントのみ

# =========================================================
# 省電力（バッテリーモード）設定
# =========================================================
BATTERY_MODE = True  # True: モバイルバッテリー運用向け省電力モード
BATTERY_CPU_FREQ = 80_000_000   # バッテリーモード時CPU周波数 (80MHz, CYW43最低要件)
# Wi-Fiパワーセーブモード: ラジオをアイドル時に間欠動作させる（接続は維持）
# CYW43_PM_AGGRESSIVE = 0xa11142 : 積極的な省電力（ビーコン間隔でスリープ）
WIFI_PM_POWERSAVE = 0xa11142

# =========================================================
# アップデート設定（version.json 管理方式）
# =========================================================
UPDATE_HOSTS = [
    "http://192.168.4.118:8080",
    "http://raspberrypi3modelB.local:8080",
    "http://192.168.0.116:8080",
    "http://192.168.0.196:8080",
    
]
UPDATE_INTERVAL_SEC = 3600
# Pico W側に保存する現在のバージョン管理ファイル名
LOCAL_VERSION_FILE = "version.json" 
last_update_check = 0.0

# =========================================================
# Pi3送信先（MODE="pi3"）
# =========================================================
PI3_URL = "http://192.168.4.118:5000/direction"
PI3_SEND_INTERVAL_SEC = 0.5

# =========================================================
# Sinric（MODE="alexa"）
# =========================================================
SINRIC_API_KEY = "cdbfe918-0816-406f-bc91-cf718d010600"
DEVICE_IDS = {
    "north": "698bdb5140cb098d90d0d21f",
    "east":  "698bdb7fda2ae47a6c939f71",
    "south": "698bdbc540cb098d90d0d27c",
    "west":  "698bdbf140cb098d90d0d2a8",
}
SINRIC_API_URL_FMT = "https://api.sinric.pro/api/v1/devices/{device_id}/action"

# =========================================================
# Compass（QMC5883L系）設定
# =========================================================
I2C_BUS = 1
SDA_PIN = 2
SCL_PIN = 3
I2C_FREQ = 10000   # 安定の10kHz
ADDR = 0x0d

BAD_MIN = -32768
READ_RETRY = 12
DRDY_WAIT_MS = 250
DRDY_POLL_MS = 5

# --- キャリブレーション（椅子の鉄部補正） ---
X_OFFSET = 798.0
Y_OFFSET = 270.5
#X_OFFSET = 838.5
#Y_OFFSET = -1401.5

# 実家の北オフセット（※中心補正によりズレる可能性があるため、動作後に要微調整）
NORTH_DEG_RAW = 151

# 判定パラメータ
TARGETS = [("north", 0.0), ("east", 90.0), ("south", 180.0), ("west", 270.0)]
WINDOW_DEG = 22.0          # この角度内に入ったら検知
RELEASE_EXTRA_DEG = 15.0   # WINDOW + この値以上離れたら再アーム（遊び）
COOLDOWN_SEC = 2.0         # 送信後の最低休止時間
LOOP_SLEEP_MS = 120        # ループ周期

# ノイズフィルタ
EMA_ALPHA = 0.2              # 角度の追従性（小さいほどノイズに強い）
STABLE_COUNT_THRESHOLD = 5   # 何回連続で同じ方向なら発火するか

# =========================================================
# グローバル変数
# =========================================================
pending_dir = None
pending_at = 0.0
last_send_try = 0.0
i2c = I2C(I2C_BUS, sda=Pin(SDA_PIN), scl=Pin(SCL_PIN), freq=I2C_FREQ)

# =========================================================
# Wi-Fi / WebREPL 設定読み込み / 送信関数群 (省略なし)
# =========================================================
def load_secrets():
    wifi_list = []
    enable_webrepl = True
    webrepl_port = 8266
    try:
        import secrets
        wifi_list = getattr(secrets, "WIFI_LIST", [])
        enable_webrepl = getattr(secrets, "ENABLE_WEBREPL", True)
        webrepl_port = getattr(secrets, "WEBREPL_PORT", 8266)
    except Exception as e:
        if DEBUG_MODE: print("[secrets] load failed:", repr(e))
    return wifi_list, enable_webrepl, webrepl_port

def wifi_connect(timeout_sec=20):
    wifi_list, _, _ = load_secrets()
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        if DEBUG_MODE: print("WiFi already connected. IP:", wlan.ifconfig()[0])
        return wlan
    if not wifi_list:
        raise RuntimeError("WIFI_LIST is empty")
    for item in wifi_list:
        ssid = item.get("ssid")
        pw = item.get("password")
        if not ssid: continue
        try:
            if DEBUG_MODE: print(" - try SSID:", ssid)
            wlan.connect(ssid, pw)
            t0 = time.time()
            while not wlan.isconnected():
                if time.time() - t0 > timeout_sec: break
                time.sleep(0.5)
            if wlan.isconnected():
                print("Connected. IP:", wlan.ifconfig()[0])
                # バッテリーモード: Wi-Fiパワーセーブ有効化
                if BATTERY_MODE:
                    try:
                        wlan.config(pm=WIFI_PM_POWERSAVE)
                        print("[POWER] WiFi power-save enabled")
                    except Exception as e:
                        print("[POWER] WiFi PM set failed:", e)
                return wlan
        except Exception as e:
            if DEBUG_MODE: print("   connect error:", ssid, repr(e))
    raise RuntimeError("WiFi connect failed")

def wifi_disconnect():
    """Wi-Fiを切断しラジオをOFFにして消費電力を削減する"""
    wlan = network.WLAN(network.STA_IF)
    try:
        wlan.disconnect()
    except:
        pass
    wlan.active(False)
    if DEBUG_MODE: print("[WiFi] Radio OFF")

def start_webrepl_if_enabled():
    _, enable_webrepl, _ = load_secrets()
    if not enable_webrepl: return False
    try:
        import webrepl_cfg
        passwd = webrepl_cfg.PASS
        import webrepl
        webrepl.start(passwd)
        if DEBUG_MODE: print("[webrepl] started")
        return True
    except Exception as e:
        print("[webrepl] start failed:", repr(e))
        return False

def check_and_update():
    wlan = network.WLAN(network.STA_IF)
    if not wlan.isconnected(): return
    for host in UPDATE_HOSTS:
        try:
            res = requests.get(host + "/version.json", timeout=5)
            if res.status_code != 200:
                res.close()
                continue
            server_info = res.json()
            res.close()
            new_v = server_info.get("version")
            current_v = None
            try:
                with open(LOCAL_VERSION_FILE, "r") as f:
                    local_info = ujson.load(f)
                    current_v = local_info.get("version")
            except:
                pass

            # --- 整数バージョン比較（世代戻り防止） ---
            try:
                if new_v is not None and current_v is not None:
                    if int(new_v) <= int(current_v):
                        if DEBUG_MODE: print("[OTA] v{} <= v{}, skip".format(new_v, current_v))
                        return
            except ValueError:
                # 整数変換できない場合（旧形式からの移行時）は通過させる
                if DEBUG_MODE: print("[OTA] version parse fallback, proceeding")

            print("[OTA] New version {} found (current: {}). Syncing files...".format(new_v, current_v))
            files_to_sync = server_info.get("files", [])
            for filename in files_to_sync:
                print(" - Downloading: {}".format(filename))
                r = requests.get(host + "/" + filename, timeout=10)
                if r.status_code == 200:
                    with open(filename, "w") as f:
                        f.write(r.text)
                r.close()
            with open(LOCAL_VERSION_FILE, "w") as f:
                ujson.dump(server_info, f)
            print("[OTA] Update complete. Resetting...")
            time.sleep(1)
            machine.reset()
        except Exception as e:
            if DEBUG_MODE: print("[OTA] Error from {}: {}".format(host, e))

def sinric_contact_event(device_id: str, state: str):
    url = SINRIC_API_URL_FMT.format(device_id=device_id)
    headers = {"X-SINRIC-API-KEY": SINRIC_API_KEY, "Content-Type": "application/json"}
    payload = {"type": "event", "action": "setContactState", "value": ujson.dumps({"state": state})}
    r = None
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=5)
        return getattr(r, "status_code", None), r.text
    finally:
        if r: r.close()

def pi3_send(direction: str):
    payload = {"dir": direction}
    r = None
    try:
        r = requests.post(PI3_URL, headers={"Content-Type": "application/json"}, json=payload, timeout=5)
        return getattr(r, "status_code", None), r.text
    finally:
        if r: r.close()

# --------------------------
# Compass制御
# --------------------------
def init_qmc():
    try:
        i2c.writeto_mem(ADDR, 0x0A, b'\x80')
        time.sleep(0.1)
        i2c.writeto_mem(ADDR, 0x09, b'\xD1')
        time.sleep(0.02)
        i2c.writeto_mem(ADDR, 0x0B, b'\x01')
        time.sleep(0.02)
    except OSError as e:
        print("init_qmc error:", e)

def read_status():
    return i2c.readfrom_mem(ADDR, 0x06, 1)[0]

def read_xyz_once():
    d = i2c.readfrom_mem(ADDR, 0x00, 6)
    x = (d[1] << 8) | d[0]
    y = (d[3] << 8) | d[2]
    z = (d[5] << 8) | d[4]
    if x > 32767: x -= 65536
    if y > 32767: y -= 65536
    if z > 32767: z -= 65536
    return x, y, z

def read_xyz_stable():
    for _ in range(READ_RETRY):
        t0 = time.ticks_ms()
        while True:
            try:
                if read_status() & 0x01: break
            except OSError: pass
            if time.ticks_diff(time.ticks_ms(), t0) > DRDY_WAIT_MS: break
            time.sleep_ms(DRDY_POLL_MS)
        try:
            x, y, z = read_xyz_once()
            if x == BAD_MIN or (x == 0 and y == 0): continue
            return x, y, z
        except OSError: continue
    init_qmc()
    raise OSError("I2C Stable Read Failed")

def ema_angle(prev_deg, new_deg, alpha):
    if prev_deg is None: return new_deg
    pr, nr = math.radians(prev_deg), math.radians(new_deg)
    mx = (1.0 - alpha) * math.cos(pr) + alpha * math.cos(nr)
    my = (1.0 - alpha) * math.sin(pr) + alpha * math.sin(nr)
    out = math.degrees(math.atan2(my, mx))
    return out + 360.0 if out < 0 else out

def deg_from_xy(x, y):
    deg = math.degrees(math.atan2(y, x))
    return deg + 360.0 if deg < 0 else deg

def ang_diff(a, b):
    d = abs(a - b) % 360.0
    return 360.0 - d if d > 180.0 else d

def decide_direction(deg):
    best, best_d = None, 999.0
    for name, center in TARGETS:
        d = ang_diff(deg, center)
        if d < best_d:
            best_d = d
            best = name
    return (best, best_d) if best_d <= WINDOW_DEG else (None, best_d)

def enqueue_fire(direction: str):
    global pending_dir, pending_at
    pending_dir, pending_at = direction, time.time()
    print("[QUEUE] ->", direction)

def try_send_pending():
    global pending_dir, pending_at, last_send_try
    if pending_dir is None: return
    now = time.time()
    if (now - last_send_try) < PI3_SEND_INTERVAL_SEC: return
    last_send_try = now
    d = pending_dir
    try:
        if MODE == "pi3":
            st, body = pi3_send(d)
            print("[SENT]", d, st)
        else:
            st, body = sinric_contact_event(DEVICE_IDS[d], "open")
            print("[SENT]", d, st)
        pending_dir = None
    except Exception as e:
        print("[SEND_ERR]", e)
        if (now - pending_at) > 3.0: pending_dir = None


# --------------------------
# Main Loop
# --------------------------
def main():
    global last_update_check, DEBUG_MODE
    print("Starting Mode:", MODE, "(DEBUG:{}, BATTERY:{})".format(DEBUG_MODE, BATTERY_MODE))

    # --- 起動時: Wi-Fi接続 → OTA確認（フルクロックで実行） ---
    wlan = wifi_connect()

    # --- バッテリーモード: Wi-Fi接続後にCPU周波数低減 ---
    if BATTERY_MODE:
        machine.freq(BATTERY_CPU_FREQ)
        print("[POWER] CPU freq: {}MHz".format(machine.freq() // 1_000_000))
        # バッテリーモード時はデバッグ出力を抑制
        DEBUG_MODE = False
    check_and_update()
    last_update_check = time.time()

    if BATTERY_MODE:
        # バッテリーモード: WebREPL不要（メモリ・CPU節約）
        print("[POWER] WebREPL skipped (battery mode)")
    else:
        start_webrepl_if_enabled()

    init_qmc()

    last_fired_dir, last_fired_at, armed = None, 0.0, True
    deg_smoothed, stable_dir, stable_count, last_print_ms = None, None, 0, 0
    wifi_check_interval = 10  # Wi-Fi死活監視の間隔(秒)
    last_wifi_check = time.time()

    print("Main Loop Started.")

    while True:
        try:
            now = time.time()

            # --- Wi-Fi死活監視: 切れていたら自動再接続 ---
            if (now - last_wifi_check) >= wifi_check_interval:
                last_wifi_check = now
                wl = network.WLAN(network.STA_IF)
                if not wl.isconnected():
                    print("[WiFi] Lost! Reconnecting...")
                    try:
                        wifi_connect(timeout_sec=10)
                    except Exception as e:
                        print("[WiFi] Reconnect failed:", e)

            if (now - last_update_check) > UPDATE_INTERVAL_SEC:
                check_and_update()
                last_update_check = now

            # 生データを取得
            x_raw, y_raw, z_raw = read_xyz_stable()

            # --- 鉄部補正適用 ---
            x = x_raw - X_OFFSET
            y = y_raw - Y_OFFSET

            # 補正後のx, yで角度を計算
            deg_raw = (deg_from_xy(x, y) - NORTH_DEG_RAW + 360.0) % 360.0
            deg_smoothed = ema_angle(deg_smoothed, deg_raw, EMA_ALPHA)
            direction, d = decide_direction(deg_smoothed)

            if DEBUG_MODE:
                now_ms = time.ticks_ms()
                if time.ticks_diff(now_ms, last_print_ms) > 500:
                    print("raw:{:5.1f} sm:{:5.1f} dir:{:5s} d:{:4.1f}".format(
                        deg_raw, deg_smoothed, str(direction), d
                    ))
                    last_print_ms = now_ms

            if direction == stable_dir and direction is not None:
                stable_count += 1
            else:
                stable_dir = direction
                stable_count = 1

            if stable_count >= STABLE_COUNT_THRESHOLD and armed:
                if (last_fired_dir != stable_dir) or ((now - last_fired_at) >= COOLDOWN_SEC):
                    enqueue_fire(stable_dir)
                    last_fired_dir, last_fired_at, armed = stable_dir, now, False
                stable_count = STABLE_COUNT_THRESHOLD

            if not armed:
                if direction != last_fired_dir:
                    if d > (WINDOW_DEG + RELEASE_EXTRA_DEG):
                        armed = True
                        print("[ARMED] Ready")

            try_send_pending()

        except Exception as e:
            print("LOOP ERR:", e)
            time.sleep(0.1)

        time.sleep_ms(LOOP_SLEEP_MS)

if __name__ == "__main__":
    main()
