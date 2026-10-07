# 关系查询溯源与反事实解释 API

客户端一次请求提交 **多张带主键的关系表快照** 和一个 **受限关系代数计划**，
服务执行查询，并为每条输出返回：

* **全部极小来源行见证集**（minimal witness sets / why-provenance）；
* **布尔溯源表达式**（DNF：见证内部为合取 `∧`，见证之间为析取 `∨`）；
* 可选地，对指定输出计算 **令其消失的全部最小源行删除集**，并对每个删除集
  **在删除后的快照上重算整条计划**，返回重算结果。

技术栈：Python 3.11 · FastAPI · SQLite（幂等响应持久化）· 无外部服务依赖。

---

## 1. 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 用途 | 地址 |
| --- | --- |
| HTTP API | http://localhost:8000 |
| 交互式文档（Swagger） | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health |

幂等响应持久化在 Docker 卷 `provenance-data`（容器内 `/data/provenance.db`）。

### 本地直接运行（不使用 Docker）

```bash
python3 -m pip install -r requirements.txt
PROVENANCE_DB=./data/provenance.db \
  python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
python3 -m pip install pytest
python3 -m pytest -q
```

---

## 2. API

### `POST /query`

请求体（JSON）：

```jsonc
{
  "idempotency_key": "readme-demo-1",        // 必填，绑定本次规范化请求
  "tables": [ ... ],                         // 1..n 张表快照（见下）
  "plan": { ... },                           // 1 个关系代数计划
  "explain": { "index": 0 }                  // 可选：对某条输出做反事实解释
}
```

**表快照**

```jsonc
{
  "table": "orders",
  "primary_key": ["oid"],                    // 必填，非空；支持复合主键
  "columns": [                               // 可省略（从行推断）
    {"name": "oid", "type": "int"},          // 类型: int/float/string/bool/any
    {"name": "cid", "type": "int"},
    {"name": "amount", "type": "int"}
  ],
  "rows": [ {"oid": 1, "cid": 10, "amount": 50}, ... ]
}
```

**关系代数计划节点**

| 节点 | 形式 |
| --- | --- |
| 扫描 | `{"type":"scan","table":"orders"}` |
| 选择/过滤 | `{"type":"filter","input":<node>,"predicate":<谓词>}` |
| 投影 | `{"type":"project","input":<node>,"columns":["a",{"from":"b","as":"c"}]}` |
| 等值连接 | `{"type":"join","left":<node>,"right":<node>,"on":[{"left":"cid","right":"cid"}]}` |
| 分组聚合 | `{"type":"aggregate","input":<node>,"group_by":["cid"],"aggregates":[{"fn":"sum","column":"amount","as":"total"}]}` |

* 谓词树：叶子 `{"column":"amount","op":">=","value":40}`；
  算子 `= <> < <= > >=` 以及 `is_null` / `is_not_null`；
  布尔组合 `{"and":[...]}`、`{"or":[...]}`、`{"not":{...}}`。
  谓词按 SQL 三值逻辑求值：与 `NULL` 比较得到 UNKNOWN，不满足选择条件。
* 聚合函数：`count`（`column` 可写 `"*"` 或具体列，后者忽略 NULL）、
  `sum` / `min` / `max`（自动忽略 NULL；全 NULL 时 `sum/min/max` 为 `null`）。
* 等值连接的连接列按 USING 语义合并；其余右表列名冲突时自动限定为
  `right.<列>`（可用 `right_alias` 指定限定名），或在投影中重命名。
* 计划只能引用已提交的表与其字段，否则返回 `ILLEGAL_REFERENCE` / `UNKNOWN_TABLE`。

**反事实目标选择器**（`explain`，二选一）：

* `{"index": 0}` —— 输出按全部列全序排序后的稳定下标；
* `{"row": {...}}` —— 与输出列完全相等的一行。

### 响应约定

* 所有响应都是 **规范化（canonical）JSON**：对象键按码点排序、无多余空白、
  无尾随换行；成功响应附带 `X-Idempotent-Replay: true/false`。
* 幂等：`(idempotency_key, 规范化请求指纹)` 绑定。重复请求 **逐字节一致**；
  同一键但规范化计划/快照/目标不同 → `409 IDEMPOTENCY_KEY_CONFLICT`。
  JSON 中对象键的书写顺序不影响绑定（指纹基于规范化 JSON 计算）。

---

## 3. 实际请求 / 响应示例

### 请求：过滤 + 等值连接 + 投影，并解释输出行 0

```bash
curl -s -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{
  "idempotency_key": "readme-demo-1",
  "tables": [
    {"table": "orders", "primary_key": ["oid"],
     "columns": [{"name":"oid","type":"int"},{"name":"cid","type":"int"},{"name":"amount","type":"int"}],
     "rows": [{"oid":1,"cid":10,"amount":50},{"oid":2,"cid":10,"amount":30}]},
    {"table": "customers", "primary_key": ["cid"],
     "columns": [{"name":"cid","type":"int"},{"name":"name","type":"string"}],
     "rows": [{"cid":10,"name":"Alice"}]}
  ],
  "plan": {
    "type": "project",
    "columns": ["name", {"from":"oid","as":"order_id"}, "amount"],
    "input": {
      "type": "filter",
      "predicate": {"column":"amount","op":">=","value":40},
      "input": {
        "type": "join",
        "left": {"type":"scan","table":"orders"},
        "right": {"type":"scan","table":"customers"},
        "on": [{"left":"cid","right":"cid"}]
      }
    }
  },
  "explain": {"index": 0}
}'
```

实际响应（线上传输为单行紧凑 JSON；这里为可读性格式化）：

```json
{
  "status": "ok",
  "symbol_table": [
    {"symbol": "s1", "source": "customers[10]"},
    {"symbol": "s2", "source": "orders[1]"}
  ],
  "result": {
    "columns": ["name", "order_id", "amount"],
    "row_count": 1,
    "rows": [
      {
        "row": {"name": "Alice", "order_id": 1, "amount": 50},
        "witness_count": 1,
        "minimal_witnesses": [
          {
            "labels": ["customers[10]", "orders[1]"],
            "symbols": ["s1", "s2"],
            "conjunction": "s1 ∧ s2"
          }
        ],
        "provenance": {
          "dnf": "(s1 ∧ s2)",
          "dnf_structured": [["s1", "s2"]],
          "labels_structured": [["customers[10]", "orders[1]"]]
        }
      }
    ]
  },
  "counterfactual": {
    "target_index": 0,
    "target": {"name": "Alice", "order_id": 1, "amount": 50},
    "target_provenance": {
      "dnf": "(s1 ∧ s2)",
      "dnf_structured": [["s1", "s2"]],
      "labels_structured": [["customers[10]", "orders[1]"]]
    },
    "minimal_deletion_count": 2,
    "minimal_deletion_sets": [
      ["customers[10]"],
      ["orders[1]"]
    ],
    "scenarios": [
      {
        "delete": ["customers[10]"],
        "target_present": false,
        "result": {"columns": ["name", "order_id", "amount"], "row_count": 0, "rows": []}
      },
      {
        "delete": ["orders[1]"],
        "target_present": false,
        "result": {"columns": ["name", "order_id", "amount"], "row_count": 0, "rows": []}
      }
    ]
  }
}
```

