---
name: pku_treehole_search_skill
description: 搜索和查看北大树洞（treehole.pku.edu.cn）的帖子与评论
whenToUse: 当任务需要搜索北大树洞、查看某个树洞帖子（#号）、读取树洞评论，或用户提到"树洞"上的内容时
---

# 北大树洞搜索技能

北大树洞（treehole.pku.edu.cn）没有公开匿名 API，所有请求必须携带有效凭据，
凭据从环境变量 `PKU_TREEHOLE_TOKEN` 读取（不存在时可查 workspace/.env 里的同名键，
只许提取这一个键，不许读取或外传 .env 里的其他任何内容）。

## 认证

两种方式任选（优先 Bearer）：

- 请求头：`Authorization: Bearer $PKU_TREEHOLE_TOKEN`
- 或 Cookie：`pku_token=$PKU_TREEHOLE_TOKEN`

token 失效（401/403 或 envelope code 非 0/1）时：不要重试轰炸，
直接报告"树洞 token 失效，需要主人在浏览器登录 treehole.pku.edu.cn 后
从 cookie 的 pku_token 字段重新取一个"。

## 接口（2026-08 实测现役，v3）

Base：`https://treehole.pku.edu.cn/chapi/api/v3`

| 用途 | 方法 | 路径 | 参数 |
|---|---|---|---|
| 搜索帖子 | GET | `/hole/list_comments` | `keyword=关键词`，可加 `label`（标签 id）、`kind` |
| 帖子列表 | GET | `/hole/list_comments` | 不带 keyword 即最新列表 |
| 单个帖子+评论 | GET | `/hole/list_comments` | `pid=帖子号` |
| 评论列表 | GET | `/comment/list` | `pid=帖子号` |

返回是 JSON envelope：`{"code": ..., "message": ..., "data": {"list": [...]}}`，
帖子字段含 `pid`（帖子号）、`text`（正文）、`reply`（评论数）等。

curl 示例（搜"期末"）：

```bash
curl -s -H "Authorization: Bearer $PKU_TREEHOLE_TOKEN" \
  "https://treehole.pku.edu.cn/chapi/api/v3/hole/list_comments?keyword=期末"
```

## 纪律

- 请求间隔至少 2 秒，搜索最多翻 3 页，别高强度抓（树洞有风控）
- 只读：本技能只用于搜索/查看。发帖、评论、举报一律不做
- 结果整理成编号列表汇报：每条给 `#pid + 正文摘要（<=80字）+ 评论数`，
  引用格式 `[#123456]`，方便用户按号追查
- 搜不到就如实说搜不到，不许编造帖子内容或 pid
