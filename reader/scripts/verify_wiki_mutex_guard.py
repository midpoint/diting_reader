#!/usr/bin/env python3
"""静态守卫：编译一条龙进行中，所有「审批 / 编辑」写视图都必须被挡（第十一步）。

为什么需要静态扫描：方向二硬锁要在**十几个**视图开头各接一次守卫，漏一个不会
报错 —— 编译照样跑、页面 200、日志干净，只是那一处能穿透互斥。这正是
「修完一处要问同类还有几处」该变成可回归判据的地方。

R1 写视图（会改 .drafts/ / wiki/ 或参与裁决）必须调 `_compile_running_guard()`。
R2 非写 POST 视图必须写 `# mutex-ok: <理由>` 显式豁免 —— 新增端点若不写，
    守卫会当场报出来，不靠人记清单。
R3 写视图不许用 mutex-ok 绕过（豁免必须窄，且必须写出理由）。
R4 守卫本身必须真的读 `ingest.compile_state()`（不许恒返回放行）。
R5 内建自测：好样本必不报、坏样本必报（守卫自己会响）。

输出 `结果：N 通过 / M 失败` + `ALL PASS`；rc=1 表示不合格。
"""
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
VIEWS = ROOT / "apps" / "wiki" / "views.py"

GUARD_CALL = "_compile_running_guard"

#: 会改 `.drafts/` / `wiki/` 文件、或属于审批裁决过程的调用。
#: ★ 不列「记录痕迹」（marks.add_* / chat_send）：用户定的口径是「暂停的是触发，
#:   不是记录」，划线和批注在编译期间照常落库。
WRITE_CALLS = {
    "wstore.approve", "wstore.discard", "wstore.commit",
    "wstore.save_draft", "wstore.pull_page", "wstore.request_delete",
    "wstore.save_page", "wstore.apply_relink_action",
    "wa.set", "wa.apply_retire_action", "wiki_loop.run", "ingest.set_status",
    # ★ 2026-11-11「可能相关的已入库页」：这两个写口落在 core 层（一个写
    #   `meta/related.json`，一个原地改写 `.drafts/*.md`），视图里看不到
    #   `wstore.*`。白名单漏一个，守卫就会把真写视图判成「非写」，反过来
    #   要求它写 `mutex-ok` 豁免 —— 那等于用豁免掩盖漏守卫，正是 R3 要防的。
    #   ★ 新增写口一律补这里；`self_test` 有一条专门钉住白名单扩展后会响。
    #   ★ 边界（2026-11-13）：只补**视图里直接调用**的写口。经 `wiki_loop.run`
    #     由 agent 工具触发的 core 层写口（如
    #     `ingest.attach_candidates_to_page`）不必补 —— 入口视图
    #     `approval_chat_send` 已因 `wiki_loop.run` 判为写视图并挂了守卫。
    #     实测 post_views()：25 个 POST 视图 / 14 个写视图，该视图
    #     is_write=True、has_guard=True、exempt=False。
    "wiki_related.recommend", "ingest.merge_draft_into",
    # ★ 2026-11-22 一键重编：视图直接调它写 .drafts/ 且花钱（wiki 池）。
    #   不补进白名单，守卫会把真写视图判成「非写」→ 反过来要求 mutex-ok 豁免，
    #   等于用豁免掩盖漏守卫（R3 明令禁止）。
    "wiki_loop._polish_one",
}


