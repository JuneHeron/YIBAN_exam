#!/usr/bin/env python3
"""考试时用的粘题窗口：粘一道、出一道，循环不停。

双击 问答案.bat 启动，或在命令行跑 python ask.py
"""

from __future__ import annotations

import sys

# 往控制台写时让 Python 用控制台自己的编码（Windows 下走 WriteConsoleW，中文能正常显示），
# 只在被管道/重定向时才强制 UTF-8。errors="replace" 是兜底——题库里有 ①②③④「」
# 这类字，万一某个字符编不出来也不该让程序崩掉。
try:
    if sys.stdout.isatty():
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import bank  # noqa: E402


def main() -> None:
    conn = bank.connect()
    total = conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
    print("=" * 66)
    print(f"  题库 {total} 题")
    print()
    print("  用法：把题目连选项一起粘进来，粘完【按一下回车】。")
    print("        那一下屏幕上不会有任何变化（这一行是空的），")
    print("        不用管，直接等答案出来就行。")
    print()
    print("  输入 q 再回车 = 退出")
    print("=" * 66)

    while True:
        print("\n粘题目（连选项），粘完按一下回车：")
        lines = bank.read_pasted_question()
        if lines is None:
            print("已退出。")
            return
        try:
            print()
            print(bank.resolve(conn, "\n".join(lines)))
        except Exception as exc:  # 任何异常都不能把窗口关掉
            print(f"查询出错：{exc!r}")
        conn.rollback()


if __name__ == "__main__":
    main()