解读：输出行 `(Alice,1,50)` 的唯一极小见证是 `orders[1] ∧ customers[10]`
（两条源行合取）；让它消失的最小删除集有两个：删客户 `customers[10]`
或删订单 `orders[1]`。两种删除都会在重算后使目标消失。

### 分组聚合的数值溯源（count / sum / min / max）

聚合输出行 = 组键 + 各聚合值。每个聚合表达式都为“这一确切数值”贡献一个
见证组件，与“组至少剩一行”的存在性组件合取（逐组件取并集）后极小化：

* `count(*)` / `count(col)`：覆盖 **所有参与计数的行**（后者忽略 NULL）
  —— 一个合取；
* `sum(col)`：枚举 **和为目标值的全部极小非空行组合**（按行而非按值，
  同值行产生不同组合并完整保留；空组合不是见证 —— 零行的和不由任何
  来源行产生）；
* `min(col)` / `max(col)`：枚举 **仍能产生目标极值的极小组合** —— 每个
  等于极值的行各成一个见证。

因此同组订单金额为 50 和 30 时，`count=2`、`sum=80` 只在两行都在时成立，
溯源是单个合取 `s1 ∧ s2`（而不是两条单行见证的析取）；删任一行数值即变，
最小删除集是两个单例，与删除后重算完全一致。

### 请求：分组聚合 + 解释组 `cid=10`

```bash
curl -s -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{
  "idempotency_key": "readme-agg-1",
  "tables": [
    {"table": "orders", "primary_key": ["oid"],
     "columns": [{"name":"oid","type":"int"},{"name":"cid","type":"int"},{"name":"amount","type":"int"}],
     "rows": [
       {"oid":1,"cid":10,"amount":50},
       {"oid":2,"cid":10,"amount":30},
       {"oid":3,"cid":20,"amount":10}
     ]}
  ],
  "plan": {
    "type": "aggregate",
    "input": {"type":"scan","table":"orders"},
    "group_by": ["cid"],
    "aggregates": [
      {"fn":"count","column":"*","as":"n"},
      {"fn":"sum","column":"amount","as":"total"}
    ]
  },
  "explain": {"row": {"cid":10,"n":2,"total":80}}
}'
```

实际响应（线上传输为单行紧凑 JSON；这里为可读性格式化）：

```json
{
  "status": "ok",
  "symbol_table": [
    {"symbol": "s1", "source": "orders[1]"},
    {"symbol": "s2", "source": "orders[2]"},
    {"symbol": "s3", "source": "orders[3]"}
  ],
  "result": {
    "columns": ["cid", "n", "total"],
    "row_count": 2,
    "rows": [
      {
        "row": {"cid": 10, "n": 2, "total": 80},
        "witness_count": 1,
        "minimal_witnesses": [
          {
            "labels": ["orders[1]", "orders[2]"],
            "symbols": ["s1", "s2"],
            "conjunction": "s1 ∧ s2"
          }
        ],
        "provenance": {
          "dnf": "(s1 ∧ s2)",
          "dnf_structured": [["s1", "s2"]],
          "labels_structured": [["orders[1]", "orders[2]"]]
        }
      },
      {
        "row": {"cid": 20, "n": 1, "total": 10},
        "witness_count": 1,
        "minimal_witnesses": [
          {"labels": ["orders[3]"], "symbols": ["s3"], "conjunction": "s3"}
        ],
        "provenance": {
          "dnf": "(s3)",
          "dnf_structured": [["s3"]],
          "labels_structured": [["orders[3]"]]
        }
      }
    ]
  },
  "counterfactual": {
    "target_index": 0,
    "target": {"cid": 10, "n": 2, "total": 80},
    "target_provenance": {
      "dnf": "(s1 ∧ s2)",
      "dnf_structured": [["s1", "s2"]],
      "labels_structured": [["orders[1]", "orders[2]"]]
    },
    "minimal_deletion_count": 2,
    "minimal_deletion_sets": [
      ["orders[1]"],
      ["orders[2]"]
    ],
    "scenarios": [
      {
        "delete": ["orders[1]"],
        "target_present": false,
        "result": {
          "columns": ["cid", "n", "total"],
          "row_count": 2,
          "rows": [
            {"cid": 10, "n": 1, "total": 30},
            {"cid": 20, "n": 1, "total": 10}
          ]
        }
      },
      {
        "delete": ["orders[2]"],
        "target_present": false,
        "result": {
          "columns": ["cid", "n", "total"],
          "row_count": 2,
          "rows": [
            {"cid": 10, "n": 1, "total": 50},
            {"cid": 20, "n": 1, "total": 10}
          ]
        }
      }
    ]
  }
}
```

解读：目标行 `(10,2,80)` 的唯一极小见证是 `orders[1] ∧ orders[2]`；
令它消失的最小删除集是删 `orders[1]` 或 `orders[2]`。两个场景都在删除后
重算了整条计划：组 `cid=10` 仍在，但变成 `(10,1,30)` / `(10,1,50)`，
目标行消失（`target_present: false`）——布尔溯源、最小删除集与重算结果
三者一致。

多见证（DNF 多析取项）在 `sum`/`min`/`max` 出现多种极小组合时产生。例如
同组金额为 `30, 30, 60, -60` 时 `sum=60` 的极小组合是 `{30,30}` 与 `{60}`，
溯源为 `(s1 ∧ s2) ∨ (s3)`；该组含正负混合值，求和是非单调的 —— 删除
**任意一行** 都会改变总和（去掉 30 剩 30、去掉 60 剩 0、去掉 -60 剩 120），
因此最小删除集是四个单例 `["orders[1]"]`、`["orders[2]"]`、`["orders[3]"]`、
`["orders[4]"]`（由删除后重算判定，见下节）；`min`/`max` 则对每个等于
极值的行各给出一个单例见证。

### 混合正负金额的 sum（非单调求和）

同组金额一正一负时，`sum` 对删除不是单调的：删掉一行不属于任何极小
组合的来源行，总和照样会变。此时“令目标行消失的删除集”**不能**由见证
的命中集刻画（命中集是单调近似，会漏报甚至误报），服务先对目标组做候选
约简（见下节），再对剩余候选删除集逐个在删除后的快照上重算整条计划，
以完整目标行是否消失为准，返回全部按包含关系极小的方案。

下面的请求里，组 `cid=10` 的两行金额为 `5` 和 `-5`，合计 `total=0`：

