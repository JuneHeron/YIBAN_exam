"""用 Python 等价实现油猴脚本里的识别算法，在真实 DOM 上跑一遍。

本地没有 JS 运行时（node/deno 都没有，代理不通装不了 esprima），所以没法直接跑
yiban-bank.user.js。这里把 user.js 里的 adoptOptionElements / 分组 / extraText /
jsCleanStem 按同样的规则重写一份，喂真实 DOM，验证结果是否正确。

只验证**算法**，不验证 JS 语法——语法只能靠在浏览器里加载一次来验。
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser

VOID = {"br", "img", "input", "hr", "meta", "link"}


class Node:
    def __init__(self, tag: str, attrs: list[tuple[str, str]] | None = None, text: str = ""):
        self.tag = tag
        self.attrs = dict(attrs or [])
        self.children: list["Node"] = []
        self.text = text           # 只有 tag == "#text" 时用
        self.parent: Node | None = None

    # --- 便捷访问 ---
    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def text_content(self) -> str:
        if self.tag == "#text":
            return self.text
        return "".join(c.text_content() for c in self.children)

    def element_children(self) -> list["Node"]:
        """元素子节点。JS 的 el.children 不含文本节点，模拟时必须对齐——踩过：
        直接用 self.children 会把「只含文本的 div」误判成非叶子，选项一个都找不到。"""
        return [c for c in self.children if c.tag != "#text"]

    def descendants(self):
        for c in self.children:
            yield c
            yield from c.descendants()

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent

    def contains(self, other: "Node") -> bool:
        return any(a is self for a in other.ancestors())

    def clone(self) -> "Node":
        n = Node(self.tag, list(self.attrs.items()))
        n.text = self.text
        for c in self.children:
            cc = c.clone()
            cc.parent = n
            n.children.append(cc)
        return n

    def drop(self, tags: set[str]) -> None:
        """递归删掉指定标签的子树（含自身若匹配）。"""
        self.children = [c for c in self.children if c.tag not in tags]
        for c in self.children:
            c.drop(tags)


class Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root-node")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            node = Node(tag, attrs)
            node.parent = self.cur
            self.cur.children.append(node)
            return
        node = Node(tag, attrs)
        node.parent = self.cur
        self.cur.children.append(node)
        self.cur = node

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        p = self.cur
        while p is not None and p.tag != tag:
            p = p.parent
        if p is not None and p.parent is not None:
            self.cur = p.parent

    def handle_data(self, data):
        if data.strip():
            n = Node("#text", None, data)
            n.parent = self.cur
            self.cur.children.append(n)


# ---------------- 以下与 user.js 的规则一一对应 ----------------

OPT_RE = re.compile(r"^([A-H])\s*[.、．)）:：]\s*(\S[\s\S]*)$")

CHROME_RE = re.compile(
    r"(?:查看试卷|查看答案|查看解析|随机选择|返回列表|上一题|下一题|答题卡|"
    r"提交试卷|开始考试|考试时长|剩余时间|我的试卷|错题回顾|冲！|冲!)\s*")
SECTION_RE = re.compile(
    r"(?:^|\s)[一二三四五六七八九十]+\s*[、.．]\s*"
    r"(?:单选题|多选题|判断题|是非题|填空题|简答题|问答题|选择题)\s*")
PROGRESS_RE = re.compile(r"(?:\d+\s+/\s+\d+|考试倒计时|剩余\s*\d+[:：]\d+)\s*")
LEADNO_RE = re.compile(r"^\s*(?:第\s*)?\d{1,4}\s*[、.．)）]\s*(?:[\[（(]\s*\d+\s*分\s*[\]）)])?\s*")
SCORE_RE = re.compile(r"^[\[（(]\s*\d+\s*分\s*[\]）)]\s*")
ANS_BLOCK_RE = re.compile(r"(?:正确答案|参考答案|正确选项)\s*[：:]\s*[A-H][A-H\s、，,]*")


def js_clean_stem(text: str) -> str:
    t = CHROME_RE.sub("", text or "")
    t = SECTION_RE.sub(" ", t)
    t = PROGRESS_RE.sub(" ", t)
    out = []
    for ln in t.split("\n"):
        s = ln.strip()
        for _ in range(3):
            n = SCORE_RE.sub("", LEADNO_RE.sub("", s)).strip()
            if n == s:
                break
            s = n
        out.append(s)
    return "\n".join(out).strip()


def adopt_option_elements(root: Node) -> list[Node]:
    found = []
    for el in root.descendants():
        if el.tag == "#text" or el.element_children():
            continue                       # 只看叶子元素（无元素子节点）
        if any(a.attrs.get("id") == "yqbank-panel" for a in el.ancestors()):
            continue
        t = el.text_content().strip()
        if not t or len(t) > 300:
            continue
        if OPT_RE.match(t):
            found.append(el)
    return found


def extra_text(container: Node, option_els: list[Node]) -> str:
    clone = container.clone()
    clone.drop({"button", "script", "style", "noscript", "svg", "input"})
    t = clone.text_content()
    for o in option_els:
        s = o.text_content().strip()
        if s:
            t = t.replace(s, "")
    t = ANS_BLOCK_RE.sub("", t)
    return js_clean_stem(t)


def adopt_questions(root: Node) -> list[dict]:
    opts = adopt_option_elements(root)
    if not opts:
        return []
    groups, used = [], set()
    for el in opts:
        if el in used:
            continue
        chain = list(el.ancestors())[:10]
        picked = None
        for anc in chain:
            inside = [o for o in opts if anc.contains(o)]
            if len(inside) > 8:
                break
            if len(inside) >= 2:
                picked = (anc, inside)
                break
        if not picked:
            continue
        anc, inside = picked
        if any(o in used for o in inside):
            continue
        used.update(inside)
        groups.append((anc, inside))

    out = []
    for opt_box, inside in groups:
        options = []
        for i, el in enumerate(inside):
            t = el.text_content().strip()
            m = OPT_RE.match(t)
            options.append({
                "letter": m.group(1) if m else chr(65 + i),
                "text": m.group(2).strip() if m else t,
            })
        container, p = opt_box, opt_box.parent
        for _ in range(6):
            if p is None:
                break
            if len(extra_text(p, inside)) >= 6:
                container = p
                break
            p = p.parent
        out.append({
            "stem": extra_text(container, inside),
            "options": options,
            "container": container,
        })
    return [q for q in out if q["stem"] and len(q["options"]) >= 2]


REVEAL_RE = re.compile(r"(?:正确答案|参考答案|正确选项)\s*[：:]\s*([A-H][A-H\s、，,]*)")
CORRECT_CLASS_RE = re.compile(r"(?:correct|right|dui)", re.I)


def adopt_revealed_answer(container: Node) -> str | None:
    """与 user.js 的 adoptRevealedAnswer 同规则：先按文本找，再按正确项 class 找。"""
    text = container.text_content()
    m = REVEAL_RE.search(text)
    if m:
        return re.sub(r"[^A-H]", "", m.group(1))
    letters = []
    for el in container.descendants():
        if el.tag == "#text":
            continue
        if any(CORRECT_CLASS_RE.search(c) for c in el.classes):
            mm = OPT_RE.match(el.text_content().strip())
            if mm:
                letters.append(mm.group(1))
    return "".join(sorted(set(letters))) or None


def main(path: str) -> int:
    html = open(path, encoding="utf-8").read()
    b = Builder()
    b.feed(html)
    root = b.root

    raw_opts = adopt_option_elements(root)
    print(f"识别到「像选项」的叶子元素: {len(raw_opts)} 个")
    for o in raw_opts:
        print(f"   <{o.tag} class={o.attrs.get('class','')!r}> {o.text_content().strip()!r}")
    print()

    qs = adopt_questions(root)

    # 每个样本各自的期望值。写死成一份样本会导致另一份必然报错（踩过）。
    EXPECT = {
        "_test_page.html": {
            "stem": "2025年是成都理工大学成立()周年(学校前身成都地质勘探学院始建于1956年)。",
            "options": [("A", "75"), ("B", "69"), ("C", "66"), ("D", "100")],
            "answer": "B",          # 试卷页显示「正确答案：B」
        },
        "_test_page_multi.html": {
            "stem": "学校以服务国家行业地方发展建设为己任，积极参与()等工程建设",
            "options": [("A", "能源开发"), ("B", "西气东输"), ("C", "青藏铁路"), ("D", "南水北调")],
            "answer": None,          # 答题页不显示答案，读不到才是对的
        },
    }
    name = path.replace("\\", "/").split("/")[-1]
    exp = EXPECT.get(name)
    if exp is None:
        print(f"!! 这个样本没有配期望值: {name}")
        return 1

    print("识别到题目: %d 道" % len(qs))
    print()
    ok = True
    for q in qs:
        print(f"题干   : {q['stem']!r}")
        print(f"容器   : <{q['container'].tag} class={q['container'].attrs.get('class','')!r}>")
        print(f"选项   : {[(o['letter'], o['text']) for o in q['options']]}")
        ans = adopt_revealed_answer(q["container"])
        print(f"读到的正确答案: {ans!r}（期望 {exp['answer']!r}）")
        print()

        if q["stem"] != exp["stem"]:
            print("!! 题干不对")
            print("   期望 %r" % exp["stem"])
            print("   实际 %r" % q["stem"])
            ok = False
        for bad in ("查看试卷", "在线考试", "冲！", "随机选择", "单选题", "多选题", "1、", "[1分]", "正确答案", "考试倒计时"):
            if bad in q["stem"]:
                print(f"!! 题干里混进了 {bad!r}")
                ok = False
        got_opts = [(o["letter"], o["text"]) for o in q["options"]]
        if got_opts != exp["options"]:
            print(f"!! 选项不对 期望={exp['options']}")
            ok = False
        if ans != exp["answer"]:
            print("!! 正确答案读取不对")
            ok = False

    if not qs:
        print("!! 一道题都没识别到——这必须算失败，否则空结果也会被误判成通过（踩过）")
        return 1
    print("=" * 56)
    print("算法验证通过：题干干净、选项正确" if ok else "算法验证失败，见上面 !!")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "_test_page.html"))
