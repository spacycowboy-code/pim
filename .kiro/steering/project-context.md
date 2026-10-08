---
inclusion: always
---

# pim-analysis 项目档案

从 MI（Manufacturing Intelligence，制造智能，也叫 PLDA）拉生产测试数据，
分析 PIM（Passive Intermodulation，无源互调）良率与失效。

## 环境与命令

- 操作系统 Windows，命令行 PowerShell（连续命令用 `;` 不是 `&&`）
- Python 用 uv 临时环境，不建虚拟环境目录：
  ```powershell
  & "C:\Users\enanshn\.local\bin\uv.exe" run --quiet --with <包> python <脚本>
  ```
- 测试：`uv run --quiet --with pytest pytest tests/ -q`
- Streamlit 界面：`uv run --with streamlit --with requests-negotiate-sspi --with pandas --with matplotlib streamlit run app.py`
- matplotlib 必须 `Agg` 后端；中文字体 `Microsoft YaHei`

## MI 接口（全部实测验证过，不要重新探测）

```
主机  https://apps.mesmi.internal.ericsson.com
认证  集成 Windows 认证（响应头 WWW-Authenticate: Negotiate），不需要申请任何权限
      Python:     requests + requests_negotiate_sspi.HttpNegotiateAuth
      PowerShell: Invoke-WebRequest -UseDefaultCredentials
取数  POST /MIReports/api/ReportGetData1/<报表名>Report
载荷  {"reportFilter": {<27个字段，缺的必须显式填 null>}, "pageSetting": null}
```

### ★ MI 有两种返回结构，解析前必须先判断

这是踩过的坑：用 `data.data` 去解析别的报表会误报"0 行"，其实有数据。

| 结构 | 路径 | 列名来源 | 哪些报表 |
|---|---|---|---|
| A 纯数组 | `data.data` → 二维数组 | **没有**，靠外部常量 | `TestPointSummary` |
| B 带列名 | `data.resultSet[i]` 或 `data.tableResultSet`，各含 `columns[].name` + `rows` | 响应自带 | 其余报表 |

结构 B 的 `resultSet` 可能是**多张表**（例如测试历史 + 维修历史各一张）。

### ★ 带测试时间戳的报表（解锁"任意日期查询"的关键）

`TestPointSummary` 是聚合结果，23 列里**没有序列号也没有时间戳**，所以拿到后
无法按别的日期重新切分——换日期必须重新请求 MI。下面三个报表给的是**明细**，
含真实时间戳，一次拉全后可在本地/服务器端任意按时间筛选。

**共同限制**：筛选字段只有 `SerialNumber` + `file_SerialNumber`，
**不能按产品或日期筛**，返回该序列号的全部历史。所以必须逐台拉。

| 报表 | 表 | 关键列 |
|---|---|---|
| `FunctionalTestAndRepairHistory` | `resultSet[0]` 17 列 | `SerialNumber` `ProductNumber` `TestCriteria` **`TestStartTime`** **`TestEndTime`** `TestCount` `TestPassed`(Y/N) `TestCategoryID` `TestStationID` |
| 同上 | `resultSet[1]` 8 列 | `TestStartTime` `RepairStartTime` `RepairEndTime` `FaultName` `FaultAreaName` `RepairActionName` |
| `ChildSerialNumberTestHistory` | `resultSet[0]` 34 列 | `Serial Number` `Mfg Date` **`Test Start Date`** `Test Status` `Failed Test Point Code` `Failed Test Point Name` `Measured Value` `Fault Code` `Repair Action Name` + 14 个 `Parent *` 列 |
| `SerialNumberLog` | `tableResultSet` 15 列 | `Event` **`Event Start Time`** `Event End Time` `Lead-time` `Location` `Test Result` `Test Count` `Test Category` `Test Station` |

实测规模（8 台并行 8 线程）：

```
FunctionalTestAndRepairHistory   4.7 秒   95 行   76 KB
ChildSerialNumberTestHistory     5.0 秒   45 行   95 KB
列结构在所有台之间完全一致
```

80 台约 50 秒、不到 1 MB。

**⚠️ 只有测试历史能算良率，`TestPointSummary` 不能：**
- 良率 = **场次级 FPY** = 第一次测试通过的场次 ÷ 首次测试场次总数（量级百）
  —— 来自测试历史（`FunctionalTestAndRepairHistory`），是唯一口径
- `TestPointSummary` 只能算**测试点次数级失败率** = 失败的测试点执行次数 ÷
  总测试点执行次数（量级十万）—— 这**不是良率**，只用来定位哪个维度失败多

两个都是对的数，但只有前者能叫良率。后者当良率用会严重高估（总是 99.x%）。

### 探测报表清单和字段名的正确方法

```
报表清单  GET /MIReports          -> 解析 <a href="..//MIReports/<报表名>?">
字段名    GET /MIReports/<报表名>  -> 解析 filter-id 属性
字段类型  同上                     -> 解析 filter-type 属性
```
**不要盲猜报表名**，实测 24 个常见猜测全部 404。导航页有 130+ 个真实报表。

存储过程缺参数时错误信息会直接点名，照着补即可：
`Procedure or function 'pGetPlotValues' expects parameter '@NumberOfSTD'`


字段名的权威来源：报表页 HTML 的 `filter-id` 属性。
字段类型的权威来源：同一标签的 `filter-type` 属性。
**不要去猜 `ReportGetFilter` 的载荷**——七种格式全部 500/400，此路不通。

### TestPointSummary 的 27 个字段

`Area` `ControlCategory` `Factory` `file_SerialNumber` `FromDate` `HighLimit`
`IncludeOutliers` `Line` `LowLimit` `MaxValue` `MedianBase` `MinValue`
`ProductFamily` `ProductGroup` `ProductNumber` `ProductRevisionState` `ProductType`
`SerialNumber` `TestCategory` `TestCount` `TestCriteria` `TestPoint` `TestPointGroup`
`TestPointResult` `TestProductionComment` `TestProgram` `ToDate`

必填：`ProductNumber` + `FromDate` + `ToDate`，另需
`TestCount="[All]"` `TestPointResult="[All]"` `IncludeOutliers="No"` `MedianBase="[All]"`。

### 哪些筛选真的生效（实测行数对比，别信"没报错"）

| 字段 | filter-type | 生效 | 备注 |
|---|---|---|---|
| `SerialNumber` | txt-auto-multi | ✅ | **逗号多值**，80 个仅 28 秒 |
| `ProductNumber` | txt | ✅ | 填 `KRD 901 330/11`，**去掉尾部 R-state** |
| `FromDate`/`ToDate` | date | ✅ | 格式 `2026-09-14 00:00` |
| `TestCategory` | txt-auto-multi | ✅ | 逗号多值。已见值 26/212/38/28/32/29 |
| `TestPoint` | txt-auto-multi | ✅ | 逗号多值，**必须精确编码，不支持通配符** |
| `TestCount` | list | ✅ | `[All]`/`First`/`Last`/`2`~`10`，**First = FPY** |
| `TestPointResult` | list | ⚠️ | 只有 `Failed` 生效；`Passed` 填了等于没填 |
| `IncludeOutliers` | list | ✅ | `Yes`/`No` |
| `MedianBase` | list | ✅ | `[All]`/`Last30` |
| `TestPointGroup` | txt-auto-single | ❌ | 填 Name 和 Code 都不生效，放弃 |
| `TestProgram` | txt-auto-multi | ❌ | 填 `PIM_PROD` 不生效 |
| `file_SerialNumber` | file | — | 不需要，用 SerialNumber 逗号多值即可 |

**多值分隔符只认逗号**。分号/换行/空格会被静默忽略（返回全量），竖线只认第一个。
判断筛选是否生效必须**对比行数**，错的写法不报错。

### ⚠️ 陷阱：`TestPointResult="Failed"` 会改变统计分母

它不是"只返回失败的行"，而是**只统计失败的那几次测试**。实测同一测试点：

| 测试点 | `[All]`: #Tested / %Fail | `Failed`: #Tested / %Fail |
|---|---|---|
| `5.2.100.18.0` | 6 / **16.67** | 1 / **100** |
| `5.2.100.25.0` | 6 / 33.33 | 2 / 100 |
| `5.2.102.20.0` | 6 / 50 | 3 / 100 |

（单台 EA8E538919 的 38 个失败测试点里 36 个数值不同）

所以：
- **算失败率必须用 `TestPointResult="[All]"`**，绝不能用 `Failed`（分母会变）
- `Failed` 模式的 `#Tested` 语义是**失败次数**，可用来快速定位"哪些点失败过、失败几次"
- 两种模式的 `%Fail` 不可比，别混着用

### ⚠️⚠️ 更大的陷阱：批量查询（多台）的 `%Fail` 列恒为 0，完全不可用

同一个测试点 `5.2.100.18.0`：

| 查询模式 | #Tested | %Fail |
|---|---|---|
| 单台 `EA8E538919` | 6 | **16.67** |
| 3 台逗号批量 | 13 | **0** |
| 80 台逗号批量 | 302 | **0** |
| 不填 SN（全产品） | 863 | **0** |

3 台时 13 次测试有 1 次失败（7.7%），不可能舍入成 0，所以这不是精度问题——
**只要序列号多于一个，`%Fail` 列就失效**。
直接拿批量数据算失败率会得到 0%，完全错误。

**正确算法：两次批量请求相除**

```python
all_rows    = c.test_points(..., only_failed=False)  # #Tested = 总测试次数
failed_rows = c.test_points(..., only_failed=True)   # #Tested = 失败次数
失败率 = Σfailed#Tested / Σall#Tested x 100      # 注意：这不是良率
```

实测交叉验证（3 台，对照逐台查询的真值）：
- 失败次数：53 个有失败的测试点**全部精确一致**
- 总测试次数：77299（真值）vs 78275（批量），差 1.26%，
  因为批量返回的测试点集合是并集、略多。对良率影响仅 0.001 个百分点
- 耗时：两次请求约 56 秒，而逐台查 79 台要 5~8 分钟

只有**单台查询**的 `%Fail` 可以直接用。

### 两种查询模式（本质区别，决定架构）

| 模式 | 怎么填 | 得到什么 | 实测耗时 |
|---|---|---|---|
| 聚合 | 不填 SN，或多个 SN 逗号传 | 按测试点聚合的统计，`# Tested` 是次数，**不知道是哪台机器** | 80 台 28 秒 |
| 明细 | 填单个 SN | 那一台的全部测试点 | 单台 28~70 秒 |

要良率用聚合，要"哪台失败"必须逐台查（可并行）。
加 `TestPointResult="Failed"` 数据量降约 270 倍（3.4 MB → 12.5 KB）。

### 返回结构

- `TestPointSummary` → `d['data']['columns']`（23 列定义）+ `d['data']['data']`（行数组）
- `SerialNumberLog` → `d['data']['product']` + `d['data']['tableResultSet']['columns']` + `['data']`（是 `data` 不是 `rows`）
- `SerialNumberStructure` → `d['data']['resultSet'][0]['columns']` + `['data']`
- `ReportExportCSVData/TestPointSummaryReport` 返回 404，CSV 导出对该报表不可用
- `ReportGetData2` 是图表数据接口，本项目用不到

23 列顺序：`Test Point Code | Test Point Name | Test Point Group Code | Test Point Group Name |
Test Criteria | Test Area | Test Line | Control Category | Test Category | Unit | # Tested |
% Fail | Low Limit | High Limit | Lowest Value | Highest Value | Avg Value | Std Dev |
Cpk | CpL | CpU | Observed DPMO | Expected DPMO`
（`# Tested` 是索引 10，`% Fail` 是索引 11）

### SerialNumberStructure（下层序列号）

字段只有 2 个：`SerialNumber`、`file_SerialNumber`。
返回 6 列：`Factory Code | Product Number | Serial Number | Modified Time |
Newly Added Serial Number | Removed Serial Number`

装卸记录格式：`产品号 R-state : 序列号`，多个用逗号分隔。例：
`KRE 105 518 R1C : DS4W0270J6,SDF 111 860/1 R1B : EX20138884`

**只支持父件查子件，用子件 SN 反查返回空。** 反查必须先批量拉父件结构建本地索引。

## 产品与数据结构（已确认）

- 整机 `KRD 901 330/11 R1A`（SN 形如 `EA8E5xxxxx`）
- 天线 `KRE 105 518 R1C`（SN 形如 `DS4W0270xx`）
- **FU（Filter Unit 滤波单元）`KRF 901 809 R1A`（SN 形如 `F98BX00xxx`）**
- 一台整机 **16 个 FU 管 32 个分支**，即一个 FU 管两个分支
- 其他子件：`ROX 101 42/11`、`SDD 513 1733/2`、`SXK 125 7948/2`、`SDF 111 860/1`

**测试点编码** `5.2.<item>.<branch>.<freq_idx>`，两类测量项频率编法不同：

| 类型 | 频率在哪 | 数值语义 | 典型值 |
|---|---|---|---|
| `noise_power_level_*` / `air_leakage_*` | 编码第 5 段 (0–5) | 功率 dBm | −105 ~ −104 |
| `*knock_response` / `knock_impulse` | **写在测量项名称里**，第 5 段恒为 0 | 通过率 | 0 ~ 1 |

**两条不变量，破了就出无意义结果：**
1. 解析频率**优先从名称取**（`re.compile(r"([\d.]+)\s*MH", re.I)`），取不到才回退编码
2. 两类数值**绝不混在一起平均**（曾算出 −51.89 dBm 这种无意义值）

`mi_parse.py` 用 `value_kind` 区分：`avg <= -10` → `power`；`0.0 <= avg <= 1.0` → `passrate`。

### ⚠️ `Unit` 列不可信，判断数据类型只能看数值范围

实测单台 `EA8E538919` 的 PIM 测试点，`Unit` 列和实际数值**是反的**：

| Unit 列写的 | 实际类型 | 条数 | 数值范围 |
|---|---|---|---|
| （空） | 功率 | 768 | −105.30 ~ −104.08 |
| **`Dbm`** | **通过率** | **576** | **0.50 ~ 1.00** |
| （空） | 通过率 | 192 | 0.50 ~ 1.00 |
| `Boolean` | 通过率 | 1 | 1.00 |

真正的功率数据 `Unit` 是空的，通过率反而有 576 条标着 `Dbm`。
限值也印证：`5.2.100.0.0` 的 `Low/High Limit` 都是 `1.0`、实测 `1.0`，
但 `Unit` 写着 `Dbm` —— 显然是通过率而不是功率门限。

所以 `classify_value()` 必须靠**数值范围**判断，绝不能读 `Unit` 列。

### 测试点名称里的信息（比编码更全）

```
RX PIM Power re_radiated_and_inline_continuous_knock_response:2*C2-1*C4 1776 MHz rfPort 18
└─ 测试点组 ─┘└──────── 测量项 ────────┘└ 互调组合 ┘└ 频率 ┘└ rfPort ┘
```

**`rfPort 18` 就是分支号 18**，和编码 `5.2.100.18.0` 的第 4 段一致，可互相校验。

### `Test Criteria` 列 = 测试程序标识，可追溯数据来源

```
CXC1744995/1_F212_BC85D4621D-R1A34-2026-08-17
└── 程序号 ──┘└ 类别 ┘└  ID  ┘└ 版本┘└ 日期 ┘
```