```bash
curl -s -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{
  "idempotency_key": "readme-mixed-1",
  "tables": [
    {"table": "orders", "primary_key": ["oid"],
     "columns": [{"name":"oid","type":"int"},{"name":"cid","type":"int"},{"name":"amount","type":"int"}],
     "rows": [
       {"oid":1,"cid":10,"amount":5},
       {"oid":2,"cid":10,"amount":-5},
       {"oid":3,"cid":20,"amount":7}
     ]}
  ],
  "plan": {
    "type": "aggregate",
    "input": {"type":"scan","table":"orders"},
    "group_by": ["cid"],
    "aggregates": [{"fn":"sum","column":"amount","as":"total"}]
  },
  "explain": {"row": {"cid":10,"total":0}}
}'
```

实际响应（线上传输为单行紧凑 JSON；这里为可读性格式化）：

```json
{
  "status": "ok",
  "symbol_table": [
    {"symbol": "s1", "source": "orders[1]"},
    {"symbol": "s2", "source": "orders[2]"},
    {"symbol": "s3", "source": "orders[3]"}
  ],
  "result": {
    "columns": ["cid", "total"],
    "row_count": 2,
    "rows": [
      {
        "row": {"cid": 10, "total": 0},
        "witness_count": 1,
        "minimal_witnesses": [
          {
            "labels": ["orders[1]", "orders[2]"],
            "symbols": ["s1", "s2"],
            "conjunction": "s1 ∧ s2"
          }
        ],
        "provenance": {
          "dnf": "(s1 ∧ s2)",
          "dnf_structured": [["s1", "s2"]],
          "labels_structured": [["orders[1]", "orders[2]"]]
        }
      },
      {
        "row": {"cid": 20, "total": 7},
        "witness_count": 1,
        "minimal_witnesses": [
          {"labels": ["orders[3]"], "symbols": ["s3"], "conjunction": "s3"}
        ],
        "provenance": {
          "dnf": "(s3)",
          "dnf_structured": [["s3"]],
          "labels_structured": [["orders[3]"]]
        }
      }
    ]
  },
  "counterfactual": {
    "target_index": 0,
    "target": {"cid": 10, "total": 0},
    "target_provenance": {
      "dnf": "(s1 ∧ s2)",
      "dnf_structured": [["s1", "s2"]],
      "labels_structured": [["orders[1]", "orders[2]"]]
    },
    "minimal_deletion_count": 2,
    "minimal_deletion_sets": [
      ["orders[1]"],
      ["orders[2]"]
    ],
    "scenarios": [
      {
        "delete": ["orders[1]"],
        "target_present": false,
        "result": {
          "columns": ["cid", "total"],
          "row_count": 2,
          "rows": [
            {"cid": 10, "total": -5},
            {"cid": 20, "total": 7}
          ]
        }
      },
      {
        "delete": ["orders[2]"],
        "target_present": false,
        "result": {
          "columns": ["cid", "total"],
          "row_count": 2,
          "rows": [
            {"cid": 10, "total": 5},
            {"cid": 20, "total": 7}
          ]
        }
      }
    ]
  }
}
```

解读：`total=0` 的唯一极小见证是非空组合 `orders[1] ∧ orders[2]`（空
组合不会被当作见证，否则溯源会退化成“组存在即可”）。令目标消失的
最小删除集是两个单例：删 `orders[1]` 总和变 `-5`，删 `orders[2]` 总和
变 `5` —— 与删除后重算逐一吻合；另一组 `cid=20` 不受影响。

再如单组金额为 `5, 2, -2`、`sum=5` 时，唯一极小组合是 `{5}`（溯源
`(s1)`），但删 `2` 总和变 `3`、删 `-2` 总和变 `7`，所以最小删除集是
三个单例 `["orders[1]"]`、`["orders[2]"]`、`["orders[3]"]` —— 包括不
出现在任何极小组合中的行。注意非单调求和的删除集不具备向上封闭性：
上例中删除 `{2,-2}` 两行反而让总和回到 `5`，目标行重新出现；每个返回
的方案都经过重算验证且按包含关系极小。

#### 候选约简与搜索预算（金额 `5, 2, -2` 加十条 `0`，共 13 行）

历史行为里，只要目标组的候选行超过 12 行，精确重算搜索就会放弃并退回
见证命中集近似 —— 对 `5, 2, -2` 加十条 `0`、`total=5` 的请求只报告
删除 `5`（唯一极小求和组合），漏掉删 `2` / `-2` 两个同样极小的方案。
现在候选池先做 **候选约简**，再按预算做精确枚举：

* **候选约简（保极小性，不丢方案）**：对目标组的每个聚合表达式，删除后
  不可能改变目标值的组成员称为“惰性成员”——
  `count(*)` 无惰性行；`count(col)` 的 `col IS NULL` 行为惰性；
  `sum(col)` 的 `col IS NULL` 或 `col = 0` 行为惰性（删掉零行总和不变，
  十条补位 `0` 全部约简掉）；`min/max(col)` 中非 NULL 且不等于目标极值
  的行为惰性。只在惰性成员见证中出现的来源行标签先移出候选池。
* **选择性惰性标签门（连接场景下精确放回，不做“全有/全无”放宽）**：上面的
  “按成员数值约简”对扫描、过滤和一对一连接严格成立（删一个活跃标签恰好
  移除一个活跃成员）；但连接（尤其自连接）里同一个来源标签可能喂给多个
  成员。旧实现遇到“单删某活跃标签不致死”时会把候选池**一律放宽回全量
  成员标签**——当组里还有大量彼此独立的零值成员（例如 18 条各自成组的零
  值自连接对）时，池会被误撑过 20 标签上限而错误返回 422。现在的门完全
  基于见证结构与聚合值模拟（**不消耗整计划重算预算**）逐个惰性标签裁决：
  只有当存在某个活跃标签子集 `A`，使得删除 `A` 后①全部活跃成员都被移除、
  ②**仅靠存活的惰性成员仍能完整复现目标行**、③这些惰性存活成员确实只能
  由惰性标签命中、④`A` 中至少一个标签是不可省去的，并且该惰性标签能极小
  地命中其中一条存活见证时，才把它放回候选池。于是：维持不了目标行的
  补位零成员（如下例目标 `total=7` 时的 18 条零自连接对）一律留在池外，
  而零目标自连接例子里撑住 `total=0` 的 `ledger[4]` 会被精确放回（双标签
  方案不会漏报）。该结构性枚举本身设有 200 000 个子问题上限，极端输入下
  超限才保守放宽回全量池。为守住这个上限，枚举带两道**不改变结果**的
  见证依赖剪枝：其一，若全部惰性成员的任意非空子集都复现不了目标行
  （例如目标 `total≠0` 而惰性成员全是零值），条件②永远不可能成立，
  惰性标签被直接证明无关，0 次掩码检查返回活跃池；其二，掩码增长时
  惰性存活集只缩不增，一旦当前存活成员的任意非空子集都复现不了目标行，
  整棵超集子树被剪除——共享标签（如自连接的公共左侧行）会把依赖它的
  零值成员一起删掉，含该标签的 2^k 个超集掩码因此被整体跳过，不再
  白白消耗 20 万掩码额度（见下文"共享左侧标签的自连接求和"例）。
