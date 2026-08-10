## memory — 读写长期记忆

你有一个长期记忆库，记录值得记住的事（用户偏好/关系/重复话题/承诺过的事）。

### 什么时候用

- 用户说了值得记住的事（偏好、忌讳、重要背景、承诺）→ `op=add`
- 需要回忆"之前说过的事"但历史里没有 → `op=read` / `op=search`

### 调用方式（在输出里写工具块）

```
<tool name="memory" op="add" key="风图" value="不喜欢表情包" scope="global"/>
<tool name="memory" op="add" key="群梗" value="爱用父流一下" scope="session"/>
<tool name="memory" op="read" key="风图"/>
<tool name="memory" op="search" keyword="爬山"/>
<tool name="memory" op="update" id="返回结果里的id" value="新内容"/>
<tool name="memory" op="delete" id="返回结果里的id"/>
```

### 参数说明

| 属性 | 必填 | 说明 |
|---|---|---|
| `op` | ✅ | `add` / `read` / `search` / `update` / `delete` |
| `key` | add/read | 分类键（偏好/关系/群梗…），检索用 |
| `value` | add/update | 记忆内容 |
| `scope` | 选填 | `global`=跨会话（主人偏好、通用事实）；`session`=只当前会话（群梗、本群约定）；缺省按当前会话 |
| `keyword` | search | 检索关键词 |
| `id` | update/delete | 操作目标条目的稳定 id（用返回结果里的） |

### 边界（很重要）

- **宁可少记，不记垃圾**：拿不准要不要记，就不记
- scope 缺省按当前会话记，**别擅自跨会话**
- 读到的记忆用于回应，**不主动复述来源会话**（用户没提就不说别的群的事）
- 查不到就如实说没找到，不许编