实测单台 `EA8E538919` 涉及 5 个测试程序：
`CXC1745245/1_F26`(9092行) / `CXC1744995/1_F212`(1542) / `_P38`(1537) / `_F28`(233)
/ `21/19059-KRD901330/11-A1-2026-06-01`(78)

`Test Area` 恒为 `RADIO`；`Test Line`、`Control Category` 实测全是 `[NULL]`/`[BLANK]`，没有有效值。

频率索引映射：`0→1896, 1→1856, 2→1736, 3→1776, 4→2502.5, 5→2562.5` MHz
出图频率顺序：`[1736.0, 1776.0, 1856.0, 1896.0, 2502.5, 2562.5]`（与 RTD 图一致）
分支 0–31 对应表格 `BR0`–`BR31`。

## ★★ 良率口径（唯一，用户定的，不要再加第二种）

**良率 = 场次级 FPY = 第一次测试通过的场次 ÷ 首次测试场次总数**

```
实现     test_history.run_yield(runs, first_only=True)
接口     GET /api/history/yield      口径固定，不接受 first_only 参数
趋势     GET /api/history/trend      同口径（trend(..., first_only=True)）
界面     第 2 块「FPY 良率与趋势」，放在最前面
```

- 场次级：一次整机测试算一个场次，任一测试点失败整场算失败
- 只算 FPY：每台只取 `TestCount=1` 的那一场，所以分母天然就是台数
- **不提供"全部测试次数/最终良率"开关**，避免出现第二个良率

### 这些都不是良率，字段一律叫 fail_rate_pct

| 来源 | 分母 | 为什么不是良率 |
|---|---|---|
| `yield_calc.yield_summary` / `yield_from_pair` | 测试点执行次数（十万量级） | 总是 99.x%，当良率用会严重高估 |
| `yield_calc.yield_by*`（分支/频率/测量项） | 该维度的执行次数 | 分支不是交付单位，32 个分支坏 1 个整机就不良 |

硬约束（有测试锁住，别破）：
- `yield_calc` 的任何返回里**不许出现 `yield_pct` / `point_yield_pct`**
- `/api/yield` 返回 `level: "testpoint"`，`/api/history/*` 返回 `level: "run"`
- `yield_pct` 这个名字只属于场次级 FPY
- 已删除 `yield_calc.unit_yield()`（整机台数级良率），因为它是第二种口径

出图：`plots.fig_fail_rate_bar()`（原 `fig_yield_bar`），柱子越高越差。

## ★★ 供应商（天线 / 滤波器）—— 用 UnitProductStructure 的 Mfg.Unit，权威

**之前"MI 拿不到机械件供应商"的结论是错的**，错在只查了元件追溯
（`ComponentTraceability*`）那一族报表。正确的报表是 **`UnitProductStructure`**，
它的 `Mfg. Unit` 列直接给供应商代码+公司名。

```
POST /MIReports/api/ReportGetData1/UnitProductStructureReport
20 个筛选字段：FromDate ToDate Factory ProductGroup ProductFamily ProductType
  ProductNumber ProductBulk file_ProductBulk ProductRevisionState
  ControlCategory TestCategory SerialNumber file_SerialNumber
  ChildProductNumber file_ChildProductNumber ChildProductRevisionState
  ChildSerialNumber file_ChildSerialNumber TimeFrequency
```

⚠️ **`TimeFrequency` 必须显式填**，否则 HTTP 500 `Index was out of range`
（不点名缺哪个参数，踩过）。枚举值 `Day` / `Week` / `Month` / `None`，
要明细就填 `"None"`。另外 ProductGroup/Family/Type/Number/Bulk 构成一个
`filter-required-group="product"` 必填组，至少填一个（填 ProductNumber 即可）。

### ★ 第三种返回结构（档案原来只记了两种）

```
data.m_Item1.resultSet[]     每个元素含 columns[].name + data
data.m_Item2                 实测恒为 null
```

`m_Item*` 是 .NET Tuple 序列化的产物。用 `data.data` 或 `data.resultSet`
去解析这个报表会误判成"0 行"。两张表：

| resultSetName | 内容 | 列 |
|---|---|---|
| `Data` | 按 (子件产品号, Mfg.Unit) 聚合 | `Factory` `Product Number` `Product Revision State` `Part Product Number` `Part Product Revision State` **`Mfg. Unit`** `First Tests` `Passed Tests` `Yield %` `Yield Without Repair %` `SortingId` `ProductNumberData` |
| `Data1` | 明细，一行一个父子装配关系 | 同上前 6 列 + `First Tests` `Passed Tests` `Test Result` `Test Passed W/O Repair` + **`ParentSN => ChildSN`** |

`ParentSN => ChildSN` 是一个字符串，子件多个时用逗号分隔：
`EA8E489776 => DK4G5001Y4, DK4G5001Z9, ...`

★ 聚合表第 12 列 `ProductNumberData` 里嵌着一段 JSON 字符串，是明细的子集
但**每行只保留 16 个子件**（被截断）。要完整明细一律用 `Data1` 表，别解析它。

### 实测供应商清单（2026-02 ~ 2026-09，628 台整机，9041 行明细）

| 子件 | 产品号 | 供应商（Mfg. Unit） | 序列号前缀 | 整机台数 |
|---|---|---|---|---|
| 滤波器 | `KRF 901 809` | `F98-Grentech NanNing` | `F98BX` | 337 |
| 滤波器 | `KRF 901 809` | `DK4-Chunxing Suzhou` | `DK4G5` | 239 |
| 滤波器 | `KRF 901 809` | `EC1-Chunxing Shenzhen` | `EC1G5` | 59 |
| 天线 | `KRE 105 518` | `DS4-Comba Guangzhou` | `DS4W0` | 427 |
| 天线 | `KRE 105 518` | `EK6-AMPHENOL AIRWAVE` | `EK61B` | 171 |

其他子件的供应商（同一份数据顺带拿到）：
`ROA 128 7812/1`/`ROX 101 42/11` → CB4-ESS Nanjing；`ROR 101 0042/6` → CD3-Jabil Wuxi；
`SDD 513 1733/2` → EX4-DongGuan EONTEC / FB4-Wuhu Jinyi；
`SDF 111 860/1` → EX2-Pinghu Allied / EX1-SHENZHEN FRD；
`SXK 125 7948/2` → DC4-CHONGQING DAJIANG / DJ8-SUZHOU CHUNXING。

### ★ 序列号前缀与供应商的关系（实测验证过，有边界）

**外购机械件**：`Mfg. Unit` 代码的 3 位 == 序列号前 3 位，前 5 位与供应商
**严格一对一**（5 个前缀逐一验证，0 个一对多）。所以前缀可以当供应商的代理键。

**厂内板卡不成立**：`ROA`/`ROX` 的 Mfg.Unit 是 `CB4-ESS Nanjing`，序列号却是
`EA8E*`，前 3 位 `EA8 ≠ CB4`；而且同一供应商下有 7 种前缀
（`EA8E4` `EA8E0` `EA8E3` `EA8E2` `EA8E5` `EA8E1` `EA8D8`）——那是流水号段，
不是供应商码。**"前 5 位 = 供应商"这条规则只对外购件用，别推广。**

### ⚠️ 这个报表自带的 `Yield %` 不是整机良率，是件次级

实测 2026-02~10 全量：`ALL` 行首测 2968（这个是台次），而 `KRF 901 809`
三家供应商的首测加起来 39584 —— 因为**一台整机 16 个滤波器，整机失败一次会被
16 个件各记一次**。所以分母是 (整机测试场次 x 该供应商的件数)。

要按供应商比良率，**必须用整机级 FPY 分组**（`/api/history/yield` +
`ant_supplier`/`fu_supplier`），不能直接读这个报表的 `Yield %`。

顺带：各供应商的件次级良率差异很小（滤波器 80.58 / 81.06 / 80.98，
天线 83.81 / 79.41），这个口径看不出谁好谁坏。

### 现有实现（已显示公司名）

供应商名字**不需要实时请求 UnitProductStructure**——因为「序列号前 3 位 ==
Mfg.Unit 代码」已验证，直接查表即可，省一次网络往返。

```
structure.SUPPLIER_NAMES         前 3 位 -> 公司名，新增供应商往这里加一行
structure.supplier_of()          序列号 -> 公司名，认不出就退回前 5 位
structure.supplier_options()     列出各类件有哪些供应商 + 涉及几台
structure.parents_by_supplier()  反查装了该供应商件的整机集合
structure.suppliers_by_parent()  每台整机装过哪些供应商的件
GET /api/suppliers               下拉选项（显示公司名）
/api/history/yield|trend|runs    ant_supplier / fu_supplier 传公司名
```

```python
SUPPLIER_NAMES = {"F98": "Grentech", "DK4": "Chunxing", "EC1": "Chunxing",
                  "DS4": "Comba",    "EK6": "AMPHENOL"}
```

**产地不算供应商**（用户定的）：春兴苏州 `DK4` 和春兴深圳 `EC1` 合并成一个
`Chunxing` 选项。所以滤波器是 **2 家**供应商不是 3 家。
认不出的前缀退回前 5 位而不是报错/返回空——出现新供应商时界面照样能筛，
只是显示成前缀，不会悄悄丢数据。

实测覆盖率：9 个月全量 + 本地 80 台缓存，**未映射的前缀 0 个**。
本地 80 台的分布：滤波器 Grentech 71 台 / Chunxing 9 台，
天线 Comba 79 台 / AMPHENOL 1 台。

### 混装情况（影响按供应商归因的可信度）

实测 628 台：

| 件 | 分布 | 混装 |
|---|---|---|
| 滤波器 | Grentech 337 台 / Chunxing 291 台 | **0 台**，两家完全互斥 |
| 天线 | Comba 427 台 / AMPHENOL 171 台 | **9 台**装过两家（换过天线） |

滤波器按供应商分组是干净的、可直接比良率。天线那 9 台会同时出现在两个
供应商的筛选结果里，比良率时要留意（占 1.5%）。
另有 39 台没有天线装配记录（628 台里只有 589 台有）。

⚠️ **`RE_FU = r"\bF98BX[A-Z0-9]{5}\b"` 这个正则只认 F98BX**
（`extract_serials.py` 和本文档"关键正则"那节都有），漏掉 `DK4G5` 和 `EC1G5`
两家，实测漏约 45%（3801+922 件 vs F98BX 的 5496 件）。

## 查询条件（界面，用户定的）

### ★ 两种查询方式二选一（2026-10-04 改）

用户要求：**按条件查 和 按序列号查 是两种并列、但二选一的方式**，界面上
用 tab 体现（`static/index.html` 的 `.mode-tabs`，`static/app.js` 的
`QUERY_MODE` + `setMode()`）。选中哪个，另一组控件就 `<fieldset disabled>`
整体变灰：

| 查询方式 | 填什么 | currentParams() 产出 |
|---|---|---|
| `cond` 按产品/时间/供应商 | 产品号 + 起止时间 + 天线/滤波器供应商 | 产品/日期/供应商有值，**序列号为空** |
| `sn` 按序列号列表 | 整机序列号（逗号/换行分隔） | 序列号有值，**产品/日期/供应商一律不传** |

★ 关键：`doQuery()` 和 `currentParams()` 都按 `QUERY_MODE` 二选一取参，
不要再直接读 `#fSns` / `#fAntSup` —— 否则两种方式会串味。按序列号模式下
`p.product` 为空，所以测试点级失败分解（`/api/yield`、`/api/failures`，
需要产品号）不显示，这是预期不是出错；良率和测试结果列表照常出。

入口各自校验必填：`sn` 模式序列号不能空，`cond` 模式产品号不能空，
否则直接提示、不发注定失败的请求。

### 控件清单

```
查询方式 tab   cond / sn 二选一
--- cond 组 ---
产品号        文本 + datalist
开始/结束时间  <input type="datetime-local">
              ⚠️ 控件显示格式（12/24 小时、日期顺序）由浏览器语言决定，
                 HTML 控制不了。值格式是 "2026-09-14T08:00"（带 T），
                 发给后端前要用 dtVal() 换成空格
天线供应商    序列号前缀下拉，来自 /api/suppliers
滤波器供应商  同上
--- sn 组 ---
序列号列表    文本域，逐台列出
--- 公共（两种方式都用）---
测试类别      两个复选框 28 / 212（界面只写数字，不加业务说明——用户定的。
              28 = KRD PIM，212 = KRD PIM after GBI，但界面上不展开）
```

**已去掉**：
- 「查失败分支」勾选框（2026-10-04）—— 改成**默认就查、但放后台**，见下面
  「失败分支两阶段加载」那节
- 「只看 PIM 测试点」勾选框（`fPim`，2026-10-05）—— 它只喂给测试点级接口
  （`/api/yield`、`/api/failures`），那些块前端已删，所以它**勾或不勾结果
  完全一样**。证据：`currentParams()` 只被 `doQuery` 用，而 `doQuery` 构造的
  `histParams` 不含 `only_pim`。★ 后端 `only_pim` 参数保留（接口还在）。
  失效但还显示的控件比没有更糟 —— 用户会以为自己在筛选。
- **「日期/产品框里按回车直接查」的键盘绑定**（2026-10-05）—— 见下节

**已去掉**：
- 测试次数（良率口径固定 FPY 后它没意义了）
- 分组维度（失败分解固定按分支）
- 「填入全部序列号」按钮（二选一后序列号是独立方式，这个按钮没意义）
- 「刷新数据」按钮（用户说没用）—— ★ 但后端 `/api/refresh` 接口**保留**
  （清缓存的唯一手段，有测试锁 `test_no_cache.py`、`test_auth.py`），
  只是前端不再有入口

★ **改查询条件不自动重查**（见前面"改查询条件绝不能自动重查"那节）：
所有条件控件 `change -> markDirty()` 只加按钮光圈（`.btn-dirty`），点 Search
才查。换查询方式 tab 也触发 markDirty。
★ 原来还配一句"查询条件已修改，点「查询」后生效"的文字（`#dirtyHint`），
2026-10-05 按用户反馈**删掉**了 —— 按钮光圈已经够说明问题，那句话是噪音。

### ⚠️ 输入框尺寸：CSS 选择器必须列出 datetime-local（2026-10-05 修）

症状：**时间框比旁边的文本框、下拉框明显矮一截。**

根因：`eds.css` 的选择器只写了 `.field input[type=text]`，`datetime-local`
匹配不上，于是退回浏览器默认样式。实测计算后的值：

| 控件 | 高 | 字号 | 边框 |
|---|---|---|---|
| 产品号 `input[type=text]` | 32px | 14px | 1px |
| **时间框 `datetime-local`** | **21px** | **13.3px** | **2px** |
| 供应商 `select` | 32px | 14px | 1px |

修法：把 `datetime-local`/`date`/`time`/`number` 都列进那条规则。
★ **不要**图省事写成笼统的 `.field input` —— 那会把测试类别里的复选框
也撑成 32px 高。实测修复后五个控件高度全部 32px。

### ★ 产品号候选要轮询补全（2026-10-05）

后端**故意**把"问 MI 最近半年有哪些产品号"放在后台线程（`_discover_products_async`），
因为模块导入时同步去问会卡 90 秒、服务起不来（档案前面记过这个坑）。
那次探测要几十秒，而前端 `/api/options` 是页面一打开就调的 —— 所以第一次
**只能拿到默认的那一个产品号**，datalist 里就只有一条。

