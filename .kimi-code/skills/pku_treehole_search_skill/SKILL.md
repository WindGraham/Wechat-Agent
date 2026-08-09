---
name: pku_treehole_search_skill
description: 搜索和查看北大树洞（treehole.pku.edu.cn）的帖子与评论
whenToUse: 当任务需要搜索北大树洞、查看某个树洞帖子（#号）、读取树洞评论，或用户提到"树洞"上的内容时
---

# 北大树洞搜索技能

北大树洞（treehole.pku.edu.cn）没有公开匿名 API，所有请求必须携带凭据。
两个凭据从环境变量读（不存在时可查 workspace/.env 的同名键，
只许提取这两个键，不许读取或外传 .env 里的其他任何内容）：

- `PKU_TREEHOLE_TOKEN`：JWT，约 30 天有效期
- `PKU_TREEHOLE_UUID`：web UUID（过了短信验证的浏览器指纹）

## 请求头（缺一不可，2026-08-09 实测）

少带 `uuid`/`useragent` 会返回 `{"code":40002,"message":"请手机短信验证"}`——
这不是 token 失效，是缺头。完整头：

```
Authorization: Bearer $PKU_TREEHOLE_TOKEN
uuid: $PKU_TREEHOLE_UUID
useragent: pku_web
User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0
```

## 接口（v3，实测现役）

Base：`https://treehole.pku.edu.cn/chapi/api/v3`

| 用途 | 方法 | 路径 | 参数 |
|---|---|---|---|
| 搜索帖子 | GET | `/hole/list_comments` | `keyword=关键词`（需 URL 编码） |
| 最新列表 | GET | `/hole/list_comments` | 不带参数 |
| 单个帖子+评论 | GET | `/hole/list_comments` | `pid=帖子号` |
| 评论列表 | GET | `/comment/list` | `pid=帖子号` |

返回 JSON envelope：成功 `{"code":20000,"data":{"list":[...]}}`，
帖子字段：`pid`（帖子号）、`text`（正文）、`reply`（评论数）、
`likenum`（赞数）、`timestamp`、`tag` 等。

curl 示例（搜"期末"）：

```bash
curl -s -H "Authorization: Bearer $PKU_TREEHOLE_TOKEN" \
     -H "uuid: $PKU_TREEHOLE_UUID" -H "useragent: pku_web" \
     -H "User-Agent: Mozilla/5.0" \
  "https://treehole.pku.edu.cn/chapi/api/v3/hole/list_comments?keyword=%E6%9C%9F%E6%9C%AB"
```

## 失效处理

- 401/403 或连续 code 非 20000：token 到期（30 天）或 uuid 被风控。
  不要重试轰炸，报告"树洞凭据失效，需要主人重新取：
  Edge 打开树洞 → F12 → 网络 → 任一请求的请求标头里的
  authorization(Bearer 后) 和 uuid 两个值"
- 40002 但头已带全：uuid 需要重新过短信验证，同上报告

## 纪律

- 请求间隔至少 2 秒，搜索最多翻 3 页，别高强度抓（树洞有风控）
- 只读：发帖、评论、举报一律不做
- 结果整理成编号列表汇报：每条给 `#pid + 正文摘要（<=80字）+ 评论数`，
  引用格式 `[#123456]`，方便用户按号追查
- 搜不到就如实说搜不到，不许编造帖子内容或 pid