* **精确枚举（不利用失败剪枝）**：按删除集大小递增枚举；**只有已找到
  解的超集才跳过**（超集必不极小），某个删除集没让目标消失，绝不据此
  跳过它的超集 —— 混合正负求和下，多删一行可能正好把总和改离目标值。
* **两道搜索预算**：候选池（约简 + 选择性惰性标签门后）上限
  **20 行**（否则整体 2^n 不可行）；单次反事实搜索的整计划重算上限
  **10 000 次**。任一预算耗尽都返回结构化错误
  `422 COUNTERFACTUAL_SEARCH_BUDGET_EXCEEDED`，**不会**以成功响应输出
  命中集近似或部分方案；该错误同样绑定幂等键，重放逐字节一致。

下面就是上面那条 13 行输入的实际请求与响应（十条 `0` 被约简，三个单例
删除集按标签顺序返回，重算总和分别为 `0`、`3`、`7`）：

```bash
curl -s -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{
  "idempotency_key": "readme-mixed-padded-g1",
  "tables": [
    {"table": "orders", "primary_key": ["oid"],
     "columns": [{"name":"oid","type":"int"},{"name":"cid","type":"int"},{"name":"amount","type":"int"}],
     "rows": [
       {"oid":1,"cid":10,"amount":5},
       {"oid":2,"cid":10,"amount":2},
       {"oid":3,"cid":10,"amount":-2},
       {"oid":4,"cid":10,"amount":0},{"oid":5,"cid":10,"amount":0},
       {"oid":6,"cid":10,"amount":0},{"oid":7,"cid":10,"amount":0},
       {"oid":8,"cid":10,"amount":0},{"oid":9,"cid":10,"amount":0},
       {"oid":10,"cid":10,"amount":0},{"oid":11,"cid":10,"amount":0},
       {"oid":12,"cid":10,"amount":0},{"oid":13,"cid":10,"amount":0}
     ]}
  ],
  "plan": {
    "type": "aggregate",
    "input": {"type":"scan","table":"orders"},
    "group_by": ["cid"],
    "aggregates": [{"fn":"sum","column":"amount","as":"total"}]
  },
  "explain": {"row": {"cid":10,"total": 5}}
}'
```

实际响应（线上传输为单行紧凑 JSON；这里为可读性格式化）：

```json
{
  "status": "ok",
  "symbol_table": [
    {"symbol": "s1", "source": "orders[1]"}
  ],
  "result": {
    "columns": ["cid", "total"],
    "row_count": 1,
    "rows": [
      {
        "row": {"cid": 10, "total": 5},
        "witness_count": 1,
        "minimal_witnesses": [
          {
            "labels": ["orders[1]"],
            "symbols": ["s1"],
            "conjunction": "s1"
          }
        ],
        "provenance": {
          "dnf": "(s1)",
          "dnf_structured": [["s1"]],
          "labels_structured": [["orders[1]"]]
        }
      }
    ]
  },
  "counterfactual": {
    "target_index": 0,
    "target": {"cid": 10, "total": 5},
    "target_provenance": {
      "dnf": "(s1)",
      "dnf_structured": [["s1"]],
      "labels_structured": [["orders[1]"]]
    },
    "minimal_deletion_count": 3,
    "minimal_deletion_sets": [
      ["orders[1]"],
      ["orders[2]"],
      ["orders[3]"]
    ],
    "scenarios": [
      {
        "delete": ["orders[1]"],
        "target_present": false,
        "result": {"columns": ["cid", "total"], "row_count": 1,
                   "rows": [{"cid": 10, "total": 0}]}
      },
      {
        "delete": ["orders[2]"],
        "target_present": false,
        "result": {"columns": ["cid", "total"], "row_count": 1,
                   "rows": [{"cid": 10, "total": 3}]}
      },
      {
        "delete": ["orders[3]"],
        "target_present": false,
        "result": {"columns": ["cid", "total"], "row_count": 1,
                   "rows": [{"cid": 10, "total": 7}]}
      }
    ]
  }
}
```

解读：十条 `0` 行既不进入任何极小求和组合（符号表只有 `s1`），也被候选
约简移出重算搜索池；但 `2` 与 `-2` 是非零行，删任一行都会改变总和，
所以仍是极小删除集。若同一计划再选 `count(*)`，零行就不再是惰性行
（删它们会改变计数），约简会保留全部 13 行，十三个单例删除集全部返回。

#### 预算耗尽：结构化错误，不输出近似方案

目标组约简后仍超过 20 个有效行时（例如 20 个 `1` 加一个 `-1`，共 21 行
非零、`total=19`），服务返回 HTTP `422` 和规范化错误体，响应中没有
`counterfactual` 字段；用同一个幂等键重试返回逐字节相同的错误体：

```json
{"error":{"code":"COUNTERFACTUAL_SEARCH_BUDGET_EXCEEDED",
 "message":"The exact counterfactual search for this non-monotone (mixed positive/negative) sum could not finish within the configured budget; no approximate deletion sets are returned. Retry with a smaller target group or split the request.",
 "details":{"active_member_count":21,"dependency_gate_recomputes":0,"dropped_inert_label_count":0,
            "full_pool_size":21,"group_member_count":21,
            "pool_limit":20,"reduced_pool_size":21,
            "selective_gate_candidates":0}}}
```

其中 `selective_gate_candidates` 是选择性惰性标签门检查过的活跃标签子集数；
`dropped_inert_label_count` 是经门裁决后仍留在池外的惰性标签数；
`dependency_gate_recomputes` 为历史字段，恒为 `0`（新门纯结构性，不做
整计划重算）。上例 21 个成员全部活跃，候选池在进门前就超过上限，因此
`selective_gate_candidates` 为 `0`。

### 连接求和的反事实：自连接零值成员不能按金额丢弃

候选约简按“成员数值”判断惰性，这在扫描/一对一连接下是精确的；但同一个
来源标签可以经连接喂给多条成员。下例中表 `ledger` 自连接并过滤出三条
成员：来源行 1（金额 0）分别与行 2（金额 `5`）、行 3（金额 `-5`）连接，
行 4 与自身连接（金额 `0`）；标量 `sum(right.amount) = 0`。

* 删行 2：只剩 `(1,3)=-5` 与 `(4,4)=0`，总和变 `-5`，目标消失；
* 删行 3：只剩 `(1,2)=5` 与 `(4,4)=0`，总和变 `5`，目标消失；
* **只删行 1**：`(1,2)` 与 `(1,3)` 同时消失（净值变化 0），`(4,4)=0`
  仍然存活，总和依旧是 `0` —— 目标 **不** 消失；