修法（纯前端，零接口改动）：`pollProducts()` 退避式再取几次 `/api/options`
（5/10/15/25/30/40 秒），把后台新发现的产品号补进 datalist；数量不再增长
就提前停。提示文字同步显示"正在向 MI 探测…"/"候选 N 个产品号"。

### ★ 产品号 Combobox 替代 datalist（2026-10-07，用户反馈"没有下拉菜单"）

**症状**：用户看不到下拉可点。原来是 HTML native `<datalist>`：
- 下拉面板**浏览器自己画**，各浏览器不同
- 触发条件不明显（需双击 input，或开始输入才显示）
- 没有可见的下拉箭头 → **用户不知道可以点**
- 样式不受 eds.css 控制

**修法**（`static/index.html` + `eds.css` + `app.js`）：自定义 EDS 风格 Combobox

```html
<div class="combo" id="prodCombo">
  <input id="fProduct" role="combobox" aria-autocomplete="list"
         aria-expanded="false" aria-controls="prodMenu">
  <button class="combo-trigger" id="prodTrigger" tabindex="-1">▾</button>
  <ul class="combo-menu" id="prodMenu" role="listbox" hidden></ul>
</div>
```

- 右侧 **▾ 永远可见、永远可点**
- 面板 `<ul>`：白底、`var(--border-subtle)` 边框、`var(--elev-3)` 阴影、
  `var(--fill-selected)` hover 高亮、字号 14px —— 和其他 EDS 控件完全一致
- 保留**自由输入**（input 非 readonly，可打任意产品号，不受列表限制）
- **输入时实时筛选**面板项（contains 匹配）
- **键盘**：↑↓ 导航 / Enter 选中 / Esc 关闭

★ **关键 bug 修过**：`openProdMenu()` 原来用当前 input 值作筛选条件，
点 ▾ 时默认值 "KRD 901 330/11" 把其他 3 个候选过滤掉，面板只剩 1 项。
修法：`openProdMenu(withInputFilter)` 加参数 —— 点 ▾ / focus 显示**全部**，
只有 input 事件触发时才按输入值筛。

★ **click vs mousedown**：选项的点击处理器用 **mousedown** 而不是 click：
input 的 blur 会先于 click 触发，导致 click 事件在面板已经被关闭后落空。
mousedown 用 `preventDefault()` 阻止 input 失焦，然后同步 pickProdOption。

★ **多包一层会打破栅格 stretch**（2026-10-07 修）：
原来 `<input>` 直接是 `.field`（flex-column）的子元素，默认 stretch 到栅格列宽。
加 `<div class="combo">` 包裹后，**combo 自己 stretch，但里面的 input 不再
自动占满 combo** —— 退回 inline-block 默认宽度，看着比旁边的时间框明显窄。
修法：`.combo input { width: 100%; box-sizing: border-box; }`。
实测 5 个控件（产品号/开始时间/结束时间/天线供应商/滤波器供应商）尺寸完全
一致：268px × 32px × 14px。

★ **options 加 `default_product` 字段**（独立于 products 列表）：
原来前端 `$('fProduct').value = products[0]`，加 Crete 后字母排序 316/11
排到第一，用户熟悉的默认 330/11 不见了。契约锁在
`tests/test_api.py::test_options_默认产品是主产品而非字母排序第一个`。

实测 playwright：默认值正确、点 ▾ 显示 4 个候选、输入 "328" 筛到 1 项、
键盘 ↓/Enter 导航正确、0 JS 报错。

### ★ Crete 平台产品硬编码为默认候选（2026-10-07）

**问题**：MI 的 `products_in_range` 只返回"最近 183 天**在产**的产品"。
Crete 平台产品（`KRD 901 328/11`、`329/11`、`316/11`）当下不一定在产 →
discover 不返回 → 用户在下拉里**根本看不到**，必须手打完整产品号。

**修法**（`api.py` 加常量 `CRETE_PRODUCTS` + `data_source.discover_products`
改成合并不覆盖）：

```python
# api.py
DEFAULT_PRODUCT = "KRD 901 330/11"
CRETE_PRODUCTS = ["KRD 901 328/11", "KRD 901 329/11", "KRD 901 316/11"]
DEFAULT_PRODUCTS = [DEFAULT_PRODUCT, *CRETE_PRODUCTS]

# data_source.MiSource.discover_products：合并保留 _products
if prods:
    self._products = list(dict.fromkeys(list(self._products) + list(prods)))
```

★ 关键是**合并不覆盖**：原来 `self._products = list(prods)` 会把 Crete
覆盖掉，于是服务起几秒钟后下拉又只剩 MI 探测到的在产产品，Crete 消失。

新增 Crete 产品：往 `CRETE_PRODUCTS` 加一行即可，不用改前端。

契约锁在：
- `tests/test_api.py::test_options_包含Crete平台产品`
- `tests/test_mi_source.py::test_discover_products_合并不覆盖已有的`

★ 顺带修了测试 fixture（`tests/conftest.py` 的 `_build_live_client`）：
原来直接 `_source._products = list(client.products)` 覆盖，于是测试里
Crete 永远不在。改成合并 `client.products + CRETE_PRODUCTS`。

### ⚠️⚠️ 服务进程没真的重启，404 Not Found 的真凶（2026-10-07 诊断）

**症状**：失败分支/测试点查询失败：**Not Found**（HTTP 404）。

**根因**（现场证据）：
```
磁盘上 api.py 最后修改: 2026-10-06 18:14  （有 /api/history/branches 端点 ✓）
服务进程 PID 30872 启动时间: 2026-10-03 23:56:45   ← 4 天前的进程！
                                                  跑着的是 10-03 的老代码
                                                  那时还没有这个端点
```

用户前面说"重启了服务"，但老进程一直占着 8080 端口，**新进程启动时端口被
占直接失败**，用户的浏览器连的始终是老进程 —— 内存里的代码从没更新过。

**修法**：
1. `Stop-Process -Id 5560, 30872 -Force` 杀掉老进程
2. 用 `Start-Process` 独立起新进程（不依赖当前 PowerShell session）
3. 轮询 `/api/health` 等就绪
4. curl `/api/history/branches` 验证 HTTP 200

★ 教训：**"重启了服务"和"服务真的重启了"是两件事**。诊断时必须查进程的
`StartTime`，不能只信口头说"重启了"。前面也踩过类似的——"改了代码没生效"
实际上是浏览器缓存，不是没保存。都是同一个"**读最终生效值而不是源文件**"
原则的实例。

★ **启动服务不能用 Start-Job**（会跟当前 shell 一起死）；要用
`Start-Process -WindowStyle Hidden` 让它成为独立进程，shell 退了服务不死。
`start-local.ps1` 后台模式用 Start-Job，所以**那个脚本启的服务跟着
PowerShell window 一起死**。手动 Start-Process 更稳。

### ★ 查询进度条（2026-10-05）

一次查询 60~120 秒，原来只把按钮变灰显示"查询中"，用户不知道要等多久、
也分不清"在跑"和"卡死"。现在查询条件块下面给进度条 + 已用秒数 + 阶段。

⚠️ **百分比是估算的，不是真实进度** —— 后端是一个同步请求，不上报进度。
两条自律（别改掉）：
1. **封顶 90%**：没真拿到结果绝不画满，不假装精确
2. 预估用**上一次真实耗时**（存 localStorage 的 `pim_last_query_s`），
   首次用 90 秒经验值；超过预估 1.2 倍就改说"比平时久"
3. ★ 只记录 **≥10 秒**的耗时当基准 —— 命中 60 秒内存缓存的查询可能只要
   0.01 秒，拿它当基准会让下次进度条瞬间冲到 90% 再干等

### ★ 查询中改条件自动"中断"（2026-10-08）

**要求**：先点查询后改条件 → 查询状态立刻恢复，不要锁页等 60 秒。

**实现**：版本号机制 `QUERY_RUN_ID`。
- `doQuery` 开头：`const myRunId = ++QUERY_RUN_ID; QUERY_IN_FLIGHT = true;`
- `markDirty` 时如果 `QUERY_IN_FLIGHT`：递增 `QUERY_RUN_ID`，立刻恢复按钮和进度条
- `doQuery` 的 await 回来：对比 `myRunId !== QUERY_RUN_ID`，不等就不渲染

★ 这是"**UI 中断**"不是"真的中断网络请求" —— 后端那次 fetch 继续跑完，
只是前端丢弃结果。原因：FastAPI 后端是同步的，不响应 abort；而且用户
真正要的是"UI 立刻能用新条件操作"，不是"立刻停 MI 请求"。

★ **纯 CSS 变量名错误导致进度条贴按钮**（同一天修）：
`.qprogress{margin-top:var(--sp2);}` —— `--sp2` 在 `eds.css` 不存在
（只有 `--sp-sm/sp/sp-lg/sp-xl/sp-xxl`），解析失败退回 0 → 进度条紧贴
按钮。改成 `--sp-lg`（16px）后视觉和功能都好了。
这是**第二次踩"CSS 变量名错了但不报错"的坑**（档案前面还有一处）。

### ⚠️ 回车也算"自动重查"，键盘绑定一样要去掉（2026-10-05 修）

症状：**改了查询时间，查询按钮自己变成"查询中"** —— 用户只是在改条件。

根因不是 change 事件（那个早修了），是一条**键盘绑定**：

```js
// 已删除
['fFrom','fTo','fProduct','fCat'].forEach(id => {
  $(id).addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); doQuery(); }
  });
});
```

`datetime-local` 控件在键盘输入日期、或用日历弹层选完之后，**按回车是很
自然的确认动作** —— 一按就发起一次 60~120 秒的查询，整页锁死。

★ 原则要推广：昂贵操作只能由**明确的点击**发起。"顺手的键盘动作"和
"改下拉自动触发"是同一类问题，光修 change 不够。
（顺带：`fCat` 现在是复选框组的 `<div>` 容器，本来也接不到回车，
那条绑定对它一直是无效的。）

实测（无头浏览器）：改时间 + 按回车 → 查询请求 **0 个**，按钮保持
`Search` 不禁用，只出现"条件已改"提示和按钮高亮；产品号框按回车同样 0 个。
### ★ 结果区精简到 4 块（2026-10-04 大改）

用户砍掉了一批"文字统计"块，只留下良率 + 曲线这条主线。现在结果区只有：

```
1 查询条件
2 FPY 良率与趋势   ← 可展开每个时间段的 DUT 明细（产品号/SN/供应商/结果/失败分支）
3 测试曲线（RTD Knock）  ← 从良率表点「查看」联动；多卡片，每台一张图
4 下层件追踪
```

**已删除的 5 个块**（前端 HTML + JS 都删了，别找了）：
- 测试结果列表（`tileRuns`）—— 功能并入良率表的 DUT 明细展开
- 测试点失败统计（`tileSummary`）
- 失败分解按分支（`tileGroups`）
- 失败分布热力图（`tileHeat`）
- 单台详情（`tileSingle`，文字统计版）—— 用户要的是曲线，不是文字

★ **后端接口保留**：`/api/yield`、`/api/failures`、`/api/testpoints` 还在
（有测试锁、将来可能再用），只是前端没有入口了。删前端块时**不要**连带删
这些接口。

★ 随块删掉的 JS：`renderRuns` / `exportRuns` / `renderSummary` /
`renderGroups` / `drawFailRateChart` / `drawFailTable` / `renderFailures` /
`exportGroups` / 旧的 `doSingle`（文字版单台详情）/ 状态变量 `LAST`。
`doRuns` 保留（仍拉 `/api/history/runs`），但**不再渲染表格**，`RUNS` 只喂
给良率表的 DUT 明细展开和"查看"联动。

### ★ 单台详情 → RTD Knock 曲线（联动，不再手动输入）

良率表 DUT 明细行的「查看」→ `sendToSingle(sn, row)`：把那台的
**序列号 + 测试当天日期 + 产品号**填进（隐藏的）RTD 会话查询框，
自动列当天会话、选中、填频率、取曲线。用户不用再手打查询条件。

- RTD 会话按 **(device_id≈序列号, 日期)** 索引，和 MI 的秒级时间**不能精确拼**，
  所以用"序列号 + 当天日期"去 RTD 找会话（档案"RTD 取数协议"那节有依据）。
- `onSessionPicked` 填完频率后**自动** `loadSessionCurves()` 取一次曲线
  （从"查看"进来就是想直接看图，不该再点一次）。频率/分支下拉改了也自动重取。
- ⚠️ **RTD 凭据约 11 小时过期**。`sendToSingle` 列会话前先查 `/api/health`
  的 `rtd.available`，不可用就提示"已过期 N 小时，点 ↻ 刷新"，**不要**静默
  列不出来（踩过：提示空白，看着像点了查看没反应）。判据用 health 而不是
  "列会话按钮是否禁用" —— 后者由 initSession 异步设置，点查看时可能还没设好。

「测试曲线（RTD Knock）」块的手动输入框（日期/产品/类别/序列号）**still 在
DOM 里但 `display:none`**，仅作 JS 读写用（点查看时代码往里填值），不给用户看。

### ⚠️⚠️ 失败整机多时一次性查会超时，改成分批串行（2026-10-07 修）

**症状**：失败整机 10+ 台的时间段，DUT 明细里**所有失败行**的失败分支/
失败测试点列都显示"查询失败"。

**根因**（真实实测证据）：
```
我机器真实测 6 台失败整机：
   单台独立查：每台 0.9-2.2 秒（6 台总和 6.9 秒）
   6 台并发一起查：27.5 秒  ← 比串行还慢 4 倍
```
档案一直写"**8 路并行**"，但实测**完全没加速反而更慢**。原因：
- `MiClient` 全局共享单个 `requests.Session + HttpNegotiateAuth`
- `requests_negotiate_sspi` 走 Windows SSPI 做 Kerberos 协商
- **SSPI 调用不是线程安全**，多线程并发时内部状态冲突，被串行化
- MI 服务端对同一 Kerberos 身份也可能有并发限制

用户机器失败整机 10+ 台 → 预计 100-300+ 秒 → 撞到浏览器 fetch 默认超时 →
前端 catch 后 BRANCHES_STATE = 'error' → 所有失败行显示"查询失败"。

**修法（纯前端 `loadBranchesAsync`）**：
1. **分批串行**：每批 3 台（`const BATCH = 3`），一批一批 await
2. **一批完成立即 `refreshOpenDutRows()`**：已查完的 DUT 立刻看到数据填上
3. **某批失败不影响其他批**：那批涉及的 SN 标 `_branches_status='error'`，
   其他 SN 照常显示数据
4. **行级状态优先于全局状态**：`renderDutRows` 里查到的行用 `_branches_status`
   判断，没轮到的行才看 `BRANCHES_STATE === 'loading'` 显示"查询中（N/M）…"
5. **进度条**：`BRANCHES_PROGRESS = {done, total}` 让还没轮到的行知道进度

**为什么不修"并发不加速"的底层问题**：
- SSPI 的线程安全不是我们能控制的
- 每线程独立 session 大改动，MI 服务端也可能有并发限制
- 分批串行**在真实环境下更稳定**：每个请求小、独立，不撞超时；
  并发不生效时总耗时和"所谓的并发"一样
- 用户要求的就是"**先显示其他的，这个结果等抓到以后再显示**" —— 这正是分批的 UX

**契约锁**：playwright 验证脚本证明：
- 15 台 → 5 批 × 3 台调用
- 中间批失败，其他 12 台照常显示 BR 数据
- 失败的 3 台单独显示"查询失败"+ title 带具体错误消息
- 0 JS 报错