def dotted(node):
    """把 Name / Attribute 链拼回 `wstore.approve` 这种点号名。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return (base + "." + node.attr) if base else node.attr
    return ""


def post_views(src: str):
    tree = ast.parse(src)
    out = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        decs = {dotted(d) for d in node.decorator_list}
        if "require_POST" not in decs:
            continue
        calls = {dotted(c.func) for c in ast.walk(node) if isinstance(c, ast.Call)}
        # ★ 往前只收**连续的** `#` / `@` 行，遇到空行或上一个函数体就停：
        #   定长 4 行窗口会把上一个函数的 mutex-ok 注释串进本函数，
        #   把写视图误判成「用豁免绕过」（内建自测当场抓到过）。
        lines = src.splitlines()
        j = node.lineno - 2  # 0-based：def 行的前一行
        while j >= 0 and lines[j].lstrip().startswith(("#", "@")):
            j -= 1
        seg = "\n".join(lines[j + 1:node.end_lineno])
        out.append({
            "name": node.name,
            "line": node.lineno,
            "is_write": bool(calls & WRITE_CALLS),
            "has_guard": GUARD_CALL in calls,
            "exempt": bool(re.search(r"mutex-ok:\s*\S", seg)),
            "calls": calls,
        })
    return out, tree


def check(src: str, verbose: bool = True):
    """返回 (通过数, 失败列表)。"""
    rows, tree = post_views(src)
    passed, failed = 0, []

    def one(name, ok, why):
        nonlocal passed
        if ok:
            passed += 1
        else:
            failed.append((name, why))

    one("R4 守卫函数真实存在且读 compile_state",
        any(isinstance(n, ast.FunctionDef) and n.name == GUARD_CALL
            and "ingest.compile_state" in
            {dotted(c.func) for c in ast.walk(n) if isinstance(c, ast.Call)}
            for n in tree.body),
        "_compile_running_guard 不存在或没有读 ingest.compile_state()")

    for r in rows:
        if r["is_write"]:
            one(f"R1 {r['name']} 写视图接了守卫", r["has_guard"],
                f"views.py:{r['line']} 会写 .drafts//wiki/ 或参与裁决，却没接守卫")
            one(f"R3 {r['name']} 没用豁免绕过", not r["exempt"],
                f"views.py:{r['line']} 写视图不许用 mutex-ok 豁免")
        else:
            one(f"R2 {r['name']} 显式写明豁免理由", r["exempt"],
                f"views.py:{r['line']} 非写 POST 视图必须写 # mutex-ok: <理由>")

    if verbose:
        for r in rows:
            tag = "写·守卫" if r["is_write"] else "非写·豁免"
            print(f"  {tag:10s} views.py:{r['line']:<5d} {r['name']}")
    return passed, failed


def self_test():
    """守卫自己会响：好样本必不报、两种坏样本必报。"""
    good = '''
@require_POST
def reader(request):
    """# mutex-ok: 只读预览，不写库"""
    return HttpResponse("ok")

@require_POST
def writer(request):
    if msg := _compile_running_guard():
        return _fragment(request, flash=msg)
    wstore.approve("x")

def _compile_running_guard():
    return bool(ingest.compile_state()["running"])
'''
    bad_no_guard = good.replace(
        '    if msg := _compile_running_guard():\n'
        '        return _fragment(request, flash=msg)\n', "")
    bad_no_exempt = good.replace("    \"\"\"# mutex-ok: 只读预览，不写库\"\"\"\n",
                                 '    """只读预览"""\n')
    results = []
    _, f_good = check(good, verbose=False)
    results.append(("自测-好样本不报", not f_good, f_good))
    _, f_bad1 = check(bad_no_guard, verbose=False)
    hit1 = any("R1 writer" in n for n, _ in f_bad1)
    results.append(("自测-坏样本：写视图漏守卫必报", hit1, f_bad1))
    _, f_bad2 = check(bad_no_exempt, verbose=False)
    hit2 = any("R2 reader" in n for n, _ in f_bad2)
    results.append(("自测-坏样本：非写视图没豁免必报", hit2, f_bad2))
    # ★ 白名单是「守卫论域」本身：加进来的新写口如果哪天被删掉 / 拼错名字，
    #   上面两条自测照样全绿（它们只认 wstore.approve）。这条专门验新写口。
    bad_core_write = '''
@require_POST
def related(request):
    wiki_related.recommend("x")
'''
    _, f_bad3 = check(bad_core_write, verbose=False)
    hit3 = any("R1 related" in n for n, _ in f_bad3)
    results.append(("自测-坏样本：core 层写口漏守卫必报", hit3, f_bad3))
    return results


def main():
    src = VIEWS.read_text(encoding="utf-8")
    passed, failed = check(src)
    for name, ok, extra in self_test():
        if ok:
            passed += 1
        else:
            failed.append((name, f"自测失效：{extra}"))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    for name, why in failed:
        print(f"  FAIL  {name} <<< {why}")
    print(f"\n结果：{passed} 通过 / {len(failed)} 失败")
    if failed:
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
