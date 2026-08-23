# Tool Argument Normalization Contract

R3.4 的工具参数管线固定为以下三个阶段，后续阶段不得绕过：

1. 严格解析一个有限大小、有限深度且只含有限 JSON 值的 object；拒绝重复 key、NaN、Infinity、
   非 object 根节点和 malformed JSON，不做补括号、替换引号或截断尾部等修补。
2. 只执行字段 Schema 通过 `x-edu-agent-normalize` 明确授权的确定性转换。每个输入节点只遍历一次，
   不循环尝试不同解释。
3. 对规范化后的完整 object 执行 Draft 2020-12 JSON Schema 校验。只有零 violation 的参数才会进入
   effect/resource key、审批、幂等键和 handler 边界。

工具注册期和运行时固定使用 Draft 2020-12；Schema 可以省略 `$schema`，但若显式声明其他 dialect 则注册失败，
避免同一关键字在不同 draft 下产生不同解释。

## 允许转换表

| Rule ID | Schema 目标 | 唯一接受的源值 | 结果 |
|---|---|---|---|
| `string_to_integer_v1` | `type: integer` | JSON 十进制整数词法；无空白、前导 `+`、前导零、小数或指数，且不接受 `-0` | Python `int` |
| `string_to_number_v1` | `type: number` | 完整、有限的 JSON number 词法；无空白、前导 `+` 或前导零 | Python `int` 或有限 `float` |
| `string_to_boolean_v1` | `type: boolean` | 仅精确小写 `true` 或 `false` | Python `bool` |
| `json_string_to_array_v1` | `type: array` | 首尾无额外字符、根为 array 的严格 JSON 字符串 | Python `list`，随后递归按子 Schema 处理 |
| `json_string_to_object_v1` | `type: object` | 首尾无额外字符、根为 object 的严格 JSON 字符串；重复 key 拒绝 | Python `dict`，随后递归按子 Schema 处理 |
| `integral_number_to_integer_v1` | `type: integer` | JSON Schema 语义下有限、安全范围内且无小数部分的 number（如 `4.0`）；拒绝负零 | Python `int`；这是数值等价的 handler 表示规范化，不开放字符串 repair |

字段未声明规则时，即使值看起来可转换也保持原值并由 Schema 拒绝。规则与字段目标类型必须在工具注册时
匹配。当前安全策略只允许 `read`/`pure` effect 应用 repair；`write`、`conditional_write`、
`code_execution`、`interactive` 和 `unknown` 一律记录 `rejected_effect_policy` 后按原值校验失败。
`integral_number_to_integer_v1` 由字段的 `type: integer` 直接授权，保持 JSON Schema 已认定的同一数值；
它仍受上述 effect、ID、enum、日期、敏感字段和审批语义策略约束，不会把非积分 number、boolean 或
string 解释为 integer。被策略拒绝的非规范 Python 表示不会进入 handler。

规范化声明只允许出现在运行时能静态定位的 `properties`、schema 形式的 `additionalProperties`、`items`
和 `prefixItems` 路径。`allOf`/`anyOf`/`oneOf`、条件分支、`$defs`/引用和 `patternProperties` 中的声明
在注册期拒绝，避免按实例猜测授权分支。object 默认且强制拒绝未知字段；动态 map 必须用
`additionalProperties: { ...schema... }` 显式约束，禁止 `additionalProperties: true`。

## 禁止猜测表

| 输入类别 | 禁止行为 |
|---|---|
| ID、学号、用户名和 scope 字段 | 不做字符串/数值互转，不删除前导零，不补 tenant/course/student 身份 |
| 自由文本、源码、stdin 和搜索词 | 不 trim、不改 Unicode、不解析为 JSON、不做数值或布尔转换 |
| enum | 不做大小写折叠、相似匹配、别名映射或字符串/数值互转 |
| 日期时间 | 不猜时区、格式、世纪或本地时间，不把时间戳转字符串 |
| `null`、缺失字段和 `default` | 不把空串转 null，不注入业务默认值；`default` 仅是模型提示，不改变请求 |
| malformed JSON | 不补括号/引号/逗号，不删除前后说明文字，不提取其中的子串 JSON |
| 非有限数值和非 JSON Python 值 | 不接受 NaN/Infinity、bytes、tuple、set、自定义对象、循环引用或非字符串 key |
| 超限输入 | 不截断后继续执行；整体结构化拒绝 |

每个 JSON 字符串容器解码时先执行局部边界检查，全部规范化完成后再对组合后的 object 重新检查总字节数、
深度、节点数和单容器成员数。因此多个分别合法的嵌套字符串不能组合绕过整体上限。

## Repair Audit

每次候选转换记录 JSON Pointer、源/目标 JSON 类型、rule ID、`applied` 或拒绝结果，以及源值的
canonical SHA-256。审计不保存原值；字段分类为 credential、student PII 或 free text 时只标记
`sensitive: true` 并保留摘要。工具结果、持久化 audit 和 Trace 使用同一份无正文记录。

单个 tool call 只运行一次规范化遍历；每次 executor 调用最多扣减一次 tool-call 预算，同一个 call id 最多
扣减一次参数 repair/retry 预算。结构化错误回灌模型；handler 永远只接收完整校验通过的 object。