实测场景（用户 2026-09-28 一天失败 10+ 台）：
```
修法前：一次查 10+ 台 → 浏览器超时 → 全部"查询失败"
修法后：分 4+ 批串行查 → 每批 3 台独立 10-30 秒 → 每批完成立即填 3 行
         总耗时 1-2 分钟，但用户持续看到结果填充
```

### ⚠️⚠️ 失败分支/测试点查询出错时不许静默吞异常（2026-10-06 修）

症状：用户展开 DUT 明细，失败分支和失败测试点两列都显示**"查询失败"4 个字**，
不知道具体为什么。

根因：`data_source.failed_detail_for` 原来有一个
```python
try:
    hists = self.client.child_test_histories(want, workers=8)
except Exception:
    return {"branches": {}, "testpoints": {}}  # ← 异常被吞成空 dict
```

MI 真的报错时，这段代码把异常**静默吞成空数据**，API 层返回 **200 OK + 空字典**。
前端的 `BRANCHES_STATE` 被设成 `'done'`（因为 HTTP 200 没触发 catch），
显示"无"而不是"查询失败"。

但用户又明明看到"查询失败" —— 说明**真实失败发生在别处**（可能是网络断、
fastapi 中间件 500、MI 响应慢被浏览器 timeout），而不是 child_test_histories
内部异常。这才导致 fetch 真的 throw、BRANCHES_STATE = 'error'。

修法：
1. **`data_source.failed_detail_for` 异常不再吞** —— 直接传给 API 层
2. **API 层 catch 异常** → 转成 `HTTPException(502, "分支/测试点查询失败：...")`
3. **前端 fetch 对 502 自动 throw**，`BRANCHES_ERR` 被设置为具体错误消息
4. **前端在 DUT 明细表顶部用红字显示 BRANCHES_ERR**（已经加好）

现在用户看到"查询失败"时，表格顶部**必定有具体错误消息**：
```
失败分支/测试点查询失败：RuntimeError: MI 鉴权失败...
```

契约锁在 `tests/test_api_history.py::test_失败分支查询出错时返回502带具体错误`。

★ 教训：静默吞异常 (`except: return {}`) 是**调试噩梦** —— 系统"看起来没出错"
但错误信息完全消失。要么让异常传出去，要么至少打日志。

### ★ DUT 明细表头筛选（2026-10-08 从全局筛选条改）

**要求**：筛选器要在**每列表头**里，不是在表格上方。

**实现**：
- 良率表上方的全局筛选条删除
- `renderDutRows` 时表头渲染**两行**：第 1 行列名，第 2 行筛选控件
- 筛选器放在 4 列头部下：
  - **第几次** `<select>`：全部 / 只第 1 次
  - **整机 SN** `<input>`：包含文本
  - **天线供应商** `<select>`：全部 / 具体供应商
  - **滤波器供应商** `<select>`：全部 / 具体供应商

**全局状态 `DUT_FILTER`**：
```js
const DUT_FILTER = { firstOnly: '', sn: '', ant: '', fu: '' };
```
- 筛选控件 change/input → 写回 `DUT_FILTER` + 调 `refreshOpenDutRows()`
- 多个展开桶**共享筛选状态**：在任一桶的表头改筛选，所有展开桶一起过滤

**供应商下拉选项动态收集**：
```js
collectDutSuppliers('ant_suppliers')  // 从当前 RUNS.rows 的实际供应商
```
反映用户查询范围里真实出现的供应商，不是全量清单。

**CSS**：筛选控件高 26px（比正常控件 32px 矮一截，避免 thead 太高）、
字号 13px、浅灰背景区分普通列头。`.filter-row` + `.col-filter`。

**契约锁**：playwright 验证 11 项（表头 2 行 / 4 个控件存在 / 下拉动态选项 /
SN+天线+滤波器+第几次 分别筛选 + 组合 + 控件事件写回 DUT_FILTER + 0 JS 报错）。

★ 全局筛选条 vs 表头筛选的历史：10-08 上午先做了全局筛选条（简单），
但用户要求"**在表头直接选**"，就改成了表头。两者功能等价，表头更直观
但代码量稍多（每个展开桶都要重画一组筛选控件）。

### ★ DUT 明细筛选条（2026-10-08）

良率表上方的 4 个本地筛选控件：**SN 包含 / 天线供应商 / 滤波器供应商 / 清空筛选**
（+ 原来的 `#fFirstOnly` "只看第 1 次测试"）。

全部纯本地筛选 —— 数据已经在 `RUNS.rows` 里，不重新查询（一次查询 60~120 秒）。
在 `renderDutRows` 开头依次过滤，筛选控件 change/input → `refreshOpenDutRows()`
重渲染已展开的桶，立刻生效。

★ 供应商下拉**从当前查询结果里收集**，不是全量清单：
`fillDutSupplierOptions()` 在 `doRuns` 完成后扫 `RUNS.rows` 的 ant_suppliers/
fu_suppliers 字段去重排序填选项。这样选项反映用户实际看到的供应商，不会
出现"选了但筛完 0 行"的困惑。

★ 实现约定：
- 全局筛选条，不是每列表头 —— 用户前面说"表头筛选"，但真"表头"要每个展开
  桶都重画一组控件，UI 冗余。全局筛选条等效但简洁。
- `ant_suppliers` / `fu_suppliers` **是公司名列表**（可能一台装过多家，
  如换过天线），所以用 `includes()` 判断包含。
- 保留用户已选值：新查询完成后如果新结果里还有这个供应商就保留选中，
  否则 reset 回"全部"。

契约锁在 playwright 验证脚本（4 项筛选 + 组合筛选 + 清空）。

### ★ DUT 明细加「失败测试点」列（2026-10-06）

需求：失败分支列只告诉你 BR28 坏，但不知道具体哪个频率/测量项失败。
用户要**比分支更细**的信息（选的 B+①：失败测试点编码+名称 + 加新列）。

数据来源：`ChildSerialNumberTestHistory` —— 和「失败分支」用的**同一个响应**，
只是原来只提取了测试点编码的分支号，别的字段（名称、实测值、故障码、
Test Start Date）都扔了。扩展成**一次 MI 请求同时拿分支和失败测试点详情**，
网络开销 0 增量。

实现层次：
- `mi_client.failed_testpoints_from_child_history(obj)` 按场次分组：
  `{started: [{code, name, branch, freq_mhz, measured, fault_code}, ...]}`
  started 取前 19 字符（和 FunctionalTestAndRepairHistory 的 TestStartTime 一致）。
  频率**优先从名称取**（档案硬约束：knock 类编码第 5 段恒为 0），取不到才用
  编码第 5 段的频率索引映射。
- `data_source.failed_detail_for(serials)` 一次 MI 请求同时返回
  `{"branches": {...}, "testpoints": {...}}`，前端一个接口拿两种数据。
- `/api/history/branches` 返回加 `testpoints` 字段（向后兼容：`branches` 字段保留）。
- 前端 `loadBranchesAsync` 按 `(serial_number, started)` 把 testpoints 合并到
  `RUNS.rows`，`renderDutRows` 加「失败测试点」列。

显示规则（紧凑 + 悬停看全量）：
- 每个失败测试点显示一行 `BR28 @ 1896 MHz`
- 最多显示 3 个，超出显示 `+N 个`
- 鼠标悬停到单元格，title 弹窗显示每个失败点的 **完整名称 + 实测值 + fault code**
- 通过行显示 `—`；加载中显示「查询中…」/「查询失败」/「无」

★ `(serial_number, started)` 作主键匹配两套 MI 报表（ChildSerialNumberTestHistory
的 Test Start Date 和 FunctionalTestAndRepairHistory 的 TestStartTime），
一旦两边时间格式不一致会匹配失败。先按前 19 字符对齐，看实际数据再调。

契约锁在 `tests/test_mi_client.py::test_失败测试点按场次分组`、
`::test_失败测试点空结果不报错`、`::test_失败测试点频率优先从名称取`。

### ★ 卡片上的「↻ 重试」按钮（2026-10-06）

需求：**图没出来时怎么手动重新加载？** 原来卡片只有 ✕ 关闭按钮，取数失败
用户只能关掉再从良率表重新点一次「查看」。

修法（`static/index.html` 卡片模板 + `addCurveCard`）：在 ✕ 旁边加 `↻`，
点一下**清掉当前状态**（图/KPI/会话下拉/频率下拉/缓存的 sessions 和 data）
再重新调 `cardListSessions(card)`，整条取数链重走一遍。

为什么这样做够用：
- 凭据过期 → `cardListSessions` 开头的 `ensureRtdReady` 会自动刷新
- 网络瞬时错误 → `rtd_client._call` 底层已经自动重试 3 次
- 还是失败 → 用户点 ↻ 等于重走一次（相当于关掉再从良率表点一次，但省事）

契约：按钮用 `.btnRetry` class（不是 .btn-icon.btn-icon 复合），
title="重新加载这张图（出错或没出图时用）"。

实测：路由拦截让第一次 `/api/sessions` 返回 502 → 卡片显示"列会话失败"
→ 点 ↻ → **真实画出曲线**（取数 2 秒、画图 225 ms），0 个 JS 报错。

### ★ 非 PIM 测试行不给「查看」按钮（2026-10-06 修）

症状延续前面那个坑：用户点了「查看」后看到**"这次测试没有 knock 曲线：
频率列表是空的……"**。精确定位是对的 —— 他点的那行**本来就是**烧机前
(cat=26) 或辅助测试(cat=38)，RTD 侧对应那场也确实没频率。

但从用户视角：点了「查看」却看不到曲线，像出了问题。根因是**不该让他
点那些不是 PIM 的行**。

修法：`renderDutRows` 里只给 **cat=28（RADIO_PIM）/ 212（RADIO_PIM after GBI）**
的行渲染「查看」按钮；其它类别(26/38/29/32)显示 `—` 占位 + title 说明
"该类别没有 Knock 曲线"。

★ 为什么硬编码 `PIM_CATS = {28, 212}` 而不是等 options 返回才知道：
前端取 options 一场 1.4 秒，不可能展开 50 行 DUT 明细就查 50 次。
cat 字段 MI 直接给，是整数，28/212 对应 PIM 的事实已经锁死。真有新 PIM
类别出来再加。

实测（假 RUNS 混合 5 种类别）：cat=28/212 两行有「查看」按钮；
cat=26/38/29 三行显示 `—` 占位；没 JS 报错。

### ★ RTD 网络瞬时错误自动重试（2026-10-06）

症状：**"列会话失败：RTD 取会话失败：ConnectionError (10054, 'An existing
connection was forcibly closed by the remote host')"**。

根因：这是 Windows 的 `WSAECONNRESET` —— 远程强制关闭了连接。属于**瞬时**
网络错误（代理抖动、CDN 边缘节点换、VPN 短断、Cloudflare 边缘节点关连接），
不是我们代码的 bug，重试一次通常就通。

原来 `rtd_client._call` 不重试，用户每次网络抖都要点"查看"重试一次。

修法（`_call` 底层加重试）：
- 对 `requests.exceptions.ConnectionError` / `Timeout` 自动重试 **3 次**
  （1s、3s 指数退避）
- **只重试连接层错误**：HTTP 401/403 是凭据过期、500 是模板不匹配，
  重试无用，立刻报错
- 重试全失败才向上抛，消息里说清是"网络抖动或代理/VPN 中断"
- Dash 的 `_dash-update-component` 是纯查询（取数据），重试是幂等的，无副作用

契约锁在 `tests/test_rtd_client.py::test_网络瞬时错误自动重试`、
`::test_重试超过上限还是失败就报错`、`::test_凭据错误不重试`。

### ⚠️⚠️ 卡片默认选第一场会导致"曲线出不来"（2026-10-06 修）

症状：**用户在良率表点某台 DUT 的「查看」，卡片说"这次测试没有 knock 曲线"**，
以为取不到数据。

根因：**一台机器一天会跑好几场测试（PRE_UNIT / BOARD / ADDITIONAL / PIM 等），
只有 RADIO_PIM 那场才有 Knock 曲线**，别的类别频率列表是空的。

### ★★ MI 和 RTD 的时区差 + 精确定位到用户点的那次（2026-10-06 修）

**关键发现**：原来档案里写"RTD 和 MI 时间不能精确拼"是错的 —— 其实两边
**完全对应**，只是**时区偏移 8 小时**：

```
MI 的 started（北京时间）: 15:12:49  15:59:29  18:10:00  21:49:30  22:10:15
RTD 的 start_time（UTC）  : 07:12:49  07:59:29  10:10:00  13:49:30  14:10:15
                           +00:00  <-------- 中国 UTC+8 --------> +08:00
```

秒和分**严丝合缝**。而且更重要的 —— **`test_count` 两边完全一致**：
```
MI  cat="28" test_count=2  ←→  RTD testcat="RADIO_PIM (28)" test_count=2
```

所以**最可靠的匹配键是 `(test_count, 类别数字)`**，两边都是整数。不用处理
时区偏移、不用关心时间格式。

实现（`sendToSingle` → `addCurveCard(sn, date, product, want)` → `cardListSessions`）：
1. 良率表点"查看"时把 `{test_count, category, started}` 作为 `want` 传进来
2. 列完会话后，`findIndex(ss => ss.test_count === want.test_count && catOf(ss) === want.category)`
3. 匹配到就**在那条选项前加 ✓ 标记并自动选中**（其他 PIM 场次保留 ★）
4. 没匹配到再退回"选第一个 PIM 场次"（兼容 want 为空的情况）
5. 卡片标题里显式写出"第 N 次（类别 X）"，让用户确认是他点的那次

★ 没用时间匹配是因为：秒精度在产线不是绝对可靠的（下行 UTC、业务用北京时间
容易混），而 test_count 是明确的整数索引、两边都从同一处来，比时间稳。

实测（三次模拟从良率表不同行点查看）：
- 第 2 次 cat=28 → 下拉自动选中 ✓ 10:10:00 (RADIO_PIM test_count=2)
- 第 3 次 cat=28 → 下拉自动选中 ✓ 13:49:30 (RADIO_PIM test_count=3)
- 第 4 次 cat=28 → 下拉自动选中 ✓ 14:10:15 (RADIO_PIM test_count=4)
全部精确定位、画出图。

### ★ 卡片同时保留"精确定位 + 完整下拉"的理由

精确定位到用户点的那次之后，**没有把下拉藏起来**。因为：
- 用户经常想看同一台当天其他 PIM 场次（比较首测和复测的曲线差异）
- 真遇到匹配不上的情况（时间偏差、RTD 没录到那次等），用户可以手动挑

下拉里视觉区分：
- `✓` 对应用户点的那次（当且仅当精确匹配时）
- `★` 其他 PIM 场次（有曲线可看）
- `　` 非 PIM 场次（没曲线）

### ★ RTD 曲线改成多卡片（2026-10-04，每台一张图）

用户要"多选时把曲线同时展示"。做法 B：**每点一台「查看」就 `addCurveCard()`
往下 prepend 一张独立卡片**，点多台堆多张，各自带 会话/频率/分支 下拉 + 一张图
+ ✕ 关闭，互不干扰。

- 卡片结构在 `index.html` 的 `<template id="curveCardTpl">`，JS 克隆它。
- **状态从全局挪到每张卡片自己身上**（`card = {sn,date,sessions,data,selFreq,...}`），
  不再有全局 `SESS_LIST`/`SESS_DATA` —— 否则多张卡片会互相覆盖。
