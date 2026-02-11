# PicoW Deployment Server

Raspberry Pi Pico W のプログラムをネットワーク経由で更新するための配信サーバです。

## フォルダ構成
- `files/`: 配布したいファイルを置く場所です。ここに置いたファイルが Web 経由で見えるようになります。
- `picow-deploy.service`: 自動起動用のサービス設定ファイル（`/etc/systemd/system/` にコピー済み）

## 使い方
1. PicoW に送りたい `main.py` などのファイルを `/home/yasutoshi/projects/11.picow_deploy/files/` に配置します。
2. PicoW から以下の URL でアクセスします。
   - 自宅の場合: `http://raspberrypi3modelB.local:8080/main.py`
   - 実家の場合: `http://jikkaPi3.local:8080/main.py`

## サービスの管理コマンド
- 停止: `sudo systemctl stop picow-deploy`
- 起動: `sudo systemctl start picow-deploy`
- ステータス確認: `sudo systemctl status picow-deploy`
- ログ確認: `journalctl -u picow-deploy -f`
