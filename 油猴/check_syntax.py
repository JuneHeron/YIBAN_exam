"""JS 真·语法校验：用 esprima 解析整个文件，语法错在哪一行直接报出来。

为什么需要它：`check_balance.py` 只做「字符串完整性 + 括号配平」的机械检查，
**错了的代码它照样说通过**（比如 `const a = ;`、少一个逗号、把函数调用写成了
`foo(]`）。真正的语法校验以前做不了——本地没有 node/deno。后来发现 Windows 那个
Python（`py`，不是 msys64 的 `python`）能正常 pip 装 esprima，就补上了这道。

用法：

    py 油猴/check_syntax.py 油猴/yiban-bank.user.js
    py 油猴/check_syntax.py 油猴/*.js          # 可传多个文件

没装 esprima 时它会告诉你怎么装（一条 pip 命令，一次性）。
注意：esprima 4.0.1 支持到 ES2017，`?.` / `??` / `||=` 这类 ES2020 语法它**解析不了**，
会误报语法错。这个脚本还在用老写法，所以能过；哪天引入了新语法，先升级到
`esprima-python` 的新版本，或换 node（`node --check`）。

退出码：0 = 全部通过，1 = 有文件没通过（方便接进 CI 或批处理）。
"""

import sys
from pathlib import Path

try:
    import esprima
except ImportError:
    print("缺 esprima。装一次即可：")
    print("    py -m pip install esprima")
    print("（要用 Windows 那个 Python：`py`。msys64 的 `python` 没有 pip。）")
    raise SystemExit(2)


def check(path):
    """返回 True = 通过。"""
    src = Path(path).read_text(encoding="utf-8")
    lines = src.splitlines()
    try:
        # parseScript：按普通脚本解析（文件是个顶层 IIFE，没有 import/export）
        esprima.parseScript(src, {"loc": True})
    except Exception as e:                      # esprima.Error，但它没导出统一基类
        desc = getattr(e, "description", str(e))
        ln = getattr(e, "lineNumber", None)
        col = getattr(e, "column", None)
        print(f"[X] {path} 语法错：{desc}")
        if ln is not None:
            print(f"    第 {ln} 行 第 {col} 列")
            # 报行号时把出错那一行的原文打出来，省得再开编辑器找
            if 0 < ln <= len(lines):
                print(f"    {ln:>4} | {lines[ln - 1]}")
        return False

    # 顺带报几个数字：改完能一眼看出文件是不是被吃掉了半截
    print(f"[OK] {path}  {len(src)} 字符 / {len(lines)} 行  语法通过")
    return True


def main():
    args = sys.argv[1:]
    if not args:
        args = ["油猴/yiban-bank.user.js"]

    ok = True
    for path in args:
        if not Path(path).exists():
            print(f"[X] 找不到 {path}")
            ok = False
            continue
        ok = check(path) and ok

    if len(args) > 1:
        print("全部通过" if ok else "有文件没通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