- 底层取数函数没变（`/api/sessions`→`/api/session/options`→`/api/session/curves`），
  只是每张卡片各调一遍。频率/分支下拉改了只重取**那一张**。
- 新卡片 `prepend` 在最上面（最近看的在前）。
- 实测（假 RTD 数据，因真凭据过期）：点两台 → 2 张卡片叠加、各有独立
  频率(3)/分支/会话(2)下拉、图各自画出、标题带 SN+日期+产品号。

### ★ RTD 凭据过期时自动刷新（2026-10-04）

用户要求：取数时凭据过期能自动刷新，不用先手点 ↻。

- `ensureRtdReady(onProgress)`：查 health，可用直接返回；不可用就调
  `/api/rtd/refresh` 并轮询到结束，返回是否可用。进度通过 `onProgress`
  回调显示在触发它的那张卡片上。
- **防重复锁 `RTD_REFRESH_PROMISE`**：多张卡片同时发现凭据过期时，只跑
  一次刷新，其余 `await` 同一个 Promise。★ 两个浏览器抢同一个 profile 会
  互相破坏（档案多处记过），所以绝不能并发刷新。
- `cardListSessions` 开头先 `await ensureRtdReady(...)`，成功才列会话、
  失败提示"可能要在弹出窗口里登录"。↻ 按钮(`refreshRtdCookies`)也走同一套。
- ⚠️ 刷新**不是无感的**：要在本机弹真实 Chrome 走微软单点登录，顺利时
  1~2 分钟自己跑完，登录态也失效时会停在登录页等人工输入。
- 实测（mock RTD：health 先过期、刷新后翻可用）：同时加两张卡片 →
  `/api/rtd/refresh` 的 POST **只发 1 次**（锁生效）→ 刷完两张卡片都自动
  继续列会话并画出图。真凭据这关要本人在机器上登录，没法在验证里跑通。

### ⚠️⚠️ 缓存踩踏：一次查询把慢操作做了 3 遍（2026-10-04 修）

症状：**良率和趋势很快出来，DUT 明细（场次数据）要等很久甚至等不到。**

根因：前端点一次「查询」会**并发**打三个接口 —— `/api/history/yield`、
`/api/history/trend`（这两个在 `doTrend` 里 `Promise.all`）、
`/api/history/runs`。三者走**同一条慢路径**（`_filtered_runs` →
`serials_for` 实时列整机 + `all_test_runs` 拉测试历史，各约 55 秒），
本来指望靠内存缓存复用。

但那两处缓存（`MiSource._ups` 和 `._ensure`）是朴素的"检查-然后-填充"，
**没有「进行中去重」(in-flight dedup)**。三个请求几乎同时进来，都发现
缓存是空的，于是**各自把慢操作做了一遍** —— 这就是缓存踩踏
(cache stampede)。实测（假客户端数底层调用次数）：**测试历史被拉了 3 遍**。

为什么表现成"FPY 快、明细慢"：yield/trend 先发出、先抢到那一遍，所以先
返回；runs 最后发出，要等自己那一遍跑完。

修法（`data_source.py`）：给两处缓存加**按键的锁 + 双重检查**。
第一个请求去取，其余等它取完直接复用。

```python
self._locks: dict[Any, threading.Lock] = {}     # 每个缓存键一把锁
self._locks_guard = threading.Lock()            # 保护上面那个字典
def _lock_for(key): ...                          # 没有就建一把

# _ups / _ensure 都是：先无锁查缓存（快路径）-> 没有再加锁 -> 锁内重查一次
```

★ 按键加锁而不是一把全局锁，是为了让不相关的查询不互相阻塞。
★ 快路径（已有缓存）不加锁，避免给命中的情况增加开销。

契约锁在 `tests/test_api_history.py::test_三个接口并发查时慢操作只做一遍`。

⚠️ **写这个测试时踩过一个假阴性**：假客户端是微秒级的，第一个请求在第二个
开始前就填好缓存了，踩踏窗口根本不存在 —— 测试会"通过"，看着像没问题。
必须给假客户端**人为加延迟**（0.4 秒）还原真实世界几十秒的取数窗口，
才能暴露这个竞态。这是"验证手段本身也要可信"的一个实例。

### ★ 失败分支两阶段加载（2026-10-04，删掉「查失败分支」勾选框）

用户要求：失败分支默认就要查，但**不能让它拖住测试结果显示** ——
"先显示测试结果数据，分支数据可以稍后加载出来"。

```
阶段一  /api/history/runs?with_branches=false     快，立刻能展开看明细
阶段二  /api/history/branches?sns=失败的那些       后台补，到了自动填进表格
```

★ **为什么新开一个 `/api/history/branches` 轻接口**，而不是前端重发一次
带 `with_branches=true` 的 runs：后者会**连带重新拉全部测试历史**（几十秒），
而前端已经有场次数据了，纯浪费。新接口只做一件事——给定整机序列号返回
失败分支，内部就是 `_source.failed_branches_for()`。契约锁在
`tests/test_api_history.py::test_只查失败分支的轻接口` 和
`::test_只查失败分支不拉测试历史`（后者断言不许调 `test_histories`）。

前端实现（`static/app.js`）：
- `doRuns` 固定 `with_branches: false`，成功后 **不 await** 地调
  `loadBranchesAsync()`。
- `loadBranchesAsync()` 只传**失败的**整机序列号（通过的查了白等几十秒），
  回来把 `failed_branches` 合并进 `RUNS.rows`。
- 状态机 `BRANCHES_STATE`：`idle | loading | done | error`。分支列按状态显示
  `查询中…` / `BR3 BR10` / `无测试点级记录` / `查询失败`。
  ★ 必须区分"还在查"和"查了没有"，否则用户不知道该不该等。
- `refreshOpenDutRows()`：数据后台到达时，把**已展开的** DUT 明细重新渲染。
  展开时给 `.dut-cell` 打上 `data-bucket`/`data-by`，收起时删掉 —— 这样后台
  数据到了只刷新用户正在看的那几格。

### ⚠️ trend 比 runs 快，展开时要区分"正在取"和"还没查"（2026-10-04 修）

症状：点查询后趋势表很快画出来，用户立刻点三角展开 → 显示"还没有场次数据，
先在上面点「查询」"，看着像没点查询（用户实际已经点了）。

根因：`doTrend` 和 `doRuns` 是并发的，trend 只算桶所以快，runs 要取全部场次
明细所以慢。表画出来时 `RUNS` 还是 null，而那条提示只区分了"有数据/没数据"，
没区分"正在路上"。

修法：加 `RUNS_LOADING` 标志（`doQuery` 开头置 true，`doRuns` 结束置 false）。
展开时三态提示：正在取 / 取失败+原因 / 还没查询。并且 `doRuns` 返回后调
`refreshOpenDutRows()`，**用户不用收起再展开一次**，数据到了自动填上。

★ 顺带修了 `doRuns` 的 `catch` 静默吞异常（原来直接 `return false`，
`RUNS` 保持 null，用户只看到笼统的"没加载成功"、不知道为什么）。现在记进
`RUNS_ERR` 并显示出来。

### ★ DUT 明细的列与「只看第 1 次测试」（2026-10-05）

列顺序：`测试时间 | 第几次 | 产品号 | 整机 SN | 天线供应商 | 滤波器供应商 |
结果 | 失败分支 | 查看`

- **测试时间**：原来没有这一列，用户看不到具体时间、也无法确认排序。
  现在显式按 `started` 升序排（不依赖后端顺序），同一时刻按序列号稳定排。
- **第几次**：就是 MI 的 `TestCount`（后端 runs 一直有返回，只是前端没显示）。
  `=1` 用蓝色标签「第 1 次」点出来，复测用灰字「第 N 次（复测）」——
  一眼能分清，这是用户要的"怎么区分哪个是第一次测试"。
- **「明细里只看第 1 次测试」勾选框**（`#fFirstOnly`，在良率表上方）：
  **纯本地筛选**（`test_count` 已在 `RUNS.rows` 里），change 只调
  `refreshOpenDutRows()` 重渲染已展开的那几格，**不重新查询** ——
  和「只看失败」同一个取舍（一次查询 60~120 秒，为换个筛选重查不值得）。

★ 为什么需要这个筛选：**良率是 FPY（只算每台第 1 次），而明细默认列全部
场次（含复测）**，所以明细行数会比"首次测试场次"多，用户会以为对不上。
界面上把这句话写出来了（别删），勾上筛选后两者口径一致。

### ⚠️ 验证时踩的坑：refreshOpenDutRows 只找 `#tblTrend` 里的格子

`refreshOpenDutRows()` 的选择器是 `#tblTrend .dut-cell[data-bucket]`。
写验证脚本时我把探测容器插在 `#tileTrend .tile-body` 下（表格**外面**），
于是勾选筛选"没反应" —— 那是**测试脚本的问题，不是功能的问题**。
改成走真实渲染路径（注入 TREND 调 `renderTrend` → 点三角展开）后验证通过：
5 条场次按时间升序、勾选后只剩 3 条首测、取消勾选回到 5 条。
★ 教训：验证要走**真实渲染路径**，自己造的 DOM 容器可能不满足代码的选择器前提。

### ⚠️ DUT 明细按桶归组：用 bucketKey 相等，不能用字符串前缀（2026-10-04 修）

症状：良率表切到**按周**或**按小时**，展开某段 → DUT 明细是空的（按天/按月正常）。

根因：`renderDutRows` 原来用 `started.startsWith(bucket)` 把场次归到时间桶。
按天/按月时 bucket 恰好是 started 的前缀（`2026-09-28` / `2026-09`），能对上；
但：
- 周：bucket 是 `2026-W40`（ISO 周号），根本不是 started 的前缀 → 0 行
- 时：bucket 是 `2026-09-28 00:00`，而 started 是 `...00:01:46`，分钟不同 → 0 行

修法：前端加 `bucketKey(started, by)`，和后端 `test_history._bucket_of` **一一对应**
（含 ISO 周号算法），用"算出的 key 相等"归组，不再用前缀。四种粒度实测
一致（day/week/month JS 桶 == 后端桶；hour 单日逐条核对首测时间也精确对应）。

★ 注意 trend 用 `first_only`（只首测），DUT 明细用全部场次，所以 runs 里的
桶集合比 trend 多（含复测），但 trend 列出的每个桶在 runs 里都有对应首测，
展开一定有数据。

## 踩过的环境坑

- **PowerShell 变量名不区分大小写**，且 `$host` `$home` `$pwd` `$args` `$input` 是只读保留变量
- PowerShell 函数里的 `Write-Output` 会混进返回值
- `[Text.Encoding]::UTF8` 写文件带 BOM，Python `json.load` 会报错；
  用 `New-Object System.Text.UTF8Encoding($false)`；读 CSV 一律 `encoding='utf-8-sig'`
- .NET 正则 `.` 默认不跨行，扒压缩 JS 用 `[\s\S]`
- OneDrive 同步中的文件会被锁，读前先复制副本
- HTTP 200 ≠ 成功：Cloudflare Access 对未认证请求返回 200 + 登录页，要检查响应内容

## 关键正则

```python
RE_RADIO = re.compile(r"\bEA8[A-Z]\d{6}\b")        # 整机
RE_ANT   = re.compile(r"\bDS4W[A-Z0-9]{6}\b")      # 天线
RE_FU    = re.compile(r"\bF98BX[A-Z0-9]{5}\b")     # 滤波单元
RE_BR    = re.compile(r"\bBR\s*(\d{1,2})\b", re.I)
RE_TPC   = re.compile(r"^(DVT:)?5\.2\.(\d+)\.(\d+)\.(\d+)$")
RE_MHZ   = re.compile(r"([\d.]+)\s*MH", re.I)
```

## 已放弃的路线（别再试）

- **RTD 的编程接口**：没有干净的取数接口。它是 Plotly Dash 应用，所有数据
  都从一个 `POST /app/dashboard/_dash-update-component` 进出，请求体是 Dash
  内部格式（带回调 id 和依赖图），照抄脆弱且易随版本失效。
- **MCP 服务器**：ARM 允许清单 10 个注册表全查过，没有 MI 或 RTD 的 MCP。
- **EriDoc MCP**：需要 Docker，本机没装，此线暂停。

## RTD（Realtime Test Data）—— 已实测可访问，之前"需申请权限"的记录是错的

`https://jupyter.radiounits-realtimetestdata.ericsson.net/app/dashboard/...`

主机名里的 `jupyter` 是历史遗留，**实际是 Plotly Dash 应用，与 Jupyter 无关**。

```
门禁   Cloudflare Access -> 跳转 login.microsoftonline.com 单点登录
       命令行拿不到 CF_Authorization Cookie，curl/Kerberos 一律被挡
可行   用 Playwright 起有界面 Chrome + 持久化 profile，人工登录一次即可复用
       profile 存在 ~/.kiro/browser-profiles/ericsson-cf（含凭据，注意）
坑     curl 直连报 CRYPT_E_REVOCATION_OFFLINE，是证书吊销查询要走代理，
       加 --ssl-no-revoke 即可，不是证书坏了
```

技术栈实测：Dash 2.18.2 + React 18.2 + **Plotly.js**（SVG 渲染，canvas 0 / svg 25），
无 WebSocket。组件库 `dash_bootstrap_components` 1.4.1 / `dash_mantine_components` 2.0.0
/ `dash_table` / `dash_daq`。固定签名路径 `_dash-layout` `_dash-dependencies`
`_dash-update-component`。

URL 里的 `headers_b64` 是**zlib 压缩 + base64 的选中行 JSON**（以 `eJx` 开头即 zlib），
本地就能解开，不用登录。

### ★ RTD 与 MI 的数据粒度差异（决定了图能画到什么程度）

| | RTD | MI |
|---|---|---|
| Knock Response | 每分支 **200 个迭代点**的 PIM 功率曲线 | **拿不到迭代维度** |
| knock 类测量项 | 逐次迭代的功率 dB | 聚合成**通过率标量 0~1** |
| 最细粒度 | 逐迭代 | 每 (测量类型, 分支, 频率) 一个统计值 |

所以**不可能用 MI 数据复刻 RTD 的 Knock Response 曲线**。等价做法见下面的 curves 模块。

## 多台同屏曲线（curves.py + /api/curves）—— 实时查 MI

因为 MI 拿不到 knock 迭代维度，改成**横轴 32 个分支、一条线一台整机**，
视觉语言和 RTD 一致（细线叠加 + 纵轴贴着数据 + 极浅网格）。

```
数据源  实时逐台查 MI 的 TestPointSummary（8 路并行）
        ⚠️ 原来读 data/out/pim_measures.csv（analyze.py 产出的 20 MB 文件）。
           那份文件是 2026-09-29 生成的、之后再没更新，所以图上一直画着
           那天的数据。已改成实时查。
接口    GET /api/curves/options?product=&from_date=&to_date=[&serial_numbers=&test_category=]
        GET /api/curves?product=&from_date=&to_date=&kind=&freq_mhz=[&serial_numbers=&max_series=]
前端    static/vendor/plotly.min.js（4.8 MB，离线）
```

### ★ 台数上限是硬约束

MI **没有"批量拿明细"的接口**：批量查回来的是按测试点聚合的统计，分不出
哪行属于哪台机器，画不了"一条线一台"。所以 N 台就是 N 个请求。

