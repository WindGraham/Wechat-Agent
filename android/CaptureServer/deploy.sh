#!/bin/bash
# 部署 + 运行 CaptureServer 到真机（root 设备）
set -e
cd "$(dirname "$0")"
ADB="${ADB:-/media/data_old/Wechat-Agent/tools/platform-tools/adb}"
SERIAL="${SERIAL:-cf04642e}"

[ -f build/classes.dex ] || { echo "先运行 build.sh 编译"; exit 1; }

echo "1/3 push dex..."
"$ADB" -s "$SERIAL" push build/classes.dex /data/local/tmp/capture.dex

echo "2/3 appops 预授权（root）..."
"$ADB" -s "$SERIAL" shell appops set com.android.shell PROJECT_MEDIA android:project_media allow

echo "3/3 运行服务（stdout 接电脑，stdin 发命令）..."
echo "服务已在后台运行，用 screen_capture.py 连接抓帧"
"$ADB" -s "$SERIAL" exec-out \
  CLASSPATH=/data/local/tmp/capture.dex app_process / com.wechatagent.capture.CaptureServer
