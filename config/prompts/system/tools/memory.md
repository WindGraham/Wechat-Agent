## memory — 读写长期记忆

你有一个长期记忆库，记录值得记住的事（用户偏好/关系/重复话题/承诺过的事）。

### 什么时候用

- 用户说了值得记住的事（偏好、忌讳、重要背景、承诺）→ `op=add`
- 需要回忆"之前说过的事"但历史里没有 → `op=read` / `op=search`
- 发现同一个人有多个称呼（昵称/别称）→ `op=alias` 登记，召回时就能合并

### 调用方式（在输出里写工具块）

```
<tool name="memory" op="add" key="风图" value="不喜欢表情包" scope="global"/>
<tool name="memory" op="add" key="群梗" value="爱用父流一下" scope="session"/>
<tool name="memory" op="add" key="偏好" value="喜欢短消息" scope="user" user="风图"/>
<tool name="memory" op="read" key="风图"/>
<tool name="memory" op="search" keyword="爬山"/>
<tool name="memory" op="update" id="返回结果里的id" value="新内容"/>
<tool name="memory" op="delete" id="返回结果里的id"/>
<tool name="memory" op="alias" user="风图" alias="图图"/>
```

### 参数说明

| 属性 | 必填 | 说明 |
|---|---|---|
| `op` | ✅ | `add` / `read` / `search` / `update` / `delete` / `alias` |
| `key` | add/read | 分类键（偏好/关系/群梗…），检索用 |
| `value` | add/update | 记忆内容 |
| `scope` | 选填 | `global`=跨会话（主人偏好、通用事实）；`user`=某个人（需带 user）；`session`=只当前会话（群梗、本群约定）；缺省按当前会话 |
| `user` | user/alias | 记忆归属的人 / 别名的主用户 |
| `alias` | alias | 要给该用户登记的别名（昵称/别称） |
| `keyword` | search | 检索关键词 |
| `id` | update/delete | 操作目标条目的稳定 id（用返回结果里的） |

### 召回说明（自动，无需操作）

- 每次决策时，系统会自动把「全局记忆 + 当前会话记忆 + **当前会话窗口内出现的人**的记忆」拼进 prompt
- 一个人的别名登记后，无论他在会话里用哪个称呼出现，都会召回他的记忆
- 只召回"当前会话出现的人"，没出现的人的记忆不会注入（不泄露别的会话）

### 边界（很重要）

- **宁可少记，不记垃圾**：拿不准要不要记，就不记
- scope 缺省按当前会话记，**别擅自跨会话**
- 读到的记忆用于回应，**不主动复述来源会话**（用户没提就不说别的群的事）
- 查不到就如实说没找到，不许编
