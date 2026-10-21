---
name: svn-slim
description: Shrink a bloated SVN working copy — diagnose .svn size, safely reclaim tens of GB via official svn commands, and self-test update/commit/revert/diff afterwards. SVN 工作副本 .svn 目录瘦身：诊断空间构成、安全回收、清理后自测。Triggers / 用于 ".svn 太大"、"怎么清理 SVN 空间"、"工作副本占满磁盘"、"pristine 几十 GB"、"svn working copy too large"、"reclaim svn disk space"、"cleanup svn pristine"。核心路径 vacuum → revert → exclude → vacuum，禁止直接删 pristine 目录。
---

# svn-slim：SVN 工作副本 .svn 瘦身

## 30 秒上手

```
python svn_slim.py scan   --path "D:\work\myproject"      # 先看构成，不改动
python svn_slim.py clean  --path "D:\work\myproject"      # 回收（零风险那部分）
python svn_slim.py verify --path "D:\work\myproject" --online   # 自测，必做
```

脚本是同目录下的 `svn_slim.py`，**纯 Python 标准库，无任何第三方依赖**，
Python 3.6+ / Windows / Linux / macOS 通用。发给同事只需这一个文件。

环境要求：
- **Python 3.6+**（命令行敲 `python --version` 或 Windows 上 `py -3 --version`）
- **svn 命令行客户端 1.8+**（`svn --version --quiet` 能出版本号即可）
  - Windows：装 TortoiseSVN 时勾选 **command line client tools**；或装 SlikSVN / VisualSVN 命令行版
  - 没有 svn 也能跑 `scan`（只读诊断），但 `clean` / `verify` 需要它

## 先给结论：.svn 里哪部分能回收

SVN 1.7+ 工作副本只有一个顶层 `.svn`，里面 `pristine/` 存了每个受版本控制文件的
**原始副本**（BASE 版本）。三类占用，处理方式完全不同：

| 类型 | 判据 | 能否回收 |
|---|---|---|
| **A 孤儿副本** | `PRISTINE.refcount=0` | ✅ `svn cleanup --vacuum-pristines`，零风险 |
| **C 已删文件残留** | 文件在磁盘上不存在，但 SVN 仍登记为 `normal` | ✅ 需 revert→exclude→vacuum，**要先确认那些文件不要了** |
| **B 在用副本** | 文件存在 | ❌ **必须保留**，删了 diff/revert/commit 全废 |

**最常见的病因**：在 SVN 工作副本里直接按 Delete 删文件，磁盘空间**一点都不会释放**。
SVN 仍认为文件该存在，原始副本完整躺在 pristine 里。删掉几十 GB 文件，可用空间一个字节没变。

## 三种模式

### scan —— 只读诊断，永远先跑这个

输出 `.svn` 总体积、pristine 体积、A/B/C 三类各占多少、已删文件按目录分布 TOP10，
并给出可回收预估和推荐的 clean 命令。**不改动任何东西**。

### clean —— 回收

默认只做 A 类（零风险）。要动 C 类必须显式指定目录：

```
python svn_slim.py clean --path "<wc>" --exclude "doc/BigAssets"
python svn_slim.py clean --path "<wc>" --exclude "a" --exclude "b"
```

内部流程（全程官方命令，不改数据库）：

1. 备份 `wc.db` 到 `_svn_slim_backup`（几十 MB，是整个操作的回滚保险）
2. `svn cleanup --vacuum-pristines` —— 先拿稳零风险的 A 类
3. 对每个 `--exclude` 目录：
   - 把目录里**未版本化文件**先备份出来（加 `--keep-unversioned` 可跳过）
   - `svn revert -R <dir>` —— 从本地 pristine 把缺失文件写回磁盘，**不走网络**
   - `svn update --set-depth exclude <dir>` —— 解除引用
   - 再次 `cleanup --vacuum-pristines`
4. 输出前后体积对比 + 恢复命令

### verify —— 清理后自测（必做）

7 项检查，全部 PASS 才算安全：

1. `svn info` 可读
2. `svn status` 可执行
3. **在用原始副本齐全**（抽查 refcount>0 的 pristine 是否都在）
4. `svn diff` 能检出改动（真实追加一行再 diff）
5. `svn revert` 能**字节级还原**该文件
6. 再次 vacuum 无异常（副本已收敛）
7. `--online` 时额外测 `svn status -u` 与服务器连通

任一项 FAIL → 退出码 1，提示先恢复 wc.db 备份。**第 4、5 项是核心**：
diff 有比对基准 + revert 能还原，说明 pristine 地基完好，commit 也就没问题。

自测实现上的三个讲究（都是踩过坑才加的）：
- **只挑状态干净的文件做往返验证**。若文件有本地改动，`revert` 会毁掉别人的未提交修改，
  所以先 `svn status <f>` 确认无输出才用它做小白鼠
