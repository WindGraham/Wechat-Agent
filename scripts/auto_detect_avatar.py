import cv2
import numpy as np

# 读取完整的个人资料页截图
img_path = "workspace/real_user_profile_page_final2.png"
img = cv2.imread(img_path)
h, w = img.shape[:2]

# 限定资料页头部区域 (y: 150~600, x: 0~500)
roi = img[150:600, 0:500]

# 转灰度
gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

# 使用 Canny 边缘检测提取头像框的矩形轮廓
edges = cv2.Canny(gray, 50, 150)

# 寻找轮廓
contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

avatar_box = None
max_area = 0

for cnt in contours:
    x, y, w_box, h_box = cv2.boundingRect(cnt)
    # 微信个人资料页头像一般是正方形，宽高在 150~350 像素之间
    area = w_box * h_box
    aspect_ratio = float(w_box) / h_box
    
    if 0.85 <= aspect_ratio <= 1.15 and 150 <= w_box <= 350 and 150 <= h_box <= 350:
        if area > max_area:
            max_area = area
            # 还原到全局坐标
            avatar_box = (x, y + 150, w_box, h_box)

if avatar_box:
    x, y, bw, bh = avatar_box
    print(f"[+] CV 轮廓算法成功精准定位头像矩形框: x={x}, y={y}, w={bw}, h={bh}")
    
    # 额外精确裁剪（内缩 2 像素避开边框线）
    crop = img[y+2:y+bh-2, x+2:x+bw-2]
    out_path = "workspace/avatar_cv_exact.png"
    cv2.imwrite(out_path, crop)
    print(f"[★★★] 完美像素级头像已存入: {out_path}")
else:
    print("[-] 矩形检测未找到，尝试色差梯度分割...")
    # 备用方案：寻找饱和度/色彩标准差最大的正方形区域
    best_std = 0
    best_rect = None
    for y in range(150, 450, 10):
        for x in range(30, 200, 10):
            box = img[y:y+220, x:x+220]
            if box.shape[0] == 220 and box.shape[1] == 220:
                std = np.std(box)
                if std > best_std:
                    best_std = std
                    best_rect = (x, y, 220, 220)
    if best_rect:
        x, y, bw, bh = best_rect
        crop = img[y:y+bh, x:x+bw]
        out_path = "workspace/avatar_cv_exact.png"
        cv2.imwrite(out_path, crop)
        print(f"[+] 色彩梯度分割精确定位: {out_path}")