* 同时删 `{行1, 行4}`：所有连接成员都被移除，标量聚合输入为空，
  返回结构化 `recompute_error: EMPTY_AGGREGATE`，目标消失。

因此极小删除集是 `{ledger[2]}`、`{ledger[3]}` 与双标签方案
`{ledger[1], ledger[4]}`。选择性惰性标签门在见证结构上判定：删除
`ledger[1]` 后 `+5`/`-5` 两条成员一起消失，仅靠存活的零值自连接成员
`(4,4)` 仍复现 `total=0`，且 `ledger[4]` 能极小地命中它，便把零值自连接
成员的标签 `ledger[4]` 精确放回候选池；精确枚举随即找到双标签方案。若只
按成员金额丢弃零值标签，就会漏报该方案；而对维持不了目标行的补位零成员
“一律放回”则会误报预算耗尽（见下例）。

```bash
curl -s -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{
  "idempotency_key": "readme-selfjoin-1",
  "tables": [
    {"table": "ledger", "primary_key": ["id"],
     "columns": [
       {"name": "id", "type": "int"},
       {"name": "k", "type": "int"},
       {"name": "amount", "type": "int"}],
     "rows": [
       {"id": 1, "k": 1, "amount": 0},
       {"id": 2, "k": 1, "amount": 5},
       {"id": 3, "k": 1, "amount": -5},
       {"id": 4, "k": 2, "amount": 0}
     ]}
  ],
  "plan": {
    "type": "aggregate",
    "input": {
      "type": "filter",
      "predicate": {"or": [
        {"and": [
          {"column": "id", "op": "=", "value": 1},
          {"column": "right.id", "op": ">=", "value": 2}]},
        {"column": "id", "op": "=", "value": 4}
      ]},
      "input": {
        "type": "join",
        "left": {"type": "scan", "table": "ledger"},
        "right": {"type": "scan", "table": "ledger"},
        "on": [{"left": "k", "right": "k"}]
      }
    },
    "aggregates": [{"fn": "sum", "column": "right.amount", "as": "total"}]
  },
  "explain": {"row": {"total": 0}}
}'
```

实际响应（线上传输为单行紧凑 JSON；这里为可读性格式化）：

```json
{
  "status": "ok",
  "symbol_table": [
    {"symbol": "s1", "source": "ledger[1]"},
    {"symbol": "s2", "source": "ledger[2]"},
    {"symbol": "s3", "source": "ledger[3]"},
    {"symbol": "s4", "source": "ledger[4]"}
  ],
  "result": {
    "columns": ["total"],
    "row_count": 1,
    "rows": [
      {
        "row": {"total": 0},
        "witness_count": 2,
        "minimal_witnesses": [
          {
            "labels": ["ledger[1]", "ledger[2]", "ledger[3]"],
            "symbols": ["s1", "s2", "s3"],
            "conjunction": "s1 ∧ s2 ∧ s3"
          },
          {
            "labels": ["ledger[4]"],
            "symbols": ["s4"],
            "conjunction": "s4"
          }
        ],
        "provenance": {
          "dnf": "(s1 ∧ s2 ∧ s3) ∨ (s4)",
          "dnf_structured": [["s1", "s2", "s3"], ["s4"]],
          "labels_structured": [
            ["ledger[1]", "ledger[2]", "ledger[3]"],
            ["ledger[4]"]
          ]
        }
      }
    ]
  },
  "counterfactual": {
    "target_index": 0,
    "target": {"total": 0},
    "target_provenance": {
      "dnf": "(s1 ∧ s2 ∧ s3) ∨ (s4)",
      "dnf_structured": [["s1", "s2", "s3"], ["s4"]],
      "labels_structured": [
        ["ledger[1]", "ledger[2]", "ledger[3]"],
        ["ledger[4]"]
      ]
    },
    "minimal_deletion_count": 3,
    "minimal_deletion_sets": [
      ["ledger[1]", "ledger[4]"],
      ["ledger[2]"],
      ["ledger[3]"]
    ],
    "scenarios": [
      {
        "delete": ["ledger[1]", "ledger[4]"],
        "target_present": false,
        "recompute_error": {
          "code": "EMPTY_AGGREGATE",
          "message": "aggregate input is empty: no groups can be formed"
        }
      },
      {
        "delete": ["ledger[2]"],
        "target_present": false,
        "result": {"columns": ["total"], "row_count": 1,
                   "rows": [{"total": -5}]}
      },
      {
        "delete": ["ledger[3]"],
        "target_present": false,
        "result": {"columns": ["total"], "row_count": 1,
                   "rows": [{"total": 5}]}
      }
    ]
  }
}
```

解读：目标 `total=0` 有两条极小见证 —— 三条连接成员中产生 0 的极小非空
组合 `{(1,2),(1,3)}`（标签 `ledger[1] ∧ ledger[2] ∧ ledger[3]`）与零值
自连接对 `(4,4)`（标签 `ledger[4]`）。单删 `ledger[1]` 会同时带走 `+5`
与 `-5` 两条成员，零值自连接对仍在，目标不消失，因此它不是删除集；双标签
方案 `{ledger[1], ledger[4]}` 清空全部成员后以 `EMPTY_AGGREGATE` 判定
目标消失。三个方案全部经删除后重算整条计划确认并按包含关系极小。

反过来，金额 `5`、`-5` 加九条 `0` 的 **一对一** 连接（每个来源标签只
喂一条成员）下，删任一活跃标签都会单独移走 `+5` 或 `-5`，依赖门通过、
九条零值对照常约简，返回的仍是四个单标签方案
（`orders[1]`、`orders[2]`、`customers[1]`、`customers[2]`），不会因
候选池膨胀误报预算耗尽。

#### 反例修复：`total=7` 下 18 条独立零值自连接不得撑爆候选池

把上例的行 4 改成金额 `7`（行 4 自连接贡献 `+7`），再加 18 条各自成组的
零值行（行 5–22，每行只与自身连接，共 18 条独立的零值自连接成员）。过滤
后参与聚合的是：行 1 连行 2（`+5`）、行 1 连行 3（`-5`）、行 4 自连接
（`+7`），以及 18 条 `0` 的自连接对；投影只保留右侧金额，18 个零值成员
**同值归并为一条成员但完整保留各自的来源见证**。目标 `total = 5-5+7 = 7`。

* 删行 2：总和变 `7-5 = 2`；删行 3：总和变 `7+5 = 12`；删行 4：`+5` 与
  `-5` 抵消、只剩零值成员，总和变 `0` —— 三个单例极小删除集；
* 只删行 1：`+5`/`-5` 一起消失（净值 0），`+7` 成员仍在，目标 **不**
  消失，所以 `{ledger[1]}` 不是删除集；18 条零自连接对同样维持不了 `7`。