| 台数 | 实测耗时 |
|---|---|
| 1 台（探可选项用） | **22.2 秒** |
| 2 台 | **17.3 秒** |
| 8 台（`DEFAULT_MAX_SERIALS`） | 约 1 分钟 |
| 16 台（`MAX_SERIALS_HARD_LIMIT`） | 约 2 分钟 |
| 79 台 | 30~90 分钟 —— 不可用 |

超过上限按台**等距抽样**（不是取前 N 台 —— 取前 N 会只留下序列号相邻、
同批生产的机器），并在返回里 `truncated=true` 明确告诉前端。

`/api/curves/options` 只探 **1 台**就能得出可选项（实测各台的测试点结构
完全一致），所以它不随台数变慢。

### ★ 前端必须手动触发，不能自动加载

原来读 CSV 是几十毫秒，所以页面一打开就自动画。现在一次要一分钟，
自动触发等于页面一打开就卡住。改成两步按钮：
「探测可选项（约 22 秒）」→「取数（约 1 分钟）」。

滑块、频率下拉改动都**不触发查询**，只有点按钮才查。
已用无头浏览器实测确认：页面加载期间 `/api/curves` 请求数为 **0**。

### 返回结构（列式，别改成行式）

横轴 `branches` 一份 + 每台一个纯数值数组 `y`，缺失分支用 `null` 占位
（不能跳过，否则曲线左移画到错误分支）。额外带实时取数的元信息：
`elapsed_s` / `truncated` / `n_requested` / `failed_serials`。
单台失败不拖垮整批（记进 `failed_serials`），因为 MI 偶发超时很常见。

### ★ 实测：瓶颈是 trace 条数，不是点数

页面基线 JS 堆 19.1 MB（Plotly 库本身占大头）。79 台 x 32 分支 = 2,528 点：

| 模式 | trace | 画图 ms | 堆增量 MB | SVG 节点 |
|---|---|---|---|---|
| multi 每台一条 | 79 | 35 | 4.4 | 657 |
| single 合并 1 条 | 2 | 17 | 2.2 | 220 |
| gl WebGL | 79 | 46 | 0.1 | 131 |

推到 6 万点（79 台 x 32 分支 x 24 组）：

| 模式 | trace | 画图 ms | 堆 MB | SVG 节点 |
|---|---|---|---|---|
| single | 1 | **103** | 37.7 | 1,357 |
| multi | 1,896 | 561 | 31.9 | **12,727** |
| gl | 1,896 | 818 | 47.7 | 126 |

结论：
- **SVG 节点数随 trace 数线性增长**，79 trace → 657 节点，1,896 trace → 12,727 节点
- **WebGL 在这里没有优势**（6 万点 818 ms，比 SVG 合并模式慢 8 倍）。
  每条 trace 是一个独立 WebGL 绘制批次，trace 多时初始化开销压倒渲染收益。
  WebGL 的适用场景是**少量 trace、每条海量点**，不是"很多条短曲线"
- 合并成 1 条 trace（用 `null` 断开）是这类"多台短曲线"的最优解

（这些数字是 79 台时代测的。现在上限 16 台，渲染已不是瓶颈，取数才是。）

## ★★ MI 有没有逐次迭代（iteration）数据 —— 实测结论：没有

问题背景：RTD 的 Knock Response 图是「每分支 200 个 knock 迭代点」，
想知道能不能从 MI 的测试 log 里拿到同样粒度。

**答案：拿不到。** 迭代维度在 MI 入库前就被聚合掉了。

验证方法（在全部 8 个测试类别的协议上逐一检查「同一个 Test Point Code
是否在一次测试里重复出现」）：

```
类别 26(9224点) 38(1614) 28(1858/1841) 212(1623/1606) 29(4)
    -> 重复编码 0 个，每个测试点只记一个值
类别 32(204点)
    -> 24 个重复码，但是 Common.SLP(Sleep) / RPower.Off,On(开关机)
       BURN.CLOG(清日志) / BURN.SRLOG(保存RU日志)，值全是 0/1 状态标志
       是烧机流程的步骤重复，不是测量采样的迭代
```

`TestPointPlot(PlotType=1)` 也印证：对单个测试点只返回 2 个点，对应该机
在日期范围内被测的 2 次，时间戳是**测试开始时间**，组内各 1 点、跨度 0 秒。
是「跨测试场次的趋势」，不是「场次内的迭代」。

**要迭代数据只能走 RTD**（它直接从测试台取实时数据）。

## ★ 新解锁：单次测试的全部测试点实测值（含通过的点）

`TestPointSummary` 里通过的测试点 `TestPointValue` 是 `[N/A]`，这是本项目
一直以来的限制。两步钻取链能拿到：

```
FunctionalTestProtocolSearch(SerialNumber, FromDate, ToDate)   必填日期！
    -> 每次测试一行，第 19 列 Link 装着
       {"report":"FunctionalTestProtocol","parameters":{"FunctionalTestKey":"..."}}
FunctionalTestProtocol(FunctionalTestKey)                      只要这 1 个字段
    -> resultSet[0] 表头 18 列
       resultSet[1] 该次测试的每一个测试点 11 列，含 Test Point Value
```

实测成本（单台 EA8E538914，8 次测试，跨 6 个类别 26/38/28/32/212/29）：

```
Search    11.1 秒  8.4 KB
Protocol  0.8~2.7 秒/次   9 KB ~ 4.4 MB   4 ~ 9,224 个测试点
全量估算  79 台 x 约 8 次 = 约 632 次请求，约 15 分钟，约 500 MB
```

**价值**：现在 `pim_measures.csv` 只有多次聚合的 avg/lowest/highest，
协议给的是**单次精确值**，可以做「同一台机器第 1 次 vs 第 2 次测试」的对比，
语义和 RTD 的 drill-down 一致。

### ⚠️ 顺带发现：TestPointSummary 的 Lowest/Highest 不是真实极值

因为查询时 `IncludeOutliers="No"`，离群值被剔除了。拿协议的单次原始值比对：
**6,912 个点里 1,176 个（17%）落在 TestPointSummary 给的区间之外**。
要真实极值得填 `IncludeOutliers="Yes"`。

### ⚠️ 枚举值能扒到（之前记错了）

旧记录说"枚举值拿不到（无自动完成端点）"是错的。枚举值就在报表页面 HTML 的
`<option>` 里，和 `filter-id`/`filter-type` 同一处。`TestPointPlot` 的
`PlotType` 只能填数字 `1/2/3`，填 'Histogram' 这类名字会静默返回 data=null。

## ★★ RTD 取数协议（已实测打通，纯 HTTP）

架构分工（用户定的）：

```
MI   -> FPY / 良率 / 趋势 / 跨机队统计        已实现
RTD  -> 单台单次测试的逐迭代 200 点曲线       本节
```

### 认证

```
Cookie  CF_AppSession / CF_Authorization / user_settings
有效期  CF_Authorization 实测 10.9 小时（会话级的 user_settings 关浏览器失效）
刷新    过期后用 Playwright 起有界面 Chrome + 持久化 profile 走一次单点登录
        profile: ~/.kiro/browser-profiles/ericsson-cf
```

### 唯一接口

```
POST https://jupyter.radiounits-realtimetestdata.ericsson.net/app/dashboard/_dash-update-component
Content-Type: application/json
Referer:      .../app/dashboard/drill_down
```

Dash 回调格式，请求体 = `{output, outputs, inputs, state, changedPropIds,
parsedChangedPropsIds}`。**请求体里没有会话绑定的哈希，可以直接复现。**

### 三步取数流程（顺序不能换）

```
1. 会话列表  ->  拿 (device_id, start_time)
   output          ..layout_test-sessions_table_element.data..
   关键 input      layout_test_sessions_start_end_date_store.data =
                   {"start_date_time":"2026-09-30T00:00:00",
                    "end_date_time":"2026-09-30T23:59:59.999999"}
                   layout_drilldown_test_sessions_fam_prod_store.data = ["ALL"]
   实测            2026-09-30 全天 20,723 条，1.3 MB，5.9 秒
   返回字段        id start_time_short start_time_local factory_name
                   test_station_id device_id prod_slash rstate Testcat
                   testpoints_count auxpoints_count sbt_skipped_total
                   first_fail Status test_count testtype manual legacy
   ★ id 是 {"device_id":"...","start_time":"2026-09-30T01:42:29.552000"}
     start_time 带毫秒，MI 的 TestStartTime 没有毫秒，不能直接拼

2. 按会话查可选参数  ->  必须做，不能沿用上一台的值
   output  layout-pim_knock-frequency-dropdown.options
           layout-pim_knock-branch-dropdown.options
           layout-pim_knock-iteration-dropdown.options
   把 layout_drill_down_selected_sessions_store.data 设成 [该会话整行]
   实测  频率 6 个 [1736000000.0 1776000000.0 1856000000.0 1896000000.0
                    2502500000.0 2562500000.0]   ← 单位 Hz，和 MI 的频率一致
         分支 33 个 ['ALL', 0..31]
         迭代 21 个 ['ALL', 1..20]
         默认 value 全是 None，必须显式指定
   ⚠️ 沿用别的机器的频率会返回 0 条 trace（踩过）
   ⚠️ branch/iteration 传 'ALL' 会被忽略，传纯数字列表

3. 取曲线
   output  ..layout-pim_knock-overlay-graph.figure..
           ..layout-pim_knock-sequential-graph.figure..
           ..layout-pim_knock-3d_panel1-graph.figure..
           ..layout-pim_knock-3d_panel2-graph.figure..
   input   layout-pim_knock-search-button.n_clicks = 1
   state   layout-pim_knock-radio_button.value          = "sequential"
           layout-pim_knock-frequency-dropdown.value    = 1896000000
           layout-pim_knock-branch-dropdown.value       = [0..31]
           layout-pim_knock-branch_3d_panel2-dropdown.value = []
           layout-pim_knock-iteration-dropdown.value    = [1..20]
           layout-pim-testsession-dropdown.value        = 0
           layout_drill_down_selected_sessions_store.data = [该会话整行]
   实测    32 分支全要 230 KB / 1-2 秒；只要 4 个分支 35.8 KB
   返回    response["layout-pim_knock-sequential-graph"]["figure"]["data"]
           每条 trace: name="EA8E573017, TC: 5, Br: 0", x/y 各 200 点
           x = Knock iteration（0.0, 0.2, 0.5446, ... 小数，不是整数）
           y = PIM Power [dB]
```

### 粒度真相

**迭代是 20 次，采样 200 点**（每次迭代约 10 个采样）。`iteration-dropdown`
的选项是 1..20，而每条曲线有 200 个点，x 轴值是小数。

### 和 MI 的对应关系

| | MI | RTD |
|---|---|---|
| 频率 | 1736/1776/1856/1896/2502.5/2562.5 MHz | **完全相同**（单位 Hz） |
| 分支 | `rfPort` 0..31 | `Br` 0..31 |
| 测试次数 | `TestCount` | `TC` |
| 时间戳 | `TestStartTime` 秒级 | `start_time` **毫秒级** |
| knock 数据 | 通过率标量 0~1 | **200 点原始功率曲线** |

频率和分支定义一致，可以安全关联。但**时间戳精度不同**，关联要按
(device_id, 日期, TestCount) 而不是精确时间。

### 实现：rtd_client.py（模板驱动）

```
rtd_client.py          RtdClient：sessions() / knock_options() / knock_curves()
refresh_cookies.py     起浏览器登录 + 抓请求模板（Cookie 过期或 RTD 升级后跑）
data/rtd/cookies.json  Cookie
data/rtd/templates.json 请求模板（sessions / freq_options / branch_options / iter_options）
tests/test_rtd_client.py  16 项，全部用假 HTTP 层，不碰网络
```

**为什么模板驱动而不是手工拼请求** —— Dash 的 inputs/state 分布是框架实现
细节，手工重建踩了三个静默失败的坑：

```
outputs 填成空列表                     -> 500（没有任何有用信息）
会话 store 放进 inputs 而不是 state    -> 500
沿用别的机器的频率                     -> 200 但 0 条曲线
```

正确的分布（照抄真实抓包）：

```
sessions      inputs  = fam_prod_store / store-table-type /
                        input_device_id_store / start_end_date_store
              state   = layout_test-sessions_table_element.data = []
*_options     inputs  = layout-pim-testsession-dropdown.value = 0
              state   = layout_drill_down_selected_sessions_store.data = [会话行]
                        （加上该下拉自己的 options = []）
curves        inputs  = layout-pim_knock-search-button.n_clicks = 1
              state   = radio_button / frequency / branch /
                        branch_3d_panel2 / iteration / testsession / 会话 store
```

`curves` 模板是**内置在代码里**的：它要点"查询"按钮才触发，而按钮在参数
选全之前是 disabled（抓包里能看到反复设置 `search-button.disabled`），
自动抓取不稳定。结构来自真实抓包并实测验证。

### 实测端到端（2026-09-30 的 KRD 901 330/11）

```
第1步 会话列表           30 条同型号 PIM 会话     6.0 秒
第2步 可选参数           频率 6 个 / 分支 32 / 迭代 20     1.4 秒
第3步 曲线（32 分支全要） 32 条 x 200 点 = 6400 点    1.0 秒
      不指定分支自动全要                          1.9 秒
```

### ⚠️ x 轴的含义尚未确定

轴标题是 `Knock iteration`，但实测 200 个点的 x 值范围是 **0.0 ~ 61.38**
（前 5 个 0.0 / 0.2 / 0.5674 / 0.9278 / 1.3038），而迭代下拉只有 20 个选项。
所以 x **不是迭代序号**，可能是累计敲击次数或时间（秒），**没有验证，
不要在界面上标成"第 N 次迭代"**。

## RTD 单次测试明细（第 9 块）—— 已接入前端

数据分工（架构性，不是偷懒）：

    MI   存判定结果 —— 一个 knock 测试点在 MI 里就是 Limit=EQU/1、Value=1 一个标量
    RTD  存判定依据 —— 同一个测试点背后 200 个采样点的完整曲线

接口（都在 api.py）：

    GET /api/sessions?date=&product=&testcat=&status=&sn=
    GET /api/session/options?date=&device_id=&start_time=
    GET /api/session/curves?date=&device_id=&start_time=&freq_mhz=&branches=&iterations=

三条设计约束，改的时候别破坏：

1. **对外只认 device_id + start_time 两个字符串。** RTD 的会话字典有 18 个字段，
   其中 id 是 {"device_id":..,"start_time":..} 的嵌套结构且带毫秒。后端用
   `_rtd_sess_cache` 按 "device_id|start_time" 缓存原始字典，缓存没有就把那天
   全量拉一次。别把 RTD 的内部结构放进接口契约。
2. **freq_mhz 必填，不给默认值。** 沿用别的机器的频率会静默返回 0 条曲线
   （HTTP 200、不报错、图是空的）。对外一律 MHz，接口层换算成 Hz。
3. **x_title 照搬 RTD 原文。** 它的值实测是 0.0~61.38 的浮点，不是 1..20 的
   迭代序号，真实含义尚未确认。界面上不要标成「第 N 次迭代」。

实测耗时（2026-09-30，KRD 901 330/11）：

    列会话（筛产品）  7.1 秒  19 次       不筛产品 8.2 秒 1269 次
    查可选参数        4.1 秒  6 频率/32 分支/20 迭代
    取曲线            2.0~3.0 秒  129 KB  32 条 x 200 点 = 6400 点
    前端画图          79 ms   合并成 1 条 trace，JS 堆 19.5 MB

