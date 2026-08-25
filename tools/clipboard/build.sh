#!/bin/bash
# 构建 tools/clipboard/dex/clip.dex（Android 15 剪贴板读取：一次性 ClipIO + 常驻 ClipIOServer）
# 依赖：javac（JDK）+ d8（用 r8.jar 里的 D8）。全程反射，无需 android.jar。
set -e
cd "$(dirname "$0")"
R8=r8.jar
if [ ! -f "$R8" ]; then
  echo "下载 r8（d8）..."
  curl -sSL -o "$R8" \
    "https://dl.google.com/dl/android/maven2/com/android/tools/r8/9.4.14/r8-9.4.14.jar"
fi
rm -rf out dex && mkdir -p out dex
echo "1/2 javac..."
javac -d out ClipIO.java ClipIOServer.java
echo "2/2 d8..."
java -cp "$R8" com.android.tools.r8.D8 --release --output dex out/ClipIO.class out/ClipIOServer.class
cp dex/classes.dex dex/clip.dex
echo "✅ 已生成 tools/clipboard/dex/clip.dex"