旧的“全有/全无”标签依赖门在重算“仅删行 1”后发现目标仍在（`+7` 撑住），
便把 **全部 18 条零值标签** 都扩入候选池，4 个活跃标签 + 18 个零值标签
超过 20 标签上限，于是错误地返回 422。新的选择性门依据“剩余成员能否维持
完整目标行”逐标签裁决：零成员无论如何凑不出 `total=7`，18 个零标签全部
留在池外，候选池只有 `ledger[1..4]`，精确枚举返回上述三个单例方案，重算值
为 `2`、`12`、`0`。

请求体较长，完整内容见 [`examples/selfjoin_total_7.json`](examples/selfjoin_total_7.json)，
也可一键复现：

```bash
curl -s -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  --data-binary @examples/selfjoin_total_7.json
```

实际响应（线上为单行紧凑 JSON；这里格式化）：

```json
{
  "status": "ok",
  "symbol_table": [
    {"symbol": "s1", "source": "ledger[4]"}
  ],
  "result": {
    "columns": ["total"],
    "row_count": 1,
    "rows": [
      {
        "row": {"total": 7},
        "witness_count": 1,
        "minimal_witnesses": [
          {"labels": ["ledger[4]"], "symbols": ["s1"], "conjunction": "s1"}
        ],
        "provenance": {
          "dnf": "(s1)",
          "dnf_structured": [["s1"]],
          "labels_structured": [["ledger[4]"]]
        }
      }
    ]
  },
  "counterfactual": {
    "target_index": 0,
    "target": {"total": 7},
    "target_provenance": {
      "dnf": "(s1)",
      "dnf_structured": [["s1"]],
      "labels_structured": [["ledger[4]"]]
    },
    "minimal_deletion_count": 3,
    "minimal_deletion_sets": [
      ["ledger[2]"],
      ["ledger[3]"],
      ["ledger[4]"]
    ],
    "scenarios": [
      {"delete": ["ledger[2]"], "target_present": false,
       "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": 2}]}},
      {"delete": ["ledger[3]"], "target_present": false,
       "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": 12}]}},
      {"delete": ["ledger[4]"], "target_present": false,
       "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": 0}]}}
    ]
  }
}
```

注意 18 条零成员既不进入符号表（它们不参与任何极小求和组合），也不进入
重算候选池；但它们见证的“独立性”在零目标场景下至关重要。若把目标改成
`total=0`（去掉 `+7` 成员、18 条零自连接对都在），只删行 1 后正是这些
零成员把 `0` 撑住，此时 **每一个** 零标签都是真实候选（与行 1 共同构成
清空全部成员的多标签删除集），18 + 4 > 20，服务仍会诚实地返回结构化
422 而不是漏掉多标签方案或给出近似答案。

#### 反例修复：共享左侧标签的自连接求和误报 422（`total=0`，22 行）

上节的零成员各自独立（见证是单标签）；本例反过来——**所有成员共享同一个
左侧来源行**。`ledger` 22 行的连接键都是 `k=1`：行 1（金额 0）作为左侧
与行 2–22 配对（过滤只保留 `id = 1 AND right.id >= 2`）；行 2–11 金额
`+9`（十行），行 12–20 金额 `-10`（九行），行 21、22 金额 `0`，投影右侧
行号与金额后求和，目标 `total = 10×9 − 9×10 = 0`。

* 19 条非零成员的见证都含 `ledger[1]`，汇成 **20 个活跃标签**
  `ledger[1..20]`；两条零值成员 `(1,21)`、`(1,22)` 的见证
  `{ledger[1], ledger[21]}`、`{ledger[1], ledger[22]}` 也依赖行 1。
* 删行 1：全部 21 条成员的见证都被命中，聚合输入为空
  （`EMPTY_AGGREGATE`），目标消失 —— `{ledger[1]}` 是极小删除集；
* 删行 j（2–20）：只移除成员 `(1,j)`，总和变 `-9` 或 `+10`，目标消失
  —— 19 个单标签极小删除集；
* 删行 21 或 22：只删一条零值成员，总和仍是 `0`，目标存活；任何含
  21/22 的更大删除集要么杀不死目标、要么真包含某个单标签解（不极小）。
  所以 **21、22 不属于任何极小删除集**，正确答案就是 20 个单标签集合。

旧门控在这里误报 422：活跃见证边的极小覆盖是 `{ledger[1]}` 与
`{ledger[2..20]}`，对 `{ledger[1]}` 做超集枚举要展开 2^19 个掩码——但
任何含 `ledger[1]` 的掩码连两条零值成员也一起杀掉（它们的见证含行 1），
惰性存活集为空、永远复现不了 `total=0`，这 50 余万次检查全是无用功；
掩码额度（20 万）耗尽后门控保守退回 22 标签全池，超过 20 标签上限而
错误返回 422。新的**存活可复现剪枝**在检查 `{ledger[1]}` 时发现零值
成员无一存活、目标已不可能由惰性成员复现，整棵子树立即剪除；再检查
`{ledger[2..20]}`（零值成员存活且合计 0，但删掉其中任一活跃标签都会
复活一条非零成员、使 `A` 可省去，条件④不成立），全程只检查 **3 个
掩码**。惰性标签 21、22 被证明无关而留在池外，候选池 = 20 个活跃标签，
精确枚举在 21 次重算内返回 20 个单标签方案（预算 20 万掩码 / 1 万次
重算均未触及）。

请求体较长，完整内容见
[`examples/selfjoin_total_0_shared_left.json`](examples/selfjoin_total_0_shared_left.json)，
一键复现：

```bash
curl -s -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  --data-binary @examples/selfjoin_total_0_shared_left.json
```

实际响应（线上为单行紧凑 JSON；这里格式化）：

