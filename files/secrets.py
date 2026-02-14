# Wi-Fi設定を複数登録しておく（上から順に接続を試す）
WIFI_LIST = [
    {"ssid": "TP-Link_9F26", "password": "41051528"},
    {"ssid": "TP-Link_A208", "password": "15405173"},
    # 予備（スマホテザリング等）
     {"ssid": "Pixel_5274", "password": "11112222"},
]

# WebREPL起動を有効にするか

# =========================
# WebREPL設定
# =========================
ENABLE_WEBREPL = True

WEBREPL_PORT = 8266  # 通常8266（変更不要）
