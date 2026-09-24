"""油猴脚本的两项机械检查。

1. **跨行的单/双引号字符串** —— 本项目误过五次的坑：文件在多次转义传递中把
   `\\n` 变成了真换行，字符串断成两行。括号配平检查**看不见**这种错
   （字符串被整体跳过，括号照样配平），但 JS 会直接语法报错，所以必须单独查。
   模板串（反引号）天然可以跨行，不算错。

2. **括号配平** —— 圆括号 / 方括号 / 花括号，含模板串 ${} 内的代码。

这两项都是启发式的。**真·语法校验用同目录的 `check_syntax.py`**（esprima），
两个一起跑才是完整的；跑完都过了，最后仍然建议在浏览器里加载一次。
"""
import pathlib
import sys

BS = chr(92)   # 反斜杠
BT = chr(96)   # 反引号
SQ = chr(39)   # 单引号
DQ = chr(34)   # 双引号
QUOTES = (DQ, SQ, BT)
REGEX_PREV = "(,=:[!&|?{};+-*%~^<>"


def scan(src: str, want_multiline: bool):
    """走一遍源码。want_multiline=True 时返回跨行字符串的行号对，否则返回去噪文本。"""
    out: list[str] = []
    multiline: list[tuple[int, int]] = []
    i, n = 0, len(src)
    state = None
    prev = ""
    line = 1
    str_start = 0
    tmpl: list[int] = []

    def keep(c: str) -> None:
        if c == "\n":
            out.append(c)

    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if state is None:
            if c == "/" and nxt == "/":
                state, i = "line", i + 2
                continue
            if c == "/" and nxt == "*":
                state, i = "block", i + 2
                continue
            if c in QUOTES:
                state, str_start, i, prev = c, line, i + 1, c
                continue
            if c == "/" and (prev == "" or prev in REGEX_PREV):
                state, i, prev = "regex", i + 1, "/"
                continue
            if tmpl:
                if c == "{":
                    tmpl[-1] += 1
                elif c == "}":
                    if tmpl[-1] == 0:
                        tmpl.pop()
                        state, i = BT, i + 1
                        continue
                    tmpl[-1] -= 1
            out.append(c)
            if c == "\n":
                line += 1
            elif not c.isspace():
                prev = c
            i += 1
            continue

        if state == "line":
            if c == "\n":
                state = None
                out.append(c)
                line += 1
            i += 1
            continue

        if state == "block":
            if c == "*" and nxt == "/":
                state, i = None, i + 2
                continue
            keep(c)
            if c == "\n":
                line += 1
            i += 1
            continue

        if state == "regex":
            if c == BS:
                keep(c)
                i += 2
                continue
            if c == "[":
                i += 1
                while i < n and src[i] != "]":
                    keep(src[i])
                    i += 2 if src[i] == BS else 1
                i += 1
                continue
            if c == "/":
                state = None
            keep(c)
            if c == "\n":
                line += 1
            i += 1
            continue

        # 字符串 / 模板串内
        if c == BS:
            keep(c)
            i += 2
            continue
        if state == BT and c == "$" and nxt == "{":
            tmpl.append(0)
            state, i = None, i + 2
            continue
        if c == "\n":
            if state != BT:
                # 报「从第几行跨到第几行」：换行符在第 line 行末尾，所以是跨到 line+1。
                # 写 (str_start, line) 会显示成「469 跨到 469」，定位不到问题（踩过）。
                multiline.append((str_start, line + 1))
                state = None
            keep(c)
            line += 1
            i += 1
            continue
        if c == state:
            state = None
        else:
            keep(c)
        i += 1

    return multiline if want_multiline else "".join(out)


def check_multiline(path: str, src: str) -> bool:
    bad = scan(src, want_multiline=True)
    if not bad:
        print("没有跨行的单/双引号字符串")
        return True
    print("!! 有单/双引号字符串跨行了（多半是 \\n 转义在传递中被吃成真换行）:")
    for a, b in bad:
        print(f"   第 {a} 行开始，跨到第 {b} 行")
    return False


def check_balance(src: str) -> bool:
    clean = scan(src, want_multiline=False)
    pairs = {")": "(", "]": "[", "}": "{"}
    stack, line, bad = [], 1, None
    for ch in clean:
        if ch == "\n":
            line += 1
        elif ch in "([{":
            stack.append((ch, line))
        elif ch in pairs:
            if not stack or stack[-1][0] != pairs[ch]:
                bad = f"第 {line} 行出现多余的 {ch}"
                break
            stack.pop()
    if bad:
        print("!! 括号不配平:", bad)
        return False
    if stack:
        print("!! 有未闭合的括号:", [f"{c}@第{l}行" for c, l in stack[:5]])
        return False
    print("圆括号 / 方括号 / 花括号 全部配平（含模板串 ${} 内的代码）")
    return True


def main(path: str) -> int:
    src = pathlib.Path(path).read_text(encoding="utf-8")
    print(f"源码 {len(src)} 字符 / {src.count(chr(10)) + 1} 行")
    ok1 = check_multiline(path, src)
    ok2 = check_balance(src)
    print("两项都通过" if (ok1 and ok2) else "有问题，见上面 !!")
    print("局限：只查结构与字符串完整性，不校验语法语义——语法校验请跑 check_syntax.py。")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