```json
{
  "status": "ok",
  "symbol_table": [
    {"source": "ledger[1]", "symbol": "s1"},
    {"source": "ledger[10]", "symbol": "s10"},
    {"source": "ledger[11]", "symbol": "s11"},
    {"source": "ledger[12]", "symbol": "s12"},
    {"source": "ledger[13]", "symbol": "s13"},
    {"source": "ledger[14]", "symbol": "s14"},
    {"source": "ledger[15]", "symbol": "s15"},
    {"source": "ledger[16]", "symbol": "s16"},
    {"source": "ledger[17]", "symbol": "s17"},
    {"source": "ledger[18]", "symbol": "s18"},
    {"source": "ledger[19]", "symbol": "s19"},
    {"source": "ledger[2]", "symbol": "s2"},
    {"source": "ledger[20]", "symbol": "s20"},
    {"source": "ledger[21]", "symbol": "s21"},
    {"source": "ledger[22]", "symbol": "s22"},
    {"source": "ledger[3]", "symbol": "s3"},
    {"source": "ledger[4]", "symbol": "s4"},
    {"source": "ledger[5]", "symbol": "s5"},
    {"source": "ledger[6]", "symbol": "s6"},
    {"source": "ledger[7]", "symbol": "s7"},
    {"source": "ledger[8]", "symbol": "s8"},
    {"source": "ledger[9]", "symbol": "s9"}
  ],
  "result": {
    "columns": ["total"],
    "row_count": 1,
    "rows": [
      {
        "minimal_witnesses": [
          {
            "conjunction": "s1 ∧ s2 ∧ s3 ∧ s4 ∧ s5 ∧ s6 ∧ s7 ∧ s8 ∧ s9 ∧ s10 ∧ s11 ∧ s12 ∧ s13 ∧ s14 ∧ s15 ∧ s16 ∧ s17 ∧ s18 ∧ s19 ∧ s20",
            "labels": [
              "ledger[1]",
              "ledger[2]",
              "ledger[3]",
              "ledger[4]",
              "ledger[5]",
              "ledger[6]",
              "ledger[7]",
              "ledger[8]",
              "ledger[9]",
              "ledger[10]",
              "ledger[11]",
              "ledger[12]",
              "ledger[13]",
              "ledger[14]",
              "ledger[15]",
              "ledger[16]",
              "ledger[17]",
              "ledger[18]",
              "ledger[19]",
              "ledger[20]"
            ],
            "symbols": [
              "s1",
              "s2",
              "s3",
              "s4",
              "s5",
              "s6",
              "s7",
              "s8",
              "s9",
              "s10",
              "s11",
              "s12",
              "s13",
              "s14",
              "s15",
              "s16",
              "s17",
              "s18",
              "s19",
              "s20"
            ]
          },
          {
            "conjunction": "s1 ∧ s21",
            "labels": ["ledger[1]", "ledger[21]"],
            "symbols": ["s1", "s21"]
          },
          {
            "conjunction": "s1 ∧ s22",
            "labels": ["ledger[1]", "ledger[22]"],
            "symbols": ["s1", "s22"]
          }
        ],
        "provenance": {
          "dnf": "(s1 ∧ s2 ∧ s3 ∧ s4 ∧ s5 ∧ s6 ∧ s7 ∧ s8 ∧ s9 ∧ s10 ∧ s11 ∧ s12 ∧ s13 ∧ s14 ∧ s15 ∧ s16 ∧ s17 ∧ s18 ∧ s19 ∧ s20) ∨ (s1 ∧ s21) ∨ (s1 ∧ s22)",
          "dnf_structured": [
            [
              "s1",
              "s2",
              "s3",
              "s4",
              "s5",
              "s6",
              "s7",
              "s8",
              "s9",
              "s10",
              "s11",
              "s12",
              "s13",
              "s14",
              "s15",
              "s16",
              "s17",
              "s18",
              "s19",
              "s20"
            ],
            ["s1", "s21"],
            ["s1", "s22"]
          ],
          "labels_structured": [
            [
              "ledger[1]",
              "ledger[2]",
              "ledger[3]",
              "ledger[4]",
              "ledger[5]",
              "ledger[6]",
              "ledger[7]",
              "ledger[8]",
              "ledger[9]",
              "ledger[10]",
              "ledger[11]",
              "ledger[12]",
              "ledger[13]",
              "ledger[14]",
              "ledger[15]",
              "ledger[16]",
              "ledger[17]",
              "ledger[18]",
              "ledger[19]",
              "ledger[20]"
            ],
            ["ledger[1]", "ledger[21]"],
            ["ledger[1]", "ledger[22]"]
          ]
        },
        "row": {"total": 0},
        "witness_count": 3
      }
    ]
  },
  "counterfactual": {
    "minimal_deletion_count": 20,
    "minimal_deletion_sets": [
      ["ledger[1]"],
      ["ledger[2]"],
      ["ledger[3]"],
      ["ledger[4]"],
      ["ledger[5]"],
      ["ledger[6]"],
      ["ledger[7]"],
      ["ledger[8]"],
      ["ledger[9]"],
      ["ledger[10]"],
      ["ledger[11]"],
      ["ledger[12]"],
      ["ledger[13]"],
      ["ledger[14]"],
      ["ledger[15]"],
      ["ledger[16]"],
      ["ledger[17]"],
      ["ledger[18]"],
      ["ledger[19]"],
      ["ledger[20]"]
    ],
    "scenarios": [
      {
        "delete": ["ledger[1]"],
        "recompute_error": {
          "code": "EMPTY_AGGREGATE",
          "message": "aggregate input is empty: no groups can be formed"
        },
        "target_present": false
      },
      {
        "delete": ["ledger[2]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": -9}]},
        "target_present": false
      },
      {
        "delete": ["ledger[3]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": -9}]},
        "target_present": false
      },
      {
        "delete": ["ledger[4]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": -9}]},
        "target_present": false
      },
      {
        "delete": ["ledger[5]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": -9}]},
        "target_present": false
      },
      {
        "delete": ["ledger[6]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": -9}]},
        "target_present": false
      },
      {
        "delete": ["ledger[7]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": -9}]},
        "target_present": false
      },
      {
        "delete": ["ledger[8]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": -9}]},
        "target_present": false
      },
      {
        "delete": ["ledger[9]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": -9}]},
        "target_present": false
      },
      {
        "delete": ["ledger[10]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": -9}]},
        "target_present": false
      },
      {
        "delete": ["ledger[11]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": -9}]},
        "target_present": false
      },
      {
        "delete": ["ledger[12]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": 10}]},
        "target_present": false
      },
      {
        "delete": ["ledger[13]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": 10}]},
        "target_present": false
      },
      {
        "delete": ["ledger[14]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": 10}]},
        "target_present": false
      },
      {
        "delete": ["ledger[15]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": 10}]},
        "target_present": false
      },
      {
        "delete": ["ledger[16]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": 10}]},
        "target_present": false
      },
      {
        "delete": ["ledger[17]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": 10}]},
        "target_present": false
      },
      {
        "delete": ["ledger[18]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": 10}]},
        "target_present": false
      },
      {
        "delete": ["ledger[19]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": 10}]},
        "target_present": false
      },
      {
        "delete": ["ledger[20]"],
        "result": {"columns": ["total"], "row_count": 1, "rows": [{"total": 10}]},
        "target_present": false
      }
    ],
    "target": {"total": 0},
    "target_index": 0,
    "target_provenance": {
      "dnf": "(s1 ∧ s2 ∧ s3 ∧ s4 ∧ s5 ∧ s6 ∧ s7 ∧ s8 ∧ s9 ∧ s10 ∧ s11 ∧ s12 ∧ s13 ∧ s14 ∧ s15 ∧ s16 ∧ s17 ∧ s18 ∧ s19 ∧ s20) ∨ (s1 ∧ s21) ∨ (s1 ∧ s22)",
      "dnf_structured": [
        [
          "s1",
          "s2",
          "s3",
          "s4",
          "s5",
          "s6",
          "s7",
          "s8",
          "s9",
          "s10",
          "s11",
          "s12",
          "s13",
          "s14",
          "s15",
          "s16",
          "s17",
          "s18",
          "s19",
          "s20"
        ],
        ["s1", "s21"],
        ["s1", "s22"]
      ],
      "labels_structured": [
        [
          "ledger[1]",
          "ledger[2]",
          "ledger[3]",
          "ledger[4]",
          "ledger[5]",
          "ledger[6]",
          "ledger[7]",
          "ledger[8]",
          "ledger[9]",
          "ledger[10]",
          "ledger[11]",
          "ledger[12]",
          "ledger[13]",
          "ledger[14]",
          "ledger[15]",
          "ledger[16]",
          "ledger[17]",
          "ledger[18]",
          "ledger[19]",
          "ledger[20]"
        ],
        ["ledger[1]", "ledger[21]"],
        ["ledger[1]", "ledger[22]"]
      ]
    }
  }
}
```

