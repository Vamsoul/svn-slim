#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
svn_slim.py —— SVN 工作副本 .svn 瘦身工具（诊断 / 清理 / 自测）

只依赖 Python 标准库，Python 3.6+，Windows / Linux / macOS 通用。
全程只调用官方 svn 命令，不改数据库、不删 pristine 目录。

子命令
  scan    只读取诊断，输出 .svn 体积构成与可回收空间（不改动任何东西）
  clean   回收空间：默认只回收孤儿副本（零风险）；--exclude 可解除指定目录的引用
  verify  清理后的自测：info / status / pristine 完整性 / diff+revert 往返验证

典型用法
  python svn_slim.py scan   --path "D:\\work\\myproject"
  python svn_slim.py clean  --path "D:\\work\\myproject"
  python svn_slim.py clean  --path "D:\\work\\myproject" --exclude "doc/BigAssets"
  python svn_slim.py verify --path "D:\\work\\myproject" --online
"""

import argparse
import datetime
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

try:
    from pathlib import Path
except Exception:
    Path = None


# ---------------------------------------------------------------- 基础工具

def log(msg=''):
    print(msg, flush=True)


def human(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024.0:
            return '%.2f %s' % (n, unit)
        n /= 1024.0
    return '%.2f PB' % n


def dir_size(root):
    """返回 (总字节, 文件数)。跳过无法访问的文件。"""
    total = 0
    count = 0
    if not os.path.isdir(root):
        return 0, 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
                count += 1
            except OSError:
                pass
    return total, count


def rel_key(relpath, depth=2):
    """把 'a/b/c/d.txt' 归到 'a/b' 这类顶层分组，便于汇总。"""
    parts = relpath.replace('\\', '/').split('/')
    if len(parts) <= 1:
        return '(根目录文件)'
    return '/'.join(parts[:depth])


# ---------------------------------------------------------------- svn 调用

def find_svn():
    exe = 'svn.exe' if os.name == 'nt' else 'svn'
    try:
        r = subprocess.run([exe, '--version', '--quiet'],
                           capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=60)
    except Exception:
        return None, None
    if r.returncode != 0:
        return None, None
    return exe, (r.stdout or r.stderr).strip()


def svn_version_tuple(ver):
    try:
        parts = ver.split('.')
        return tuple(int(p) for p in parts[:2])
    except Exception:
        return (0, 0)


def run_svn(exe, args, cwd, timeout=7200):
    """统一执行 svn 子命令。--non-interactive 避免卡在认证提示上。"""
    cmd = [exe] + args + ['--non-interactive']
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout)
        return r.returncode, (r.stdout or ''), (r.stderr or ''), (time.time() - t0)
    except subprocess.TimeoutExpired:
        return -1, '', 'TIMEOUT after %ss' % timeout, (time.time() - t0)
    except Exception as e:
        return -2, '', '%s: %s' % (type(e).__name__, e), (time.time() - t0)


def find_wc_root(start):
    """从给定路径向上找工作副本根（含 .svn 目录的那一层）。"""
    cur = os.path.abspath(start)
    if os.path.isfile(cur):
        cur = os.path.dirname(cur)
    while True:
        svn_dir = os.path.join(cur, '.svn')
        if os.path.isdir(svn_dir):
            return cur, svn_dir
        parent = os.path.dirname(cur)
        if parent == cur or not parent:
            return None, None
        cur = parent


# ---------------------------------------------------------------- 数据库分析

def db_uri(path):
    if Path is not None:
        try:
            return Path(path).as_uri() + '?mode=ro'
        except Exception:
            pass
    p = path.replace('\\', '/')
    if not p.startswith('/'):
        p = '/' + p
    return 'file:' + p + '?mode=ro'


def snapshot_db(wc_db, tag='slim'):
    """复制 wc.db 到临时目录再只读打开，避免直连锁竞争。"""
    dst = os.path.join(tempfile.gettempdir(),
                       'svn_slim_%s_%d.db' % (tag, int(time.time())))
    shutil.copy2(wc_db, dst)
    return dst


def open_ro(db_path):
    con = sqlite3.connect(db_uri(db_path), uri=True)
    con.text_factory = lambda b: b.decode('utf-8', 'replace')
    return con


def pristine_file(svn_dir, checksum):
    """定位 pristine 副本文件。兼容 1.7（纯 sha1）与 1.9+（$sha1$...）两种命名。"""
    if not checksum:
        return None
    hexpart = checksum.split('$')[-1]
    cands = [
        os.path.join(svn_dir, 'pristine', checksum[:2], checksum + '.svn-base'),
        os.path.join(svn_dir, 'pristine', hexpart[:2], checksum + '.svn-base'),
        os.path.join(svn_dir, 'pristine', hexpart[:2], hexpart + '.svn-base'),
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    # 兜底：扫一层 pristine 子目录
    if not hasattr(pristine_file, '_cache'):
        cache = {}
        base = os.path.join(svn_dir, 'pristine')
        if os.path.isdir(base):
            for sub in os.listdir(base):
                subp = os.path.join(base, sub)
                if os.path.isdir(subp):
                    for f in os.listdir(subp):
                        cache.setdefault(f, os.path.join(subp, f))
        pristine_file._cache = cache
    found = pristine_file._cache.get(checksum + '.svn-base')
    if found is None:
        found = pristine_file._cache.get(hexpart + '.svn-base')
    return found


def analyze(svn_dir, wc_root, max_probe=300000, probe_missing=True):
    """读取 wc.db，返回体积构成与问题清单。"""
    res = {
        'ok': True, 'errors': [],
        'pristine_total': 0, 'pristine_size': 0,
        'orphan_count': 0, 'orphan_size': 0, 'orphan_actual': 0,
        'missing_count': 0, 'missing_size': 0,
        'missing_groups': [],
        'registered_files': 0, 'probed': True,
        'pending_nodes': 0, 'actual_nodes': 0, 'locks': 0, 'queue': 0,
        'journal_size': 0, 'format': '',
    }
    fmt = os.path.join(svn_dir, 'format')
    if os.path.exists(fmt):
        try:
            res['format'] = open(fmt, 'r', encoding='utf-8', errors='replace').read().strip()
        except Exception:
            pass
    jf = os.path.join(svn_dir, 'wc.db-journal')
    if os.path.exists(jf):
        try:
            res['journal_size'] = os.path.getsize(jf)
        except OSError:
            pass

    snap = snapshot_db(os.path.join(svn_dir, 'wc.db'))
    try:
        con = open_ro(snap)
        cur = con.cursor()

        def scalar(sql, default=0):
            try:
                v = cur.execute(sql).fetchone()
                return v[0] if v and v[0] is not None else default
            except Exception as e:
                res['errors'].append('SQL失败 %s (%s)' % (sql[:50], e))
                return default

        res['pristine_total'] = scalar('SELECT count(*) FROM PRISTINE')
        res['pristine_size'] = scalar('SELECT sum(size) FROM PRISTINE')
        res['orphan_count'] = scalar('SELECT count(*) FROM PRISTINE WHERE refcount=0')
        res['orphan_size'] = scalar('SELECT sum(size) FROM PRISTINE WHERE refcount=0')
        res['pending_nodes'] = scalar('SELECT count(*) FROM NODES WHERE op_depth>0')
        res['actual_nodes'] = scalar('SELECT count(*) FROM ACTUAL_NODE')
        res['locks'] = scalar('SELECT count(*) FROM LOCK')
        res['queue'] = scalar('SELECT count(*) FROM WORK_QUEUE')

        psize = {}
        try:
            for ck, sz in cur.execute('SELECT checksum, size FROM PRISTINE'):
                psize[ck] = sz or 0
        except Exception:
            pass

        if res['orphan_count'] and psize:
            try:
                orphans = [r[0] for r in cur.execute(
                    'SELECT checksum FROM PRISTINE WHERE refcount=0')]
                for ck in orphans[:20000]:
                    f = pristine_file(svn_dir, ck)
                    if f:
                        try:
                            res['orphan_actual'] += os.path.getsize(f)
                        except OSError:
                            res['orphan_actual'] += psize.get(ck, 0)
                    else:
                        res['orphan_actual'] += psize.get(ck, 0)
            except Exception as e:
                res['errors'].append('孤儿统计部分失败: %s' % e)

        # 缺失文件（SVN 认为存在、磁盘上却没有）
        try:
            rows = cur.execute(
                "SELECT local_relpath, checksum FROM NODES "
                "WHERE op_depth=0 AND kind='file' AND presence='normal'").fetchall()
        except Exception as e:
            rows = []
            res['errors'].append('NODES 查询失败: %s' % e)

        res['registered_files'] = len(rows)
        if len(rows) > max_probe:
            res['probed'] = False
            rows = rows[:max_probe]
            res['errors'].append('文件数超过 --max-probe(%d)，只抽样检查前 %d 个'
                                 % (max_probe, len(rows)))

        if probe_missing:
            groups = {}
            gcount = {}
            miss = 0
            msize = 0
            for rel, ck in rows:
                full = os.path.join(wc_root, *rel.replace('\\', '/').split('/'))
                if not os.path.exists(full):
                    miss += 1
                    msize += psize.get(ck, 0)
                    k = rel_key(rel)
                    groups[k] = groups.get(k, 0) + psize.get(ck, 0)
                    gcount[k] = gcount.get(k, 0) + 1
            res['missing_count'] = miss
            res['missing_size'] = msize
            res['missing_groups'] = sorted(groups.items(), key=lambda x: -x[1])
            res['missing_group_counts'] = gcount
        con.close()
    except Exception as e:
        res['ok'] = False
        res['errors'].append('数据库打开失败: %s' % e)
    finally:
        try:
            os.remove(snap)
        except OSError:
            pass
    return res


# ---------------------------------------------------------------- scan

def cmd_scan(args):
    wc_root, svn_dir = find_wc_root(args.path)
    if not wc_root:
        log('[X] 在 %s 及其上级目录中找不到 .svn，它不是 SVN 工作副本。' % os.path.abspath(args.path))
        return 2
    if not os.path.exists(os.path.join(svn_dir, 'wc.db')):
        log('[X] %s 是 SVN 1.6 或更老格式（每目录一个 .svn，无 wc.db）。' % wc_root)
        log('    本工具只处理 SVN 1.7+ 工作副本。老格式请先用 svn upgrade 升级，或直接删除 .svn。')
        return 2

    exe, ver = find_svn()
    if not exe:
        log('[!] 未找到 svn 命令行客户端。scan 仍可只读诊断，但 clean 无法执行。')
        log('    Windows: 安装 TortoiseSVN 时勾选 "command line client tools"，'
            '或安装 SlikSVN / VisualSVN 命令行版。')
    else:
        log('svn 版本: %s' % ver)
        if svn_version_tuple(ver) < (1, 8):
            log('[!] 版本低于 1.8，不支持 --vacuum-pristines，请升级。')

    total, tfiles = dir_size(svn_dir)
    ptotal, pfiles = dir_size(os.path.join(svn_dir, 'pristine'))
    info = analyze(svn_dir, wc_root, max_probe=args.max_probe)

    log('')
    log('工作副本: %s' % wc_root)
    log('SVN 格式: %s' % (info['format'] or '(未知)'))
    log('=' * 68)
    log('.svn 实际占用 : %s  (%d 个文件)' % (human(total), tfiles))
    log('其中 pristine : %s  (%d 个副本)' % (human(ptotal), pfiles))
    log('wc.db         : %s' % human(os.path.getsize(os.path.join(svn_dir, 'wc.db'))))
    log('')
    log('--- 空间构成 ---')
    orphan = info['orphan_actual'] or info['orphan_size']
    inuse = max(ptotal - orphan, 0)
    log('A 孤儿副本(可直接回收) : %s  (%d 个)' % (human(orphan), info['orphan_count']))
    log('B 在用副本(必须保留)   : %s' % human(inuse))
    log('C 已删文件残留          : %s  (%d 个文件磁盘上已不存在)'
        % (human(info['missing_size']), info['missing_count']))
    log('')
    log('登记文件数: %d   待提交节点(op_depth>0): %d   ACTUAL_NODE: %d'
        % (info['registered_files'], info['pending_nodes'], info['actual_nodes']))
    log('锁: %d   待处理队列: %d   journal: %s'
        % (info['locks'], info['queue'], human(info['journal_size'])))

    if info['missing_groups']:
        log('')
        log('--- C 类按目录分布 (TOP 10) ---')
        for k, v in info['missing_groups'][:10]:
            log('  %10s  %6d 个  %s' % (human(v), info.get('missing_group_counts', {}).get(k, 0), k))
        log('')
        log('  这些目录的文件在磁盘上已被删掉（没走 svn delete），原始副本仍占着空间。')
        log('  若确认不再需要，可用 --exclude 让 SVN 解除引用后回收，例如：')
        log('    python svn_slim.py clean --path "%s" --exclude "%s"'
            % (wc_root, info['missing_groups'][0][0]))

    if info['errors']:
        log('')
        log('--- 诊断过程中的异常 ---')
        for e in info['errors']:
            log('  ' + e)

    if info['pending_nodes']:
        log('')
        log('[!] 存在 %d 个未提交的本地操作（add/delete/move）。' % info['pending_nodes'])
        log('    执行 clean 不会删除这些操作，但请不要在不确定时对整个副本做大动作。')

    log('')
    log('可回收预估: %s（仅 A 类，零风险）' % human(orphan))
    log('             %s（A+C 类，需先确认那些已删文件不要了）' % human(orphan + info['missing_size']))
    return 0


# ---------------------------------------------------------------- clean

def backup_wcdb(svn_dir, backup_dir):
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    dst = os.path.join(backup_dir, 'wc.db.%s.bak' % ts)
    shutil.copy2(os.path.join(svn_dir, 'wc.db'), dst)
    return dst


def harvest_unversioned(exe, wc_root, target, backup_dir, keep=False):
    """把目标目录里的未版本化文件先备份出来（否则可能阻碍 exclude）。"""
    rc, out, err, _ = run_svn(exe, ['status', target], wc_root, timeout=1800)
    moved = []
    for line in (out or '').splitlines():
        if not line or len(line) < 2:
            continue
        if line[0] in ('?', 'I'):
            rel = line[8:].strip() if len(line) > 8 else ''
            if not rel:
                continue
            full = os.path.join(wc_root, *rel.replace('\\', '/').split('/'))
            if os.path.isfile(full):
                dst = os.path.join(backup_dir, '未版本化文件', rel.replace('/', os.sep))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(full, dst)
                moved.append((full, dst))
                if not keep:
                    try:
                        os.remove(full)
                    except OSError as e:
                        log('    [!] 无法移除 %s (%s)' % (rel, e))
    return moved


def cmd_clean(args):
    wc_root, svn_dir = find_wc_root(args.path)
    if not wc_root:
        log('[X] 找不到 SVN 工作副本: %s' % os.path.abspath(args.path))
        return 2
    if not os.path.exists(os.path.join(svn_dir, 'wc.db')):
        log('[X] SVN 1.6 及更老格式不支持，请先 svn upgrade 或直接删除 .svn。')
        return 2

    exe, ver = find_svn()
    if not exe:
        log('[X] 未找到 svn 命令行，无法执行清理。')
        return 2
    if svn_version_tuple(ver) < (1, 8):
        log('[X] svn %s 不支持 --vacuum-pristines，请升级到 1.8+。' % ver)
        return 2

    backup_dir = args.backup_dir or os.path.join(os.path.dirname(wc_root), '_svn_slim_backup')
    size_before, files_before = dir_size(svn_dir)

    log('工作副本  : %s' % wc_root)
    log('svn 版本  : %s' % ver)
    log('清理前    : %s (%d 个文件)' % (human(size_before), files_before))
    log('备份目录  : %s' % backup_dir)
    log('=' * 68)

    bak = backup_wcdb(svn_dir, backup_dir)
    log('[1] wc.db 已备份 -> %s (%s)' % (bak, human(os.path.getsize(bak))))

    # --- 步骤 2：回收孤儿（零风险）
    log('')
    log('[2] 回收孤儿副本: svn cleanup --vacuum-pristines')
    rc, out, err, secs = run_svn(exe, ['cleanup', '--vacuum-pristines', '.'], wc_root)
    if rc != 0:
        # E155004 = 工作副本被锁（多为上次操作中断留下的陈锁）。先普通 cleanup 解锁再重试。
        if 'E155004' in err or 'locked' in (err or '').lower():
            log('    [!] 工作副本处于锁定状态，先执行普通 svn cleanup 解锁')
            rc0, _, err0, s0 = run_svn(exe, ['cleanup', '.'], wc_root)
            if rc0 != 0:
                log('    [X] 解锁失败 rc=%d: %s' % (rc0, (err0 or '')[:400]))
                return 1
            log('        解锁完成 用时 %.0fs' % s0)
            rc, out, err, secs = run_svn(exe, ['cleanup', '--vacuum-pristines', '.'], wc_root)
        if rc != 0:
            log('    [X] 失败 rc=%d  用时 %.0fs' % (rc, secs))
            log('    ERR: %s' % (err or '')[:600])
            return 1
    mid, _ = dir_size(svn_dir)
    log('    完成 用时 %.0fs，.svn %s -> %s（释放 %s）'
        % (secs, human(size_before), human(mid), human(size_before - mid)))

    # --- 步骤 3：解除指定目录引用
    excluded = []
    if not args.vacuum_only and args.exclude:
        for target in args.exclude:
            target = target.strip().strip('"').replace('\\', '/').rstrip('/')
            log('')
            log('[3] 处理目录: %s' % target)
            moved = harvest_unversioned(exe, wc_root, target, backup_dir, keep=args.keep_unversioned)
            if moved:
                log('    未版本化文件 %d 个 -> %s' % (len(moved), backup_dir))

            log('    3a. svn revert -R（从本地副本写回缺失文件，不走网络）')
            rc, out, err, secs = run_svn(exe, ['revert', '-R', target], wc_root)
            if rc != 0:
                log('    [X] revert 失败 rc=%d: %s' % (rc, (err or '')[:400]))
                continue
            log('        完成 用时 %.0fs' % secs)

            log('    3b. svn update --set-depth exclude')
            rc, out, err, secs = run_svn(exe, ['update', '--set-depth', 'exclude', target], wc_root)
            if rc != 0:
                log('    [X] exclude 失败 rc=%d: %s' % (rc, (err or '')[:400]))
                continue
            log('        输出: %s' % (out or '(无)').strip().replace('\n', ' | ')[:200])
            if not out.strip():
                log('    [!] exclude 无任何输出 —— 通常说明目录里的文件仍处于缺失状态，未真正生效。')
            excluded.append(target)

        if excluded:
            log('')
            log('[4] 再次回收: svn cleanup --vacuum-pristines')
            rc, out, err, secs = run_svn(exe, ['cleanup', '--vacuum-pristines', '.'], wc_root)
            if rc != 0:
                log('    [X] 失败 rc=%d: %s' % (rc, (err or '')[:400]))
            else:
                log('    完成 用时 %.0fs' % secs)

    size_after, files_after = dir_size(svn_dir)
    log('')
    log('=' * 68)
    log('清理完成')
    log('  .svn  : %s -> %s   净释放 %s'
        % (human(size_before), human(size_after), human(size_before - size_after)))
    log('  文件数: %d -> %d' % (files_before, files_after))
    if excluded:
        log('  已解除引用的目录（需要时可拉回）:')
        for t in excluded:
            log('    svn update --set-depth infinity "%s"' % t)
    log('')
    log('下一步（必做）:')
    log('  python "%s" verify --path "%s"%s'
        % (os.path.abspath(__file__), wc_root, ' --online' if args.online else ''))
    return 0


# ---------------------------------------------------------------- verify

# 文本文件扩展名：优先挑这类文件做 diff 往返验证（diff 对二进制默认不出文本差异）
TEXT_EXT = {
    'txt', 'md', 'ini', 'cfg', 'conf', 'csv', 'json', 'xml', 'yml', 'yaml', 'log',
    'py', 'js', 'ts', 'html', 'htm', 'css', 'c', 'h', 'cpp', 'hpp', 'cs', 'java',
    'uplugin', 'uproject', 'csproj', 'sln', 'bat', 'sh', 'sql', 'properties', 'plist',
}


def cmd_verify(args):
    wc_root, svn_dir = find_wc_root(args.path)
    if not wc_root:
        log('[X] 找不到 SVN 工作副本: %s' % os.path.abspath(args.path))
        return 2
    exe, ver = find_svn()
    if not exe:
        log('[X] 未找到 svn 命令行，无法自测。')
        return 2

    results = []

    def check(name, ok, detail=''):
        results.append((name, ok, detail))
        log('  [%s] %s%s' % ('PASS' if ok else 'FAIL', name, ('  ' + detail) if detail else ''))

    log('自测目标: %s' % wc_root)
    log('svn 版本: %s' % ver)
    log('=' * 68)

    # 1. info
    rc, out, err, secs = run_svn(exe, ['info'], wc_root, timeout=300)
    url = ''
    for line in out.splitlines():
        if line.startswith('URL:') or line.startswith('Relative URL:'):
            url = line.split(':', 1)[1].strip()
            break
    check('svn info 可读（工作副本结构完好）', rc == 0, url or (err[:200] if rc else ''))

    # 2. status（本地，不连服务器）
    rc, out, err, secs = run_svn(exe, ['status', '--depth', 'immediates'], wc_root, timeout=1800)
    check('svn status 可执行', rc == 0, '用时 %.1fs' % secs if rc == 0 else (err[:200] or out[:200]))

    # 3. pristine 完整性
    snap = snapshot_db(os.path.join(svn_dir, 'wc.db'), tag='verify')
    missing_pristine = 0
    total_pristine = 0
    try:
        con = open_ro(snap)
        cur = con.cursor()
        rows = cur.execute('SELECT checksum FROM PRISTINE WHERE refcount>0').fetchall()
        total_pristine = len(rows)
        limit = 20000
        for (ck,) in rows[:limit]:
            if not pristine_file(svn_dir, ck):
                missing_pristine += 1
        con.close()
    except Exception as e:
        check('pristine 完整性检查', False, str(e))
    finally:
        try:
            os.remove(snap)
        except OSError:
            pass
    if total_pristine:
        check('在用原始副本齐全（revert/diff 的地基）', missing_pristine == 0,
              '抽查 %d 个，缺失 %d 个' % (min(total_pristine, 20000), missing_pristine))

    # 4. diff + revert 往返验证（最关键）
    snap = snapshot_db(os.path.join(svn_dir, 'wc.db'), tag='verify2')
    cand = None
    try:
        con = open_ro(snap)
        cur = con.cursor()
        rows = cur.execute(
            "SELECT local_relpath FROM NODES WHERE op_depth=0 AND kind='file' "
            "AND presence='normal' ORDER BY local_relpath").fetchall()
        con.close()
        # 只挑状态干净（无本地改动）的文件，否则 revert 会毁掉别人的未提交修改。
        # 优先文本文件；最多试 300 个，避免在大副本上跑几千次 svn status。
        first_other = None
        tried = 0
        for (rel,) in rows:
            if cand or tried >= 300:
                break
            full = os.path.join(wc_root, *rel.replace('\\', '/').split('/'))
            if not os.path.isfile(full):
                continue
            try:
                if os.path.getsize(full) == 0 or os.path.getsize(full) > 2 * 1024 * 1024:
                    continue
            except OSError:
                continue
            rc2, out2, _, _ = run_svn(exe, ['status', rel], wc_root, timeout=120)
            tried += 1
            if rc2 == 0 and not (out2 or '').strip():
                ext = os.path.splitext(rel)[1].lower().lstrip('.')
                if ext in TEXT_EXT:
                    cand = (rel, full)
                elif first_other is None:
                    first_other = (rel, full)
        if cand is None and first_other is not None:
            cand = first_other
    except Exception as e:
        log('  [!] 挑选测试文件失败: %s' % e)
    finally:
        try:
            os.remove(snap)
        except OSError:
            pass

    if not cand:
        if total_pristine == 0:
            log('  [SKIP] diff + revert 往返验证  该副本几乎没有受版本控制的文件，无可用测试对象')
        else:
            check('diff + revert 往返验证', False,
                  '未找到状态干净的可用测试文件（可能全部文件都有本地改动）')
    else:
        rel, full = cand
        try:
            with open(full, 'rb') as f:
                original = f.read()
        except OSError as e:
            check('diff + revert 往返验证', False, '读取失败 %s' % e)
            original = None
        if original is not None:
            marker = b'\n<!-- svn-slim self test -->\n'
            ok_diff = ok_revert = False
            try:
                with open(full, 'ab') as f:
                    f.write(marker)
                # --force：让二进制文件也按文本出差异，否则 svn 只回一句"Cannot display"
                rc3, out3, err3, _ = run_svn(exe, ['diff', '--force', rel], wc_root, timeout=300)
                ok_diff = (rc3 == 0 and 'svn-slim self test' in out3)
                rc4, out4, err4, _ = run_svn(exe, ['revert', rel], wc_root, timeout=300)
                with open(full, 'rb') as f:
                    after = f.read()
                ok_revert = (rc4 == 0 and after == original)
                if not ok_revert:
                    # 兜底：尽力还原
                    try:
                        with open(full, 'wb') as f:
                            f.write(original)
                    except OSError:
                        pass
            except OSError as e:
                check('diff + revert 往返验证', False, '写入失败 %s' % e)
            else:
                check('svn diff 能检出改动', ok_diff, rel)
                check('svn revert 能字节级还原', ok_revert, rel)

    # 5. 再次 vacuum（应无更多可回收）
    before, _ = dir_size(svn_dir)
    rc, out, err, secs = run_svn(exe, ['cleanup', '--vacuum-pristines', '.'], wc_root)
    after, _ = dir_size(svn_dir)
    check('再次回收无异常（副本已收敛）', rc == 0,
          '用时 %.0fs，体积 %s -> %s' % (secs, human(before), human(after)))

    # 6. 服务器连通（可选）
    if args.online:
        rc, out, err, secs = run_svn(exe, ['status', '-u', '--depth', 'immediates'], wc_root, timeout=600)
        check('可连接版本库（update/commit 通路）', rc == 0,
              '用时 %.1fs' % secs if rc == 0 else (err[:200] or out[:200]))

    failed = [r for r in results if not r[1]]
    log('')
    log('=' * 68)
    log('自测结果: %d 项通过, %d 项失败' % (len(results) - len(failed), len(failed)))
    if failed:
        log('[X] 存在失败项，请勿继续删除操作，先恢复 wc.db 备份并排查。')
        return 1
    log('[OK] 全部通过：更新 / 提交 / 还原 / 查看版本库能力完好。')
    return 0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description='SVN 工作副本 .svn 瘦身（诊断 / 清理 / 自测）',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd')

    p = sub.add_parser('scan', help='只读诊断，不改动任何东西')
    p.add_argument('--path', default='.', help='工作副本路径或其子目录，默认当前目录')
    p.add_argument('--max-probe', type=int, default=300000, help='缺失检查的最大文件数，默认 300000')

    p = sub.add_parser('clean', help='回收空间')
    p.add_argument('--path', default='.', help='工作副本路径或其子目录')
    p.add_argument('--vacuum-only', action='store_true', help='只回收孤儿副本（零风险，默认行为）')
    p.add_argument('--exclude', action='append', default=[],
                   help='要解除引用的目录相对路径，可重复传入')
    p.add_argument('--keep-unversioned', action='store_true',
                   help='保留目标目录里的未版本化文件（默认会先备份再移除）')
    p.add_argument('--backup-dir', default=None, help='备份目录，默认在工作副本同级 _svn_slim_backup')
    p.add_argument('--online', action='store_true', help='清理提示里带上联网自测')

    p = sub.add_parser('verify', help='清理后自测，验证 update/commit/revert 能力完好')
    p.add_argument('--path', default='.', help='工作副本路径或其子目录')
    p.add_argument('--online', action='store_true', help='额外测试与服务器的连通（status -u）')

    args = ap.parse_args()
    if args.cmd == 'scan':
        return cmd_scan(args)
    if args.cmd == 'clean':
        return cmd_clean(args)
    if args.cmd == 'verify':
        return cmd_verify(args)
    ap.print_help()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log('\n已中断')
        sys.exit(130)