★ **不是每次测试都有 knock 曲线。** 实测某天 1269 次 RADIO_PIM 里只有 19 次是
  目标产品，其余（如 KRC 163 457/11）频率列表是空的。前端在频率为 0 时禁用
  「取曲线」并给出原因，产品号默认跟随上方查询条件。

## ★★ 数据来源：只有「实时查」一条路（2026-10-03 改造）

**所有数据一律现查现用，不存在任何本地副本。** 这是用户明确要求的。

改造前有两处在读"下载后的数据"，都已移除：

| 原来 | 问题（实测） | 现在 |
|---|---|---|
| `PIM_SOURCE` 开关，默认 `cache` 读 `snapshot/*.json.gz` | 忘了设开关就悄悄读旧快照，界面正常不报错 | 整条快照路径删除，没有开关 |
| `MiClient` 的文件缓存 `data/cache/*.json.gz`，**永不过期** | 算出 4 天前的良率；界面「刷新」按钮清不掉它 | 删除，每次 query 都真发请求 |

**最硬的证据**：删缓存前查 FPY 得 457 场 / 0.2 秒，删后得 **467 场 / 55.8 秒**。
多出的 10 个场次就是那 4 天产线新测的。

已删除的东西（别找了，也别加回来）：

```
data_source.py   CacheSource / Snapshot / QueryKey / build_manifest / MANIFEST_NAME
mi_client.py     cache_key / cache_dir / _cache_path / _read_cache / _write_cache
                 以及所有方法的 force 参数（没有缓存就不存在"忽略缓存"）
api.py           SOURCE_MODE / LIVE_MODE / SNAPSHOT_DIR / PIM_SNAPSHOT_DIR
文件             export_snapshot.py（known_serials() 搬到了 data_source.py）
```

契约锁在 `tests/test_live_only.py` 和 `tests/test_no_cache.py`。

### ★★ 整机范围也实时列（2026-10-04 改造，别回退成固定清单）

**"全部整机"不再是固定的 80 台清单，而是按 (产品, 日期) 实时问 MI。**

起因：`data/serials.json` 是 9 月从一份 Comba 追踪表 Excel 提取的 80 台。
查询"留空=全部"时退回它，于是不管产线新产多少机器，良率永远只按这 80 台算。
实测 2026-09-01~10-04 同产品：固定清单 80 台，**实时列出 134 台**，
固定清单漏掉 56 台（42%）产线新机 —— 它们的良率根本没被算进去。

MI 没有"直接列序列号"的报表，但 `UnitProductStructure` 的明细表 `Data1`
每行有 `ParentSN => ChildSN` 列，`=>` 左边就是整机序列号。按日期+产品查
这个报表就能列出该时段真实在产的全部整机。

```
mi_client.serials_from_unit_structure(obj)   从 Data1 抽整机序列号（去重）
mi_client.MiClient.serials_in_range(...)      按日期+产品列整机
data_source.MiSource.serials_for(...)         上面的 + 缓存（约 55 秒一次）
```

接法（别改回固定清单）：
- `_filtered_runs`（良率/趋势/列表的枢纽）：用户没填序列号、没选供应商时，
  调 `serials_for` 实时列整机，再 `all_test_runs(serials=这批)` 拉历史
- `/api/curves*`：留空时同样 `serials_for`
- `_all_structures`（供应商台数、下层件追踪）：整机范围用 `serials_for`
  （最近半年），不再用固定清单；`_part_index` 也改成复用 `_all_structures`
- 前端 `currentParams()`：留空就**真留空**，不再塞 `OPTS.serials`

`MiSource.all_test_runs(serials=None)` 现在按**整机集合**分键缓存
（`_cache_sns`）：换了查询范围（不同产品/日期 → 不同整机集合）会重拉，
否则换了范围还用上一批历史，良率就错了。

固定清单 `data/serials.json` **降级成纯候选**：只用于页面初始化填下拉提示
（初始化是轻接口，不能像实时列整机那样卡 55 秒）。界面说清"留空=全部在产"。

### ★ 测试类别加 38 + 顺带修复 fCat.value 读取 bug（2026-10-08）

**用户要求**：测试类别筛选的复选框组加 38（辅助/诊断测试）。

**顺带发现的 bug**：`$('fCat').value` 对一个 `<div>` 永远是 `undefined`
（div 没有 value 属性），`parseList(undefined)` 返回 `[]` → 后端收到的
`category` 参数永远是空字符串 → **不筛选,返回所有类别**。

意思是：**原来界面上勾 28 和 212 完全没生效**，查询返回的是所有类别的
数据。界面显示和实际行为是脱节的。

**修法**：
1. HTML 加第 3 个 checkbox `value="38"`（默认不勾 —— 38 是辅助类别不是
   PIM，默认勾会污染 FPY 良率首测的分母）
2. 加 helper `readCheckedValues(id)` 遍历 `input[type=checkbox]:checked`
   收集 value，用逗号拼
3. 替换 2 处真正**读** fCat 的代码（`currentParams()` 和 `fTrendBy` 的
   change handler）
4. 删掉 init 里对 `$('fCat').value` 的**写入**（dead code，没在任何地方
   被读取过，HTML 的 `checked` 属性已经决定了默认）

**保留不动**：`PIM_CATS = new Set(['28', '212'])`（DUT 明细「查看」按钮
的硬编码）—— 38 不是 PIM，不给曲线按钮，和之前"非 PIM 行不给查看"的
决定一致。38 行会显示 `—` 占位（title 说明"该类别没有 Knock 曲线"）。

**契约锁**：playwright 验证 10 项：
- 复选框 3 个 / value 包含 28/212/38
- 默认 28 勾、212 勾、38 不勾
- 默认查询 URL 带 `category=28,212`
- 只勾 38 → `category=38`
- 全不勾 → URL 不带 category 参数
- 28+212 → `category=28,212`
- 0 JS 报错

★ 这是**"修 bug 时的边界"** 的实例：用户只说"加 38"，但 38 加了之后
不能真起作用（原来的筛选就是坏的），所以 bug 必须一起修。如果不修，
用户加了 38 却发现"数据没变"，又要来找我。**用户要求的功能必需的前置
修复**算在"用户要的"范围内，不算"自作主张加功能"。

### ★ "按整机反查"的按钮用 disabled 而不是切"关闭"（2026-10-08 傍晚）

用户要求：点"查看测试结果"后按钮**灰掉**（disabled），**不显示"关闭"**；
重新选 SN（整机/子件类型/子件 SN）**自动复原**按钮 + 清空旧卡片。

实现：`viewByParent` 不再复用 `openPartCurve` 的切换 UX，自己做：
- 点击：`btn.disabled=true` + 查 /api/history/runs + addCurveCard
- 失败：`btn.disabled=false` 让用户能重试
- 换整机/子件类型/子件 SN 下拉：`resetParentView()` 复原按钮 + `Plotly.purge`
  旧卡片 + 清空 `#parentResult`

`openPartCurve` 回滚到原来的硬编码"查看"/"关闭"，让时间轴那边的按钮
保持原有 UX（时间轴项每条装配是独立"证据"，可以多台叠加对比，用切换
更合适；而"按整机反查"是单一入口，灰掉更干脆）。

★ 两种 UX 并存不冲突 —— "切换"适合**多条同屏**（时间轴），
"灰掉"适合**单路径取数**（整机反查）。按场景选，不要图统一瞎套同一个。

### ⚠️ 诊断教训：验证手段本身可能是错的（2026-10-08 傍晚）

用户反馈"件视角点查看没图像、整机视角有"。playwright 反复测件视角：
cardPlot 容器有、宽 1482 高 360、display block、visibility visible，
但 `_fullData=false` 且 0 个 SVG。hint 显示"这个组合没有曲线数据"。

真相：**`drawCardCurves` 判空用的是 `d.series`，但 playwright mock 用的是
`d.traces`** —— mock 字段名错了。真实 `/api/session/curves` 返回
`{x, series: [{name, branch, test_count, y}, ...], n_series, n_points,
x_title, y_title, device_id, start_time, product, testcat, status,
freq_hz, freq_mhz, mode}`。

两边（件视角 + 整机视角）走同一个 `cardLoadCurves → drawCardCurves`，
所以**两边都没画 SVG**，不是件视角独有的 bug。前面"整机视角有图"是**错觉**
—— 前次 playwright 只检查了 `.curve-card` 的**存在**，没检查**内部 SVG**，
假阳性蒙混过了。

修正 mock 后 12/12 全过（件/整机视角各 10 个 SVG）。

★ 这是 dev-discipline.md 的"验证手段本身也要可信"的实例。用户的描述
（"件视角没图，整机视角有"）可能来自浏览器缓存状态或真实数据差异，不等于
代码里真有件视角独有的 bug。**诊断时必须用机器事实（SVG 数量、_fullData
标志）而不是口头描述反推哪里出错。**

★ 验证脚本的字段名也要**和真实后端对齐**。假数据的字段名错了，看起来功能
"坏了"或"好了"都可能是假象。写 mock 前先看真实返回长啥样（本例应该先 grep
`"series"` 看 `drawCardCurves` 读的是什么字段）。

### ★ 下层件追踪：加"按整机反查"视角（2026-10-08 傍晚）

原来下层件追踪是**件视角**：选一个天线/滤波器 SN → 看它装过哪些整机。
用户要加**整机视角**：选一台整机 SN → 看它装过哪些件 → 看该整机的测试结果。
两个视角并存，共用同一份 _ups_cache 数据。

**后端**：`/api/parts` 响应里加 `by_parent` 字段
```
{antenna: [...], filter_unit: [...], by_parent: {
    parent_sn: {antenna: [{child_sn, child_product}, ...],
                filter_unit: [...]}
}}
```
一次响应同时支持两个视角，前端不用额外请求。

**前端**：tilePart 下方加新的 inline-form（整机 SN 下拉 + 子件类型下拉 +
子件 SN 下拉 + 查看按钮）。整机或子件类型 change → 级联更新子件下拉。
查看按钮 **复用 `openPartCurve`**（加了 labels 参数支持自定义文字），
在 `#parentResult` 容器里展开 RTD 曲线。

**契约锁**：
- `tests/test_api.py::test_parts_返回by_parent反向索引`（后端）
- playwright 13 项（前端：整机下拉、级联子件、切换类型、查看/折叠）

★ 决策：两个视角用**同一个 /api/parts 响应**，不新加接口。by_parent 是从
同一份 _ups_cache 构建的（零额外网络成本），所以合并到现有接口响应里。

### ★ 下层件追踪：读已缓存的 UPS，不自己拉数据（2026-10-08）

**问题**:原来 `/api/parts` 走 `_all_structures()`（10+ 分钟）阻塞页面
初始化,用户以为"模块消失"。中间版本改用 `_ups(None, 183天)`,又踩
"product=None 必填组爆炸"的坑（档案前面记过,>20 分钟不返回）。

**最终做法**:/api/parts 不发任何 MI 请求,**读 _ups_cache 里已缓存**
的 UPS 响应构建索引:
- **瞬间返回**（18ms）
- 覆盖范围 = 用户已经 doQuery 过的 (产品, 日期) 组合
- 用户还没查过就返回空 dict,tilePart 保持 hidden

### ★ 下层件追踪和查询条件联动（2026-10-08）

**背景**:上面的"读已缓存"方案初版是**累积并集** —— 遍历 _ups_cache 的
全部 values 合并构建索引。用户查过两个不同的 (产品, 日期) 组合后,下拉
里会**同时**出现两组子件,但选项不显示"属于哪个产品",容易混淆。

**用户要求**:下层件追踪**和前面的查询条件联动**。

**实现**:后端 `_part_index` / `/api/parts` / `/api/parts/{sn}` 都加
`(product, from_date, to_date)` 可选参数,两种模式:
- **联动模式**（任一参数传了）:只用 `_ups_cache[(product, from, to)]`
  对应的那一个响应。缓存不命中就返回空（**绝不**调 _ups 去查 MI,避免
  再次撞到 product=None 爆炸坑）
- **累积模式**（三个参数都不传,保留给向后兼容）:遍历全部缓存合并

**前端**:
- `loadParts(ctx)` 接收 `{product, from_date, to_date}`
- `doQuery` 完成后把当前条件传进去
- 全局 `PARTS_CTX` 记录条件,`doPart` 查具体子件时也带上
- **sn 模式下不调 loadParts**（没有产品/时间概念,下层件追踪不显示）
- `loadParts` 当两类子件都空时主动把 tilePart 隐藏（避免给用户一个空壳）

**契约锁**:
- `tests/test_api.py::test_parts_按条件联动_命中缓存` /
  `::test_parts_按条件联动_不命中返回空`（后端）
- playwright 15 项验证联动行为（前端）

**前端配合**（原来的内容）:
- `init()` **不**调 loadParts（原来阻塞 10 分钟）
- `doQuery` 完成后调 loadParts —— 这时 UPS 响应已经被 doRuns 的
  `suppliers_for` 填进缓存,所以 /api/parts 立刻有数据

**代价**:UnitProductStructure.Data1 **没有装入/拆除时间字段**,所以
`installed_at` 和 `removed_at` 恒为 None（前端 `|| '?'` 兜底）。要精确
时间线仍需 SerialNumberStructure 慢路径。时间信息 vs 可用性,选可用性。

**实测**:
- 查 2 周 /api/history/runs:64 秒（正常）
- 之后 /api/parts:**18 ms** 返回天线 43 种 + 滤波器 698 种

契约锁：
- `tests/test_mi_client.py::test_parts_index_从unit_structure_构建反查索引`
- `tests/test_api.py::test_parts_列出下层件` / `test_part_track_*`
- `tests/conftest.py` 的 `_build_live_client` 预触发 `_ups(None, None, None)`
  填 cache，否则所有 /api/parts 测试拿不到数据

### ★ 下层件追踪时间轴里就地展开 RTD 曲线（2026-10-08）

需求：时间轴每条「装过的整机」记录能点开 RTD Knock 曲线，像良率表的
「查看」那样，但**就地展开**在当前位置，不跳到下面的「测试曲线」块。

**挑战**：UPS 走的是**快路径**，span 里**没有时间字段**（installed_at/
removed_at = None），所以**不知道该 parent 整机是哪一天测的**。而 RTD
列会话必须按日期。

**解法**：点击「查看」时现查 `/api/history/runs?sn=parent_sn` 拿这台的
全部测试历史（单传 sn 不走慢路径的 serials_for，实测 1-5 秒），从里面挑
**最新一次 PIM 测试**（cat=28 或 212），拿它的 date/product/test_count/
category 做 `want`，然后调 `addCurveCard` 展开曲线。

**UI**：
- 时间轴 `<li>` 结构加「查看」按钮（`.tl-view`，`margin-left:auto` 靠右）
- 每项内部加 `<div class="tl-curve-slot">` 作卡片插入点
- 点一次展开（按钮变"关闭"）、再点一次折叠（Plotly.purge 清资源）
- `addCurveCard(sn, date, product, want, container?)` 新加第 5 个参数：
  默认 `#sessCards`（原行为不变），下层件追踪传时间轴项的 slot

**契约锁**：playwright 验证 14 项（4 段提示语都删除 / 时间轴每项按钮
/ 点开展开卡片 / 卡片标题正确显示那次测试 / 再点折叠 / 0 JS 报错）。

