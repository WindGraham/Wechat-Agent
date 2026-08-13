#!/bin/bash
# 编译 CaptureServer.java -> classes.dex
# 前置：安装 JDK + Android SDK，设置 ANDROID_HOME 和 build-tools 里的 d8
set -e
cd "$(dirname "$0")"

SDK="${ANDROID_HOME:?请设置 ANDROID_HOME}"
ANDROID_JAR="$SDK/platforms/android-35/android.jar"
[ -f "$ANDROID_JAR" ] || { echo "找不到 $ANDROID_JAR，请确认 SDK 安装了 android-35 platform"; exit 1; }
D8=$(ls "$SDK"/build-tools/*/d8 2>/dev/null | sort -V | tail -1)
[ -n "$D8" ] || { echo "找不到 d8，请确认 SDK 安装了 build-tools"; exit 1; }

echo "1/2 javac 编译..."
rm -rf build && mkdir -p build/classes
javac -source 1.8 -target 1.8 \
  -bootclasspath "$ANDROID_JAR" \
  -d build/classes src/CaptureServer.java

echo "2/2 d8 转 dex..."
"$D8" --lib "$ANDROID_JAR" --release --output build \
  build/classes/com/wechatagent/capture/CaptureServer.class

echo "✅ 编译完成: build/classes.dex"
ls -la build/classes.dex