解读：`total=0` 有三条极小见证 —— 19 条非零成员全体构成的组合（标签
`ledger[1..20]`，十个 `+9` 与九个 `-10` 缺一不可）与两条零值成员各自的
单成员组合。最小删除集是按标签序返回的 20 个单标签集合
`["ledger[1]"]` … `["ledger[20]"]`：删行 1 清空整组，场景保留结构化的
`recompute_error: EMPTY_AGGREGATE`；删行 2–11 总和变 `-9`，删行 12–20
总和变 `+10`，20 个场景重算后目标均消失。零值标签 `ledger[21]`、
`ledger[22]` 被门控证明与任何极小删除集无关，不进候选池；但若它们
**独立**存活（不依赖行 1），删行 1 后正是它们把 `0` 撑住，门控照样会
把它们精确放回（见前面 `ledger[4]` 的双标签方案与 18 条独立零对的诚实
422）——剪枝只跳过“无论如何都复现不了目标”的子树，不改变裁决结果。

### 幂等重放逐字节一致

```bash
curl -s ... -d @req.json > a.json
curl -s ... -d @req.json > b.json
cmp a.json b.json && echo "byte-identical"   # byte-identical
# 第二次响应头: X-Idempotent-Replay: true
```

---

## 4. 结构化错误

错误统一为 `4xx` + 规范化 JSON：

```json
{"error": {"code": "DUPLICATE_PRIMARY_KEY",
           "message": "Table 't': duplicate primary key [1]",
           "details": {"table": "t", "primary_key": [1]}}}
```

| code | 触发条件 |
| --- | --- |
| `MALFORMED_REQUEST` | JSON 非法、请求不符合 schema、行含未声明列、表重名等 |
| `MALFORMED_PLAN` | 节点类型未知、谓词结构非法、投影/聚合别名冲突等 |
| `UNKNOWN_TABLE` | 计划引用未提交的表 |
| `ILLEGAL_REFERENCE` | 计划/谓词/分组/聚合引用不存在的字段 |
| `DUPLICATE_PRIMARY_KEY` | 同一表快照内主键重复 |
| `NULL_PRIMARY_KEY` | 主键的任一分量为 null |
| `EMPTY_AGGREGATE` | 聚合输入为空（无法形成任何组；含标量聚合） |
| `TYPE_ERROR` | 声明类型不符、跨类型比较/连接、对非数值求 `sum`、对布尔值做有序比较 |
| `TARGET_NOT_FOUND` | `explain` 的 index 越界或 row 不匹配任何输出 |
| `COUNTERFACTUAL_SEARCH_BUDGET_EXCEEDED` | 混合正负 `sum` 的精确反事实搜索耗尽预算（约简 + 选择性惰性标签门后候选池超过 20 行，或整计划重算超过 10 000 次；HTTP 422，不返回近似方案） |
| `IDEMPOTENCY_KEY_CONFLICT` | 幂等键已绑定到另一个规范化请求（HTTP 409） |

---

## 5. 溯源语义说明

* 来源行标签为 `表名[主键值,...]`，复合主键按顺序拼接。符号表把标签稳定映射为
  `s1, s2, ...`（按表名、主键全序排序）。
* 见证集经过包含极小化：投影去列、自连接等造成的冗余派生会被剔除；
  同一标签集合只保留一份，输出按标签全序稳定排序。
* 选择、等值连接、投影对源行单调；聚合输出的见证把“组存在性”（至少一名
  成员存活）与每个聚合表达式的数值组件合取：`count` 覆盖所有参与计数的行，
  `sum`/`min`/`max` 枚举产生目标值的极小非空行组合。因此“令输出消失的最小
  删除集”等于该输出全部极小见证的 **极小命中集**（hypergraph transversal）：
  命中所有见证后，组键或某个聚合值必然改变。
* 例外是 **含正负混合值的 `sum`**（非单调求和）：删除不属于任何极小组合的
  行也可能改变总和，且删除更多行可能让目标值重新出现，命中集刻画不再成立。
  此时服务先对目标组做 **候选约简**（`sum` 的零值/NULL 行、`min/max` 的非
  极值行、`count(col)` 的 NULL 行先移出候选池），再过一道 **选择性惰性
  标签门**：它完全基于见证结构与聚合值模拟（不做整计划重算），逐个判定
  “只支持惰性成员”的来源标签能否出现在某个极小删除集中——删除某活跃标签
  子集后全部活跃成员消失、仅靠存活的惰性成员仍能完整复现目标行、且该惰性
  标签能极小地命中存活成员时才放回候选池。连接（尤其自连接）里同一来源标签
  可喂给多条成员（如行 1 同时产出 `+5` 与 `-5` 两条连接成员），零值自连接
  成员可能正是零目标存活的原因，其标签会被精确放回；而维持不了目标行的
  独立补位零成员（如目标 `total=7` 时的 18 条零自连接对）不会被“一刀切”
  扩入候选池。门的掩码枚举设 200 000 个子问题上限，并带两道不改变结果的
  见证依赖剪枝：惰性成员的任意非空子集都复现不了目标行时直接证明惰性
  标签无关（0 次掩码检查）；掩码增长时惰性存活集只缩不增，存活成员一旦
  复现不了目标行，整棵超集子树被剪除（共享左侧标签的自连接因此不会
  耗尽掩码额度）。随后对候选删除集按大小递增枚举，**按删除后重算的完整
  目标行**
  判定，返回全部按包含关系极小的方案；枚举中只有已找到解的超集才被跳过，
  未命中的删除集不剪枝其超集。候选池（约简 + 选择性门后）超过 20 行、或
  整计划重算超过 10 000 次时返回结构化错误
  `COUNTERFACTUAL_SEARCH_BUDGET_EXCEEDED`（HTTP 422），绝不回退为命中集
  近似。无论走哪条路径，每个删除场景都会 **重算整条计划** 并显式给出
  `target_present`，以重算结果为准；删除后聚合输入为空时，场景返回结构化
  的 `recompute_error`（如 `EMPTY_AGGREGATE`）。