★ 为什么就地展开而不是跳转：时间轴每条装配是独立的"证据"，用户看一台
不等于不想对比另一台。就地展开让多台曲线可以**同屏对比**（叠加多张），
跳到底部的「测试曲线」块反而要滚上滚下。

★ 删掉的 4 段提示语（用户要求）：
- `index.html` 测试曲线块的 "MI 存判定结果 vs RTD 存判定依据"
- `#sessListHint` 的 "FPY 良率与趋势里展开..."（节点保留，JS 还写状态）
- `catHint` 的 "数据里有类别... 界面只提供 28 和 212"
- `snHint` 的 "逐个列出要查的整机序列号... 已知候选 N 台"


### ⚠️⚠️ `/api/history/runs` 绝不能走 SerialNumberStructure 查装配（卡死根因，踩过）

装配结构报表（`SerialNumberStructure`）**极慢**：实测单台约 4.5 秒、
**60 台 270 秒**（8 路并行也压不下来，是报表本身慢）。

测试结果列表 `/api/history/runs` 原来为了给每行标"天线/滤波器供应商"，
每次查询都同步 `s.structures(这页整机)` 走这个慢路径 —— 于是 runs 必然卡到超时，
表现为前端「查询按钮一直转圈」（trend/yield 没这列所以能返回，只有 runs 卡）。
排查时先后错怪过"并发死锁""_all_structures 全量"，其实都不是，就是这一处
同步查 SerialNumberStructure。

**现在 runs 的供应商走 `s.suppliers_for(product, from_date, to_date)`**
—— 它和 `serials_for` 共享同一个 `UnitProductStructure` 响应（一次请求约 15 秒），
零额外网络成本。所以 `ant_suppliers` / `fu_suppliers` **是公司名列表**（如 `["Comba"]` /
`["Grentech"]`），**不是空数组**。实测 `/api/history/runs` 返回里确实有数据。

★ 2026-10-04 的档案老记录说"两列为空、前端不显示"**早就过时了** —— 代码里
后来又改回有数据的版本。截止 2026-10-08 的事实：
- 后端返回 `ant_suppliers` / `fu_suppliers` 列表（供应商公司名）
- 前端 `renderDutRows` 显示这两列（用户截图可见 AMPHENOL、E201F、Comba 等）
- 要按供应商筛 DUT 明细，可以本地按 ant_suppliers / fu_suppliers 列过滤，
  **不需要**再走 _all_structures 慢路径

要按供应商**分析良率**（跨不同供应商比较良率）仍需查询条件里的供应商筛选下拉。

★ 顺带确认过：测试历史响应的 `SubUnitData` 列**不含子件序列号**（实测只有
`'6'`/`'0'` 这种计数），所以没法从已缓存的历史里零成本算供应商。

### ⚠️⚠️ 供应商筛选也绝不能走 `_all_structures()`（同一类卡死，2026-10-04 修）

症状：**选了天线/滤波器供应商，查询按钮就一直转圈**。和上面 runs 那个坑
同源 —— 都是同步调了逐台查装配结构的那条路。

`_all_structures()` 是两步慢操作叠加，实测：

| 步骤 | 耗时 |
|---|---|
| `serials_for(product=None, 最近183天)` 列整机 | **超过 20 分钟不返回** |
| 再 `s.structures(那一批)` 逐台查结构 | 档案实测 80 台 567 秒 |

★ 第一步本身就不可用。注意 `product=None`：`UnitProductStructure` 的
ProductGroup/Family/Type/Number/Bulk 是**必填组**，不填产品等于让 MI
扫全部产品 183 天，数据量爆炸。

**修法**：供应商改从 `MiSource.suppliers_for(product, from_date, to_date)` 拿
——它和 `serials_for` 共享同一个 `UnitProductStructure` 响应，零额外网络成本。
按供应商反查整机是纯本地字典操作（实测 0.0001 秒）。

```python
sup_map = s.suppliers_for(product=..., from_date=..., to_date=...)
hit = {k for k, v in sup_map.items() if supplier in (v.get(part_type) or [])}
```

实测修复前后（真查 MI，2026-09-20~10-04，KRD 901 330/11）：

| 条件 | 修复前 | 修复后 |
|---|---|---|
| 不选供应商（基线） | 57.7 秒 | 57.7 秒 |
| 滤波器 = Grentech（51 台） | **>20 分钟不返回** | **20.4 秒** |
| 天线 = Comba（56 台） | **>20 分钟不返回** | **18.8 秒** |
| 滤波器 = Chunxing（9 台） | **>20 分钟不返回** | **4.4 秒** |

★ 选了供应商反而**比不选更快**：筛完整机变少，要拉的测试历史也少。
台数可交叉验证：Grentech 51 + Chunxing 9 = 60 = 该范围全部整机。

契约锁在 `tests/test_api_history.py::test_供应商筛选不查装配结构`
和 `::test_供应商筛选按查询范围取数`。

### ⚠️ 改查询条件绝不能自动重查（2026-10-04 修）

症状：**选个供应商下拉，查询按钮立刻变"查询中"**，整页锁死一两分钟 ——
用户根本还没点查询。

根因在前端：`static/app.js` 原来有 4 个控件是 `change -> doQuery` 的
（代码注释写着"改完还要手点一次很反直觉"）：

| 控件 | 原来触发 | 现在代价 |
|---|---|---|
| `fAntSup` / `fFuSup` 供应商下拉 | 整个查询 | 60~120 秒 |
| `fCat` 测试类别（复选框组，靠冒泡） | 整个查询 | 60~120 秒 |
| `fRunsFailed` 只看失败 | 重查 runs | 约 60 秒 |

那个判断在**快照时代成立**（一次查询 0.2 秒）。改成全实时后同一行代码的
代价变了：下拉一选就锁页；想同时设天线+滤波器两个供应商，第一次改动的
60 秒完全白等。和曲线那一块是同一个取舍（那边也从自动加载改成手动按钮）。

**修法**：
- 条件类控件改成 `change -> markDirty()`：只加提示「查询条件已修改，点
  「查询」后生效」+ 按钮加 `.btn-dirty` 光圈，**不发请求**。`doQuery()`
  开头 `clearDirty()`。★ 不自动查就必须显式提示，否则用户以为"改了没反应"。
- 「只看失败」改成**纯本地筛选**（数据已在 `RUNS.rows` 里，带 `passed` 字段），
  不再传后端 `only_failed`，实测勾选后 0 个网络请求、立刻生效。
  ⚠️ 结果被截断（>500 行）时本地筛只能在取回的部分里筛，说明文字要讲清，
  别让人以为看到了全部失败。CSV 导出同步跟着筛，和表格一致。

实测（无头浏览器）：改 3 个条件后发出的查询请求 **0 个**，提示和按钮高亮
都正确；「只看失败」勾选 47 行 → 22 行，**0 个网络请求**。

### ⚠️⚠️ 改了前端用户看不到 —— 浏览器缓存（2026-10-04 修）

症状：上面那个修复做完、服务送出的确实是新 `app.js`，但用户界面上**还是
老行为**（改供应商仍然自动发起查询），看起来像"没修好"。

根因：Starlette 的 `StaticFiles` 只给 `ETag` + `Last-Modified`，
**不给 `Cache-Control`**。实测响应头 `Cache-Control: None`。没有这个头时
浏览器用**启发式缓存**：自己猜一个过期时间（常见做法是取"距上次修改时间"
的 10%），这段时间内连服务器都不问，直接用本地副本。

而 `index.html` 引脚本是 `<script src="/static/app.js">`，**没有版本号**
做缓存失效，所以只能靠响应头解决。

修法（`api.py` 的 `_CachedStatic`）：

| 文件 | Cache-Control | 为什么 |
|---|---|---|
| `index.html`、`app.js`、`eds.css` | `no-cache, must-revalidate` | 每次都要向服务器确认。★ `no-cache` 不是"不缓存"，是"必须重新验证" |
| `static/vendor/`（plotly 4.8 MB） | `public, max-age=604800`（7天） | 只在换版本时变，而且大，每次验证不值得 |

配合已有的 ETag，没变就回 **304**（无正文），开销很小 —— 实测 app.js
72 KB，重新验证只回 304。

实测验证（真浏览器，改文件后**普通**刷新而非 Ctrl+Shift+R）：
```
改之前浏览器拿到: __CACHE_MARK='第一版'
改之后普通刷新拿到: __CACHE_MARK='第二版'   ← 成立，不用强制刷新
```

契约锁在 `tests/test_static_cache.py`（含"没变的文件回 304 不重传"）。

★ 调试前端时如果怀疑"改了没生效"，先看浏览器实际加载的文件内容，
不要只看磁盘上的源文件 —— 这是"读最终生效值"原则的一个实例。

### ⚠️ 还在用慢路径的两处（都不在页面初始化里，但点了会卡）

`_all_structures()` 现在只剩两个调用方，**都是 20 分钟级别**：

| 调用方 | 触发方式 | 现状 |
|---|---|---|
| `/api/suppliers?with_counts=true` | 要显式加参数 | 界面不调（默认走前缀对照表，0.00 秒） |
| `/api/parts`、`/api/parts/{sn}`（第 8 块「下层件追踪」） | 用户点才查 | **未修，点了会卡死** |

下层件追踪要反查"子件装过哪些整机"，而 `UnitProductStructure` 的
`ParentSN => ChildSN` 列本来就有这个信息 —— 和供应商同一条快路径，
应该能同样改掉。**尚未实现，别以为这块是好的。**

### ⚠️ 代价：查询更慢了（接受的取舍）

实测 FPY 良率（留空=全部）：**123 秒**（先列整机 55 秒 + 拉 134 台历史），
改之前是 55 秒（只拉固定 80 台）。换来良率分母 80 → 134，覆盖全部真实在产
整机。用户明确选了"全改、接受变慢"。契约锁在 `tests/test_mi_source.py`
（serials_for 系列）、`tests/test_mi_client.py`（serials_from_unit_structure）、
`tests/test_no_cache.py`（装配结构基于实时列整机）。

### 保留的两份内存缓存（刻意的，带存活时间）

完全不缓存做不到，因为有接口会在**一次请求内**反复用同一份数据：
`/api/history/options` 一个请求里就要用 5 次全部测试历史。

| 缓存 | 存活时间 | 为什么是这个值 |
|---|---|---|
| 测试历史（`MiSource.CACHE_TTL_SECONDS`） | **60 秒** | 拉一次 80 台约 18~55 秒。够一次页面交互复用，又不会看到一分钟前的数据 |
| 装配结构（`api.STRUCT_CACHE_TTL_SECONDS`） | **600 秒** | 拉一次 80 台实测 **567 秒**。给 60 秒等于永远在重查；而装配结构只在产线换件时变 |

两份都能被 `GET /api/refresh` 立即清掉。

### ⚠️ 装配结构必须批量并行

原来是串行 `{sn: s.structure(sn) for sn in ...}`，80 台 × 约 12 秒 ≈ **16 分钟**
（实测页面初始化挂起 5 分钟还没返回）。改用 `s.structures(sns)`（8 路并行）
实测 567 秒。

### ⚠️ `/api/suppliers` 默认不查 MI

它是页面一打开就调的接口。原来从实际装配记录统计，要拉全部装配结构 → 9.5 分钟。
现在默认查本地的序列号前缀对照表（`structure.known_suppliers()`），实测 **0.00 秒**。

顺带修了一个隐性缺陷：原来只列出"实际数据里出现过的"供应商，机器恰好不在
当前查询范围内的供应商就不出现在下拉框，用户没法筛它（实测原来缺 AMPHENOL）。

要台数统计加 `?with_counts=true`，那才会去拉装配结构（9.5 分钟）。
没统计时 `n_parents` 是 `null` 而不是 `0` —— 别让"没统计"看起来像"0 台"。

新增静态表：`structure.SUPPLIER_PART_TYPES`（前缀 → 天线/滤波器）和
`structure.PART_PRODUCTS`。它的键必须和 `SUPPLIER_NAMES` 一一对应（有测试锁着），
漏一个那家供应商就不会出现在下拉框里。

## 启动方式

```powershell
.\start-local.ps1
```

**没有 `PIM_SOURCE` 这个开关了**（它原来默认 `cache`，是"悄悄用旧数据"的根源）。
服务只有实时模式，所以必须跑在有 Windows 域身份的机器上。

验证：`/api/health` 返回 `live:true, source_mode:"mi"`（这两个字段现在恒为真值，
留着是因为启动脚本和前端在读）。已删除的字段：`offline` / `snapshot_dir` /
`snapshot_ready`。

### ⚠️⚠️ 模块导入时绝不能连网

踩过的大坑：`api.py` 原来在模块加载时**同步**调 `discover_products()`
（问 MI 最近半年有哪些产品号），导致 **`import api` 本身要 90 秒以上** ——
服务启动卡住，测试更是每次 reload 模块都卡一次，几十个用例直接跑不完
（表现为"测试卡住"，很难一眼看出是导入的问题）。

改成后台 daemon 线程去问，导入只建对象、不发请求。实测 **90+ 秒 → 1.15 秒**。
代价是产品号下拉要等几十秒才填上（在那之前只有默认值 `KRD 901 330/11`）。

PowerShell 的 `$env:X` 和 Python `subprocess(env=...)` 不等价 —— 实测用 Python
给 uv run 传 env 没生效，用 PowerShell 的 `$env:` 才行。

## MI blob 里有迭代数据，但不在 CurveValues 里

更正之前"MI 没有迭代数据"的结论，准确说法：

| 路径 | 有迭代数据吗 |
|---|---|
| MI 报表接口 | 没有，knock 类只给通过率标量 |
| blob 里的 RTD_TestRecord.json 的 CurveValues | **字段存在但实测全是空数组** |
| blob 里的 DutInternalLogs/pim/rx/rxpwr/log_*.txt | **有，109 个文件** |
| RTD 的 Dash 接口 | 有，200 点，已换算成 dBm |

blob 下载链路：

    1. BlobDownload 报表拿路径（★ TestResult 必填 "[All]"，
       不填报 Snowflake 错误 (10690) Expected descriptor record does not exist）
    2. GET /MIReports/api/ReportExportData/DownloadBlob?blobFilePath=<路径>&source=Web
       走 Kerberos 不需要 Cookie。这个端点不在任何报表清单里，
       是从 /MIReports/bundles/script/report 的 JS 里挖出来的。

rxpwr 日志格式（单位是 **dBbit** 不是 dBm，`-1000` 是"分支未启用"哨兵值）：

    [2026-09-30 14:27:32.287593](+142.065316)  Filter branch 0: 22.539 dBbit
    ... freqkHz: 2562500 / gainTarget: 131.3 / absoluteInbandPower: 26.832

★ 结构和 RTD 对不上，映射关系未确认：
  blob 192 个 Filter branch x 284 点（192 = 32 x 6？）
  RTD   32 个 Br x 200 点
  同名不同义：blob 的 "Filter branch" 和 RTD 的 "Br" 不是一回事。

两条路实测对比（同一次测试的曲线）：

    RTD   端到端 1.0 秒（清单已缓存）  230 KB   内存 ~2 MB   凭据 11 小时过期
    blob  端到端 14.7 秒               4.94 MB  内存 9.8 MB  Kerberos 不过期
          其中查清单 9.9 秒（比下载还慢 4 倍，批量时按台线性累加）

结论：前端交互用 RTD，离线批量存档用 blob。