- **`svn diff --force`**。不加 `--force` 时，svn 对二进制文件（docx/xlsx/pdf/jpg/suo…）
  只回一句 "Cannot display: file marked as binary"，会被误判成 FAIL
- **优先挑文本文件**（扩展名白名单），二进制文件作为兜底副本也没问题
- **没有受版本控制文件的副本 → `[SKIP]` 而非 FAIL**（例如只有一堆待 add 文件的空副本）

## 关键陷阱（不知道会白忙一场）

### 1. exclude 对"缺失文件"静默失效

目录里的文件在磁盘上不存在时（`svn status` 显示 `!`），直接执行

```
svn update --set-depth exclude <dir>
```

会 **rc=0、无任何输出、数据库纹丝不动**。必须先 `svn revert -R <dir>` 把文件写回磁盘，
exclude 才生效。脚本已按这个顺序实现。

### 2. 显式指定路径 update 的副作用

`svn update <某目录>` 会把该目录深度重置为 infinity，**顺带把服务器上该目录下本地
从未检出的新内容一起拉回来**。实测教训：跑了一次 `svn update doc`，凭空多拉回 1.26 GB。
所以验证恢复行为时别拿真实工作副本的父目录做实验。

### 3. 工作副本被锁（E155004）

```
svn: E155004: Working copy '...' is already locked.
```

上次操作中断留下的陈锁。`clean` 会自动识别该错误，先跑一次普通
`svn cleanup` 解锁，再重试 vacuum，无需人工介入。

### 4. 恢复被排除目录的行为差异

| 操作 | 是否拉回被排除目录 |
|---|---|
| `svn update <被排除目录本身>`（不带任何深度参数） | **会** |
| `svn update --depth/--set-depth infinity <被排除目录>` | **会** |
| `svn update <父目录>` / 右键根目录 SVN Update | **不会**，excluded 被尊重 |
| 右键 → SVN Checkout | **会，而且全量**，最不该点的那个 |

恢复命令：`svn update --set-depth infinity "<目录>"`（服务器上数据毫发无损）。
TortoiseSVN 路径：Repo-browser → 找到目录 → 右键 → Update item to revision → Fully recursive。

## 禁止事项

- **不要直接删 `.svn\pristine` 目录**——网上流传的野路子。会让 diff/revert/commit 全部失效，
  且下次 update 会重新拉回，比原来更糟
- 不要手工改 `wc.db` 的 presence 字段做"数据库手术"
- 不要在副本有未提交改动（`pending_nodes>0`）时贸然做大动作——scan 会提示

## 实测记录（SVN 1.14.5 / 工作副本 format 12）

单个工作副本：

| | 清理前 | 清理后 |
|---|---|---|
| `.svn` | 50.52 GB / 19861 文件 | **8.71 GB / 10852 文件** |
| 磁盘可用 | 192.3 GB | 234.2 GB |

**净释放 41.9 GB**。清理后 `svn info` / `svn status -u` / `svn diff` / `svn revert`
全部正常，服务器 HEAD 可达，工作文件一个没少，verify 自测 7/7 通过。

病因：某资产目录 5608 个文件被人从磁盘直接删掉（没走 `svn delete`），
32 GB 原始副本全留在 pristine 里；另有 7.46 GB 孤儿副本。

批量（25 个工作副本）：`.svn` 合计 **285.47 GB → 213.58 GB，释放 71.89 GB**，
单副本最大一笔释放 24.68 GB，耗时 7 分 30 秒，verify **25/25 PASS**。

## 环境注意（Windows 实测）

- 含中文路径的脚本，落成 `.py` 文件再执行，别用 `python -c` 内联（编码易炸）
- 输出中文建议 `PYTHONIOENCODING=utf-8`（脚本内部已 try/except 处理 stdout 编码）
- `svn cleanup` 对几十 GB 副本可能跑十几分钟，**用后台任务跑**，别卡前台超时
- `revert` 需临时占用与被恢复文件等量的磁盘空间，先确认余量

## 给同事的精简版说明（可直接转发）

> `.svn` 太大，用这个脚本瘦身，只调官方 svn 命令，不动你的文件：
> 1. 装好 Python 3 和 svn 命令行（`svn --version` 有输出就行）
> 2. `python svn_slim.py scan --path "你的项目目录"` —— 先看能回收多少
> 3. `python svn_slim.py clean --path "你的项目目录"` —— 零风险回收
> 4. `python svn_slim.py verify --path "你的项目目录" --online` —— 自测 7 项，全 PASS 就放心
>
> 实测某项目 50.52 GB → 8.71 GB（释放 41.9 GB），自测 7/7 通过，
> 更新 / 提交 / 还原 / 查看版本库全部正常。
> 注意：网上"直接删 pristine 文件夹"的做法会让 revert/diff/commit 全废，别用。
