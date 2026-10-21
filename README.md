# svn-slim

**Shrink a bloated SVN working copy — safely, with a built-in 7-point self-test.**

一个给 SVN 工作副本 `.svn` 目录瘦身的命令行工具。纯 Python 标准库，无第三方依赖，
只调用官方 `svn` 命令，不手改数据库、不删 `pristine`。清理后可自测 `diff` / `revert` / `commit` 是否仍然正常。

---

## 它解决什么

SVN 1.7+ 的工作副本只有一个顶层 `.svn`，其中 `pristine/` 存放每个受版本控制文件的**原始副本（BASE 版本）**。
时间一长，几十 GB 很常见 —— 但里面大部分空间是"死"的。

最常见的病因：**在 SVN 工作副本里直接按 Delete 删文件。** 磁盘上少了文件，空间却一个字节没释放，
因为 SVN 仍认为这些文件应该存在，原始副本完整躺在 pristine 里。

`.svn` 里的占用分三类，处理方式完全不同：

| 类型 | 判据 | 能否回收 |
|---|---|---|
| **A 孤儿副本** | `PRISTINE.refcount = 0` | ✅ `svn cleanup --vacuum-pristines`，零风险 |
| **C 已删文件残留** | 磁盘上不存在，SVN 仍登记为 `normal` | ✅ 需 `revert → exclude → vacuum`，**要先确认那些文件不要了** |
| **B 在用副本** | 文件存在 | ❌ **必须保留**，删了 `diff` / `revert` / `commit` 全废 |

本工具默认**只碰 A 类**。C 类必须由你显式指定目录才会动。

## 安装

只需要一个文件：`svn_slim.py`。下载下来，放到任意目录即可运行。

环境要求：

- **Python 3.6+**（`python --version` 能看到版本号）
- **svn 命令行客户端 1.8+**（`svn --version --quiet` 有输出即可）
  - Windows：装 TortoiseSVN 时勾选 *command line client tools*；或装 SlikSVN / VisualSVN 命令行版
  - 没有 svn 也能跑 `scan`（纯只读诊断），但 `clean` / `verify` 需要它

```bash
curl -O https://raw.githubusercontent.com/<Vamsoul>/svn-slim/main/svn_slim.py
```

## 用法

```bash
# 1. 先诊断，不改动任何东西
python svn_slim.py scan --path "D:\work\myproject"

# 2. 回收（默认只做零风险的孤儿副本）
python svn_slim.py clean --path "D:\work\myproject"

# 3. 自测，7 项全 PASS 才放心
python svn_slim.py verify --path "D:\work\myproject" --online
```

### `scan` —— 只读诊断，永远先跑这个

输出 `.svn` 总体积、pristine 体积、A/B/C 三类各占多少、已删文件按目录分布 TOP10，
并给出可回收预估与推荐的 `clean` 命令。**不改动任何东西。**

### `clean` —— 回收

默认只回收 A 类。要动 C 类必须显式指定目录，可重复：

```bash
python svn_slim.py clean --path "<wc>" --exclude "doc/BigAssets"
python svn_slim.py clean --path "<wc>" --exclude "a" --exclude "b"
```

内部流程（全程官方命令）：

1. 备份 `wc.db` 到 `_svn_slim_backup` —— 整个操作的回滚保险
2. `svn cleanup --vacuum-pristines` —— 先拿稳零风险的 A 类
3. 对每个 `--exclude` 目录：
   - 先备份目录里**未版本化文件**（`--keep-unversioned` 可跳过此步）
   - `svn revert -R <dir>` —— 从本地 pristine 把缺失文件写回磁盘，**不走网络**
   - `svn update --set-depth exclude <dir>` —— 解除引用
   - 再次 `cleanup --vacuum-pristines`
4. 输出前后体积对比 + 恢复命令

### `verify` —— 清理后自测

7 项检查，全部 PASS 才算安全：

1. `svn info` 可读
2. `svn status` 可执行
3. 在用原始副本齐全（抽查 `refcount > 0` 的 pristine 是否都在）
4. `svn diff` 能检出改动（真实追加一行再 diff）
5. `svn revert` 能**字节级还原**该文件
6. 再次 vacuum 无异常（副本已收敛）
7. `--online` 时额外测 `svn status -u` 与服务器连通

任一项 FAIL → 退出码 1，并提示先恢复 `wc.db` 备份。

第 4、5 项是核心：`diff` 有比对基准、`revert` 能还原，说明 pristine 地基完好，`commit` 也就没问题。
自测实现上有三个讲究：只挑**状态干净**的文件做往返验证（避免毁掉别人的未提交修改）；
`svn diff --force`（否则二进制文件会被误判 FAIL）；优先挑文本文件（扩展名白名单）。

## 实测数据

单个工作副本（SVN 1.14.5 / format 12）：

| | 清理前 | 清理后 |
|---|---|---|
| `.svn` | 50.52 GB / 19,861 文件 | **8.71 GB / 10,852 文件** |
| 磁盘可用 | 192.3 GB | 234.2 GB |

**净释放 41.9 GB**，`verify` 7/7 通过，工作文件 10,054 个一个没少。

批量（25 个工作副本，仅零风险的 A 类回收）：

- `.svn` 合计 **285.47 GB → 213.58 GB，释放 71.89 GB**
- 单副本最大一笔：**58.26 → 33.57 GB（释放 24.68 GB）**
- 耗时 7 分 30 秒，25 个副本 `verify` **25/25 PASS**

## 关键陷阱

**1. `exclude` 对"缺失文件"静默失效。** 目录里的文件在磁盘上不存在时（`svn status` 显示 `!`），
直接 `svn update --set-depth exclude <dir>` 会 **rc=0、无输出、数据库纹丝不动**。
必须先 `svn revert -R <dir>` 把文件写回磁盘。脚本已按此顺序实现。

**2. 显式指定路径 update 的副作用。** `svn update <某目录>` 会把该目录深度重置为 infinity，
顺带把服务器上本地从未检出的新内容一起拉回来（实测多拉 1.26 GB）。别拿真实工作副本的父目录做实验。

**3. 工作副本被锁（E155004）。** 上次操作中断留下的陈锁。`clean` 会自动识别，先跑一次普通
`svn cleanup` 解锁再重试 vacuum，无需人工介入。

**4. 恢复被排除目录的行为差异。** 恢复命令是 `svn update --set-depth infinity "<目录>"`，
服务器数据毫发无损。注意 `svn update <父目录>` 和右键根目录 Update **不会**拉回 excluded 目录，
而右键 → SVN Checkout 会**全量拉回**，最不该点的就是它。

## 禁止事项

- ❌ **不要直接删 `.svn/pristine` 目录。** 网上流传的野路子。会让 `diff` / `revert` / `commit` 全部失效，
  且下次 `update` 会重新拉回，比原来更糟。
- ❌ 不要手工改 `wc.db` 的 `presence` 字段做"数据库手术"。
- ❌ 不要在副本有未提交改动（`pending_nodes > 0`）时贸然做大动作 —— `scan` 会提示。

## 环境注意（Windows）

- 含中文路径的脚本，落成 `.py` 文件再执行，别用 `python -c` 内联（编码易炸）
- 输出中文建议设 `PYTHONIOENCODING=utf-8`（脚本内部已 try/except 处理 stdout 编码）
- `svn cleanup` 对几十 GB 副本可能跑十几分钟，建议后台执行
- `revert` 需临时占用与被恢复文件等量的磁盘空间，先确认余量

## License

MIT
