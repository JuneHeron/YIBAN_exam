#!/usr/bin/env python3
"""学生手册题库：采集 / 去重 / 检索 / 刷题。

数据存单文件 SQLite（bank.db），只用标准库。

不用手工录题——题目由油猴脚本在交卷后的试卷页采回来：
    python bank.py serve           # 启动本地桥接服务，脚本通过它查询和入库

查询（考试时）:
    python bank.py ask "学生公寓几点熄灯"    # 严格，查不到就明说查不到
    python bank.py find "熄灯"               # 宽松，列候选

维护:
    python bank.py stats          题库概况
    python bank.py conflicts      答案互相打架的题
    python bank.py clear          清空题库（先备份）
    python bank.py dupes          疑似重复的题目对
    python bank.py quiz --n 20    刷题，错题优先
    python bank.py export --out bank.json
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

DB_PATH = Path(os.environ.get("BANK_DB") or Path(__file__).with_name("bank.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY,
    stem_key    TEXT NOT NULL UNIQUE,
    stem_raw    TEXT NOT NULL,
    stem_norm   TEXT NOT NULL,
    qtype       TEXT DEFAULT 'single',
    options     TEXT,
    answer      TEXT NOT NULL,
    answer_text TEXT,
    explanation TEXT,
    source      TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    times_seen  INTEGER DEFAULT 1,
    right_count INTEGER DEFAULT 0,
    wrong_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_stem_norm ON questions(stem_norm);

-- 同一道题两次录入答案不一致时记在这里，不覆盖原答案，等人来裁定
CREATE TABLE IF NOT EXISTS conflicts (
    id         INTEGER PRIMARY KEY,
    stem_key   TEXT NOT NULL,
    stem_raw   TEXT NOT NULL,
    old_answer TEXT NOT NULL,
    new_answer TEXT NOT NULL,
    source     TEXT,
    seen_at    TEXT
);
"""

# 开头的题号。只在后面跟标点或“题”字时才剥掉，避免把 "1978年…" 这类题干削掉
_LEAD_NUM = re.compile(r"^(?:第\s*\d{1,4}\s*题|[（(\[]?\d{1,3}[）)\].、:：,，])\s*")
# 分值标记：[1分] / （2分）—— 是卷面噪音，不属于题干
_SCORE = re.compile(r"^[\[（(]\s*\d+\s*分\s*[\]）)]\s*")
# 只含题号和分值标记的行，是题目之间的分界
_Q_HEADER = re.compile(r"^\s*(?:第\s*)?\d{1,4}\s*[、.．)）]\s*(?:[\[（(]\s*\d+\s*分\s*[\]）)])?\s*$")

# 块首的“题目：/问：”之类标签
_LABEL = re.compile(r"^(?:题目|问题|题干|问)\s*[：:]\s*")
# 选项行：A. xxx
_OPT_LINE = re.compile(r"^([A-Ha-h])\s*[.、．)）:：]\s*(.+)$")
# 一行里塞了多个选项时的分隔标记，要求行首或空白之后
_OPT_MARK = re.compile(r"(?:(?<=^)|(?<=\s))([A-H])\s*[.、．)）:：]\s*")
# 答案行的关键词。抽成常量，因为好几处都要用（解析、失败原因、判断像不像一道题），
# 各写一份必然漏掉某个写法——实测漏过「正确选项：D」。
_ANS_WORD = r"(?:正确答案|参考答案|正确选项|标准答案|答案|answer)"
_ANS_INLINE = re.compile(rf"{_ANS_WORD}\s*[：:]\s*(.+)$", re.I)
_EXPL_LINE = re.compile(r"^(?:答案解析|解析|说明)\s*[：:]\s*(.+)$")


# 页面框架词。油猴脚本从 DOM 取题干时会把它们带进来——实测 20/20 条被污染成
# "查看试卷 冲！随机选择 一、单选题 11、[1分] …"。其中「查看试卷/随机选择/冲！」
# 其实是另一个油猴脚本注入的按钮文字，不是平台的元素。不清掉会污染全部题干，
# 还会让本可以和已有题目合并的重复项变成新行（实测 #168 本该并入 #15）。
_CHROME = re.compile(
    r"(?:查看试卷|查看答案|查看解析|随机选择|返回列表|上一题|下一题|答题卡|"
    r"提交试卷|开始考试|考试时长|剩余时间|我的试卷|错题回顾|冲！|冲!)\s*"
)
# 题型分节：一、单选题 / 二、多选题 / 三、是非题
_SECTION = re.compile(
    r"(?:^|\s)[一二三四五六七八九十]+\s*[、.．]\s*"
    r"(?:单选题|多选题|判断题|是非题|填空题|简答题|问答题|选择题|不定项选择题)\s*"
)
# 行中题号：只在带分值标记时剥，避免误伤「共5、6项」这类正文
_MID_QNO = re.compile(r"(?:^|\s)(?:第\s*)?\d{1,4}\s*[、.．)）]\s*[\[（(]\s*\d+\s*分\s*[\]）)]\s*")
# 页面进度条 / 倒计时。这些只出现在脚本发来的落空文本里（它把整个工具栏文本也取进来了）：
# "…的()。 8 / 100 考试倒计时 A. 6%，4"
#
# 斜杠两侧必须**有空格**：题面里的分数（1/3 学分、1/2 学时）是紧挨着的，
# 早期写成 \d+\s*/\s*\d+ 会把「班级前1/3」整段吃掉，静默丢内容（踩过）。
_PROGRESS = re.compile(r"(?:\d+\s+/\s+\d+|考试倒计时|距考试结束[^\s]{0,12}|剩余\s*\d+[:：]\d+)")
# 行内选项起点：脚本有时把选项拼在题干同一行
_OPT_INLINE_CUT = re.compile(r"\s[A-H]\s*[.、．)）]\s*")


def clean_stem(text: str) -> str:
    """把页面框架、题型分节、题号、分值标记从题干里剥干净。

    处理两种形态：正常的「一行一样式」（题号在行首）逐行剥；
    被脚本压成一行的（题号卡在中间）靠 _MID_QNO 剥。
    换行必须保留——服务端的分块解析依赖行结构。
    """
    t = _CHROME.sub("", text or "")
    t = _SECTION.sub(" ", t)
    t = _MID_QNO.sub(" ", t)
    lines = []
    for ln in t.splitlines():
        s = ln.strip()
        for _ in range(3):  # "1、[1分] 题干" 要连着剥两层
            new = _SCORE.sub("", _LEAD_NUM.sub("", s)).strip()
            if new == s:
                break
            s = new
        lines.append(s)
    return chr(10).join(lines).strip()


def strip_lead(text: str) -> str:
    """去掉题干开头的题号和分值标记，它们每次模拟都可能换个写法。"""
    t = (text or "").strip()
    for _ in range(3):  # "1、[1分] 题干" 要连着剥两层
        new = _SCORE.sub("", _LEAD_NUM.sub("", t)).strip()
        if new == t:
            break
        t = new
    return t


def norm(text: str) -> str:
    """题干规范化：用于跨多次模拟识别同一道题。

    标点和空白一律不参与比对。逐字符去查 Unicode 类别（P* 标点、Z* 空白），
    而不是枚举具体字符——题干里的引号括号写法很多样（"" 与 「」、() 与 （）、
    全角与半角），枚举必然漏，漏一个就是同一道题匹配不上。
    """
    t = unicodedata.normalize("NFKC", clean_stem(text)).lower()
    return "".join(ch for ch in t if not unicodedata.category(ch).startswith(("P", "Z")))


def norm_answer(ans: str) -> str:
    a = unicodedata.normalize("NFKC", ans or "").upper().strip()
    a = re.sub(r"^[（(\[]|[）)\]]$", "", a)
    # 纯字母答案（可能写成 A、B、C 或 A,B 或 A B）统一成紧凑形式 AB，
    # 否则 "A、B" 和 "AB" 会被当成两个不同答案。文字答案（含顿号/逗号的中文）不能动。
    if re.fullmatch(r"[A-H\s、，,;；/|]+", a):
        return re.sub(r"[^A-H]", "", a)
    return a


# 纯套话型题干（「下列说法错误的是」这类）。剥掉这些套话后如果几乎不剩什么，
# 说明题干本身撑不起题目身份——同一句套话下平台可能有好几道不同的题。
_BOILERPLATE = re.compile(
    r"(?:下列|以下|下述|关于|对于)?(?:说法|选项|行为|内容|表述|叙述|做法|观点)"
    r"(?:中|里)?(?:错误|正确|不正确|不恰当|不当|有误)的?(?:是|有|包括|为)?"
)


def is_generic_stem(stem: str) -> bool:
    """题干是否短到/空到不足以唯一标识一道题。"""
    n = norm(stem)
    if len(n) < 8:
        return True  # 太短，任何题都可能撞
    return len(_BOILERPLATE.sub("", n)) < 6  # 剥掉套话后没剩下实质内容


def canonical_options(options: list[str] | None) -> str:
    """选项集合的规范化形式：排序 + 去标点，所以打乱顺序不影响它。"""
    if not options:
        return ""
    return "\x1f".join(sorted(norm(o) for o in options))


def stem_key(stem: str, options: list[str] | None = None) -> str:
    """题目身份。

    题干能唯一标识题目时就只用题干——选项顺序可能被打乱、措辞也可能微调，
    题干才是稳定的身份。但题干是「下列说法错误的是」这类纯套话时，光靠题干
    会把多道不同的题撞成一道、答案互相覆盖（实测已发生），这时必须把选项集合
    也算进身份。宁可多分出一条，也不能把不同的题合并——合并会给出错误答案。
    """
    base = norm(stem)
    if is_generic_stem(stem):
        base += "\x00" + canonical_options(options)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def answer_text_of(answer: str, options: list[str] | None) -> str | None:
    """把 "AC" 这种字母答案翻成选项原文，防止平台打乱选项顺序后答案失真。"""
    if not options or not answer:
        return None
    picked = []
    for ch in re.findall(r"[A-H]", norm_answer(answer)):
        idx = ord(ch) - ord("A")
        if 0 <= idx < len(options):
            picked.append(options[idx])
    return " / ".join(picked) if picked else None


def normalize_options(raw) -> list[str]:
    if isinstance(raw, dict):
        return [str(raw[k]).strip() for k in sorted(raw)]
    if isinstance(raw, list):
        return [str(o.get("text") if isinstance(o, dict) else o).strip() for o in raw]
    if isinstance(raw, str) and raw.strip():
        return [p.strip() for p in re.split(r"[|｜\n]", raw) if p.strip()]
    return []


def rec_options(rec: dict) -> list[str]:
    return normalize_options(rec.get("options") or rec.get("选项") or [])


def rec_stem(rec: dict) -> str:
    return clean_stem(rec.get("stem") or rec.get("题干") or rec.get("question") or "")


def upsert(conn: sqlite3.Connection, rec: dict, source: str) -> str:
    stem = rec_stem(rec)
    answer = norm_answer(rec.get("answer") or rec.get("答案") or "")
    if not stem or not answer:
        return "skip"

    options = normalize_options(rec.get("options") or rec.get("选项") or [])
    now = datetime.now().isoformat(timespec="seconds")
    key = stem_key(stem, options)
    opts_json = json.dumps(options, ensure_ascii=False) if options else None

    row = conn.execute(
        "SELECT id, answer, options, times_seen FROM questions WHERE stem_key = ?", (key,)
    ).fetchone()

    if row:
        old_opts = json.loads(row["options"]) if row["options"] else []
        # 字母相同当然是一个答案；字母不同也**不一定**是不同答案——
        # 平台打乱选项后，同一个正确选项会落到别的字母上（实测 D 变 A）。
        # 所以还要比一次「答案的选项原文」，否则每次打乱都会误报冲突。
        old_text = answer_text_of(row["answer"], old_opts)
        new_text = answer_text_of(answer, options or None)
        same = norm_answer(row["answer"]) == answer or (
            bool(old_text) and old_text == new_text
        )
        if not same:
            # 真的打架：保留先到的，记下来让人裁定。静默覆盖会让题库烂掉。
            conn.execute(
                "INSERT INTO conflicts (stem_key, stem_raw, old_answer, new_answer, source, seen_at)"
                " VALUES (?,?,?,?,?,?)",
                (key, stem, row["answer"], answer, source, now),
            )
            conn.execute(
                "UPDATE questions SET last_seen=?, times_seen=times_seen+1 WHERE id=?",
                (now, row["id"]),
            )
            return "conflict"
        conn.execute(
            "UPDATE questions SET last_seen=?, times_seen=times_seen+1, answer=?, answer_text=?,"
            " options=COALESCE(?, options) WHERE id=?",
            (now, answer, answer_text_of(answer, options or None), opts_json, row["id"]),
        )
        return "update"

    conn.execute(
        "INSERT INTO questions (stem_key, stem_raw, stem_norm, qtype, options, answer, answer_text,"
        " explanation, source, first_seen, last_seen, times_seen)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
        (
            key,
            stem,
            norm(stem),
            rec.get("type") or rec.get("题型") or "single",
            opts_json,
            answer,
            answer_text_of(answer, options or None),
            rec.get("explanation") or rec.get("解析"),
            source,
            now,
            now,
        ),
    )
    return "insert"


def split_inline_options(line: str) -> list[str]:
    """把 "A. 甲 B. 乙 C. 丙" 拆成三个选项。拆不动就返回空。"""
    marks = list(_OPT_MARK.finditer(line))
    if len(marks) < 2:
        return []
    letters = [m.group(1) for m in marks]
    if letters[0] != "A" or letters != sorted(letters) or len(set(letters)) != len(letters):
        return []
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(line)
        out.append(line[m.end() : end].strip(" \t、,，;；"))
    return [o for o in out if o]


def option_letters(lines: list[str]) -> list[str]:
    """按出现顺序取出所有选项字母，用来发现「一块里塞了两道题」。"""
    opt_idx = set(option_line_indices(lines))
    letters: list[str] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        marks = list(_OPT_MARK.finditer(s))
        if len(marks) >= 2 and marks[0].group(1) == "A":
            letters.extend(m.group(1) for m in marks)
            continue
        m = _OPT_LINE.match(s)
        if m and i in opt_idx:          # 忽略被判定为题干子项的小写行
            letters.append(m.group(1).upper())
    return letters


def has_duplicate_letters(lines: list[str]) -> bool:
    """选项字母重复 = 一块里有不止一道题。"""
    letters = option_letters(lines)
    return len(letters) != len(set(letters))


def parse_block(block: str) -> dict | None:
    lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
    if not lines:
        return None

    # 一道题的选项字母只出现一次。重复说明这一块里不止一道题——
    # 宁可拒收并报出来，也不能把两道题合并成一道（选项翻倍、答案串位）。
    if has_duplicate_letters(lines):
        return None

    # 问答对：Q: / 问： 开头，下一行是答：/ A:
    m = re.match(r"^(?:Q|问|问题|题目)\s*[：:]\s*(.+)$", lines[0], re.I)
    if m and len(lines) >= 2:
        m2 = re.match(rf"^(?:A|答|{_ANS_WORD})\s*[：:]\s*(.+)$", lines[1], re.I)
        if m2:
            opts = []
            for ln in lines[2:]:
                mo = _OPT_LINE.match(ln)
                if mo:
                    opts.append(mo.group(2))
            return {"stem": m.group(1), "options": opts, "answer": m2.group(1)}

    stem_lines: list[str] = []
    options: list[str] = []
    answer: str | None = None
    explanation: str | None = None
    # 哪些行算选项：大写优先，小写子项（a./b./c./d.）在大写选项存在时归题干。
    # 不这么做的话，「流程为()。 a.… b.… c.… d.…」这类题干会把选项位置整体顶偏。
    opt_idx = set(option_line_indices(lines))

    for li, ln in enumerate(lines):
        me = _EXPL_LINE.match(ln)
        if me:
            explanation = me.group(1)
            continue

        ma = _ANS_INLINE.search(ln)
        if ma:
            answer = ma.group(1).strip()
            head = ln[: ma.start()].strip()
            if head:
                inline = split_inline_options(head)
                if inline and not options:
                    options = inline
                elif not stem_lines:
                    stem_lines.append(_LABEL.sub("", head))
            continue

        inline = split_inline_options(ln)
        if inline:
            options.extend(inline)
            continue

        mo = _OPT_LINE.match(ln)
        if mo and li in opt_idx:
            options.append(mo.group(2))
            continue

        if options:
            continue  # 选项之后的杂行丢掉
        stem_lines.append(_LABEL.sub("", ln))

    if stem_lines and answer:
        return {
            "stem": " ".join(stem_lines),
            "options": options,
            "answer": answer,
            "explanation": explanation,
        }
    return None
def parse_text(text: str) -> tuple[list[dict], list[str]]:
    """把粘贴的文本切成一题一题，返回 (题目, 解析失败的块)。

    分界有两个：空行，以及只含题号的表头行（`1、[1分]`）。
    后者很关键——整份卷子粘下来时题目之间往往一个空行都没有。
    """
    records: list[dict] = []
    failures: list[str] = []
    cur: list[str] = []

    def flush() -> None:
        if cur:
            raw = "\n".join(cur)
            rec = parse_block(raw)
            if rec:
                records.append(rec)
            else:
                failures.append(raw)
            cur.clear()

    for line in text.splitlines():
        if not line.strip() or _Q_HEADER.match(line):
            flush()
        else:
            cur.append(line)
    flush()
    return records, failures
def run_import(conn: sqlite3.Connection, items) -> tuple[dict, list[str], list[str]]:
    """录入一批 (rec, source)，并把两种重复分开。

    同一场考试内重复 = 平台题库自己就有重复条目（这会让题库实际题数少于名义题数）；
    跨场重复 = 你在覆盖同一个池子，正常现象。
    """
    tally = {"insert": 0, "update": 0, "skip": 0, "conflict": 0}
    seen: set[str] = set()
    within: list[str] = []
    across: list[str] = []
    for rec, source in items:
        stem = rec_stem(rec)
        key = stem_key(stem, rec_options(rec)) if stem else None
        if key:
            if key in seen:
                within.append(stem)
            elif conn.execute("SELECT 1 FROM questions WHERE stem_key = ?", (key,)).fetchone():
                across.append(stem)
            seen.add(key)
        tally[upsert(conn, rec, source)] += 1
    return tally, within, across
def match(query_norm: str, cand_norm: str) -> tuple[float, str]:
    """返回 (分数, 匹配类型)。只输入片段时也得排得上来，所以两个方向都算。"""
    if query_norm == cand_norm:
        return 1.0, "完全相同"
    sm = difflib.SequenceMatcher(None, query_norm, cand_norm)
    block = sm.find_longest_match(0, len(query_norm), 0, len(cand_norm)).size
    cov_q = block / max(1, len(query_norm))  # 你打的那段有多少被这题包含
    cov_c = block / max(1, len(cand_norm))   # 这题有多少出现在你打的那段里
    if cov_q >= 0.999 and len(query_norm) < len(cand_norm):
        return 0.90 + 0.10 * len(query_norm) / len(cand_norm), "片段包含"
    if cov_c >= 0.999:
        return 0.90 + 0.10 * len(cand_norm) / len(query_norm), "被包含"
    return max(sm.ratio(), cov_c * 0.90), "近似"


def lookup(conn: sqlite3.Connection, query: str, top: int) -> list[tuple[float, str, sqlite3.Row]]:
    q = norm(query)
    if not q:
        return []
    exact = conn.execute("SELECT * FROM questions WHERE stem_norm = ?", (q,)).fetchall()
    if exact:
        return [(1.0, "完全相同", r) for r in exact]
    rows = conn.execute("SELECT * FROM questions").fetchall()
    if not rows:
        return []
    ranked = sorted(((match(q, r["stem_norm"]), r) for r in rows), key=lambda x: -x[0][0])
    return [(s, kind, r) for (s, kind), r in ranked[:top]]


def cmd_find(args: argparse.Namespace) -> None:
    conn = connect()
    if not conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]:
        print("题库是空的，先录入")
        return
    hits = lookup(conn, args.query, args.top)
    for s, kind, row in hits:
        if s < args.min:
            break
        show(row, s, kind)
    if not hits or hits[0][0] < args.min:
        print("没找到像样的匹配——这题大概还没进题库。")


def query_from_text(text: str) -> str:
    """把粘贴进来的一整道题压成用于匹配的题干。

    容许你连选项和「正确答案：X」一起粘——那些行会被丢掉，
    只拿题干去匹配。这样就不用在命令行里加引号，也不用手工剥选项。
    """
    lines = text.splitlines()
    opt_idx = set(option_line_indices(lines))
    keep = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or _Q_HEADER.match(s):
            continue
        if i in opt_idx or split_inline_options(s) or _ANS_INLINE.search(s):
            continue
        keep.append(s)          # 小写子项（a./b./c./d.）会留在这里，它们属于题干
    return " ".join(keep) if keep else text


def option_line_indices(lines: list[str]) -> list[int]:
    """哪些行算选项行。

    有一条硬规则：**存在大写选项行时，忽略小写的**。
    因为题面里的小写 a./b./c./d. 常是题干的子项，比如
    「违纪作弊处分期满后，申请解除处分的流程为()。 / a.向学院提交… / b.教务处审批 / …」，
    把它们当选项会让后面真正选项的位置整体后移，重算出来的字母落到页面上不存在的
    选项上（E），结果一个选项都点不上——实测就是这么漏答的。
    """
    upper, lower = [], []
    for i, ln in enumerate(lines):
        m = _OPT_LINE.match(ln.strip())
        if not m:
            continue
        (upper if m.group(1).isupper() else lower).append(i)
    return upper if upper else lower


def options_from_text(text: str) -> list[str]:
    """从粘贴的整道题里取出选项文本，按你看到的顺序。"""
    lines = text.splitlines()
    opt_idx = set(option_line_indices(lines))
    opts: list[str] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        inline = split_inline_options(s)
        if inline:
            if i in opt_idx or not opt_idx:
                opts.extend(inline)
            continue
        if i in opt_idx:
            opts.append(_OPT_LINE.match(s).group(2).strip())
    return opts


def correct_texts_of(row: sqlite3.Row) -> tuple[list[str], str]:
    """题库里那道题「正确选项的原文」。返回 (原文列表, 出错原因)。"""
    stored = json.loads(row["options"]) if row["options"] else []
    ans = norm_answer(row["answer"])
    if not stored:
        # 没有选项的题（文字答案），原文就是答案本身
        return ([row["answer"]] if row["answer"] else []), ""
    letters = [ch for ch in ans if "A" <= ch <= "H"]
    if not letters:
        return ([row["answer"]] if row["answer"] else []), ""
    if any(ord(ch) - 65 >= len(stored) for ch in letters):
        return [], "题库里这道题的选项不完整"
    return [stored[ord(ch) - 65] for ch in letters], ""


def remap_answer(row: sqlite3.Row, given: list[str]) -> tuple[str | None, list[str], str]:
    """把答案映射到**你这次看到的**选项顺序上。

    平台每次可能打乱选项，所以题库里存的字母只对当时那次顺序有效——
    直接照抄字母是错的。真正稳定的是正确答案的选项原文，按它在你给的
    选项里找位置，得到的字母才对得上你现在看的这道题。

    返回 (字母或None, 正确选项原文, 无法确定的说明)。
    """
    texts, why = correct_texts_of(row)
    if not texts:
        return None, [], why or "题库里这道题没有可用的答案原文"
    if not given:
        return None, texts, "你只给了题干没给选项，无法校对顺序"

    letters = []
    for t in texts:
        idx = _find_option(given, t)
        if idx is None:
            return None, texts, f"你给的选项里找不到「{t}」"
        letters.append(chr(65 + idx))
    return "".join(sorted(letters)), texts, ""


def _find_option(given: list[str], want: str) -> int | None:
    """在给定选项里找 `want` 的位置，三级放宽。

    第三级（按 norm 比对）是关键：题库里的选项原文来自**采集时的那个页面**，
    而你答题时看到的是**另一次渲染**，两者可能只差标点或空白
    （全角逗号与半角、连续空格、末尾句号）。严格相等会因为这种无关差异匹配失败，
    于是算不出字母、脚本只能随机猜——实测踩过「错了很多题但库里都有」。
    """
    if not given or not want:
        return None
    # 1) 完全相等
    for k, o in enumerate(given):
        if o == want:
            return k
    # 2) 去掉首尾空白后相等
    w = want.strip()
    for k, o in enumerate(given):
        if o and o.strip() == w:
            return k
    # 3) 规范化后相等（同 norm：去标点、去空白、NFKC）
    wn = norm(want)
    if wn:
        for k, o in enumerate(given):
            if o and norm(o) == wn:
                return k
        # 4) 仍未命中就退一步做包含（两者长度差得多时容易误判，所以放最后）
        for k, o in enumerate(given):
            on = norm(o)
            if on and wn and (on in wn or wn in on):
                return k
    return None


def resolve(conn: sqlite3.Connection, raw: str, min_score: float = 0.85) -> str:
    """给一段粘贴的题目文本，返回要显示的答案。命令行和交互窗口共用这一份逻辑。"""
    query = query_from_text(raw)
    given = options_from_text(raw)
    hits = [(s, k, r) for s, k, r in lookup(conn, query, 3) if s >= min_score]
    if not hits:
        return "题库无此题"

    out: list[str] = []
    for s, kind, row in hits:
        letter, texts, why = remap_answer(row, given)
        joined = " / ".join(texts)
        if letter:
            line = f"答案: {letter}  → {joined}"
            # 与题库存的字母不同，说明这次选项顺序被打乱了，提醒一句
            if norm_answer(row["answer"]) != letter:
                line += (f"   （题库里存的是 {norm_answer(row['answer'])}，"
                         "说明这次选项顺序不同，已按你看到的顺序重算）")
        else:
            line = f"选项原文: {joined}"
        out.append(f"{line}\n      [{kind} {s:.2f}] {row['stem_raw']}")
        if not letter:
            out.append(f"      !! 无法确定字母：{why}——请按上面的选项原文作答")
        # 题干太短（「下列说法错误的是」这类）撑不起唯一标识，把选项摊出来让人核对
        if len(row["stem_norm"]) < 15:
            opts = json.loads(row["options"]) if row["options"] else []
            if opts:
                out.append(f"      题干过短，可能是通用题干，核对选项是否一致: {' | '.join(opts)}")
        if len(hits) > 1:
            out.append("")
    return "\n".join(out)


def read_pasted_question(
    quit_words: tuple[str, ...] = ("q", "quit", "exit"),
) -> list[str] | None:
    """从标准输入读一道题：**读到空行为止**（EOF 也停）。返回 None 表示要退出。

    这里绝不能用 sys.stdin.read()——那是读到 EOF，在交互式终端里会一直等你
    按 Ctrl+D / Ctrl+Z，粘完题按回车毫无反应，看起来就像卡死。
    quit_words 只在第一行单独出现时才生效。
    """
    lines: list[str] = []
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            return lines or None
        if not line.strip():
            if lines:
                return lines
            if sys.stdin.isatty():
                print("  （还没输入内容——粘上题目后再按回车）")
            continue
        if not lines and line.strip().lower() in quit_words:
            return None
        lines.append(line)


def cmd_ask(args: argparse.Namespace) -> None:
    """严格查询：只在高置信命中时给答案，否则明说没有。"""
    conn = connect()
    if args.query is not None:
        raw = args.query
    else:
        if sys.stdin.isatty():
            print("粘题目（连选项一起），粘完按一下回车（那一下是空行，不会有回显）：")
        lines = read_pasted_question()
        if not lines:
            print("没有输入题目")
            return
        raw = "\n".join(lines)
    if not raw.strip():
        print("没有输入题目")
        return
    print(resolve(conn, raw, args.min))


def show(row: sqlite3.Row, s: float, kind: str = "") -> None:
    opts = json.loads(row["options"]) if row["options"] else []
    print(f"\n[{kind} {s:.2f}] #{row['id']}  {row['stem_raw']}")
    for i, o in enumerate(opts):
        print(f"    {chr(65 + i)}. {o}")
    print(f"  答案: {row['answer']}" + (f"  → {row['answer_text']}" if row["answer_text"] else ""))
    if row["explanation"]:
        print(f"  解析: {row['explanation']}")


USERJS_PATH = Path(__file__).with_name("油猴") / "yiban-bank.user.js"

_INSTALL_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>学生手册题库 · 本地服务</title></head>
<body style="font:15px/1.8 system-ui,'Microsoft YaHei',sans-serif;max-width:660px;margin:40px auto;padding:0 18px;color:#222">
<h1 style="margin-bottom:4px">本地服务在跑 ✓</h1>
<p style="color:#666;margin-top:0">这个浏览器窗口只是说明页，随时可以关。<b>但那个黑色命令行窗口别关</b>——它就是服务本身。</p>
<p>题库 <b>{n}</b> 题</p>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0">
<h2 style="font-size:17px">三步用完</h2>
<ol>
  <li>浏览器装油猴扩展（<b>Tampermonkey</b> 或 <b>Violentmonkey</b>／脚本猫都行）</li>
  <li><a href="/yiban-bank.user.js" style="font-size:17px">点这里安装脚本</a> —— 油猴会弹出安装页，点「安装」</li>
  <li>打开考试或模拟考页面，右下角会出现一个面板</li>
</ol>
{script_missing}
<h2 style="font-size:17px">怎么用</h2>
<ul>
  <li><b>自动答题</b>：从第一题开始逐题查库、点选项、自动翻页，一路答到底（不自动交卷）</li>
  <li><b>采集入库</b>：<b>交卷后</b>在试卷页点它，把整份卷子的题目和正确答案收进题库</li>
  <li><b>快速模式</b>：勾上就看不见翻页过程，快很多</li>
  <li><b>清空题库</b>：会先问一遍再让你确认；服务端删之前自动备份</li>
</ul>
<p style="color:#666;font-size:13px">「采集入库」每次都会把整份卷子的题目和答案收进来，所以多考几轮题就齐了。</p>
</body></html>"""


def clear_payload(conn: sqlite3.Connection, source: str | None, all_: bool) -> dict:
    """给油猴脚本用的清空接口。**删之前一定备份**——这个操作不可恢复。"""
    where = "1=1" if all_ else "source = ?"
    params: list = [] if all_ else [source]
    n = conn.execute(f"SELECT COUNT(*) c FROM questions WHERE {where}", params).fetchone()["c"]
    if not n:
        return {"ok": True, "deleted": 0, "note": "没有匹配的题"}
    total = conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
    if not all_ and total and n >= total * 0.9:
        # 事故护栏：2026-09-23 我用 --source script 清库，而当时所有题都来自 script，
        # 于是整个题库被删空。按来源清却清掉九成以上，几乎必然是「其实想全清」的误操作。
        return {"ok": False, "error": (
            f"拒绝执行：来源 {source!r} 有 {n} 题，占全部 {total} 题的 {n/total:.0%}，"
            "这基本等于清空整个题库。真要全清请用 clear --all")}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB_PATH.with_name(f"bank-{stamp}.bak.db")
    shutil.copy2(DB_PATH, backup)
    conn.execute(f"DELETE FROM questions WHERE {where}", params)
    conn.commit()
    left = conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
    return {"ok": True, "deleted": n, "left": left, "backup": backup.name}


def cmd_clear(args: argparse.Namespace) -> None:
    """清空题库。**一定先备份**，且默认只预览——这个操作不可恢复。

    按来源清时如果会清掉九成以上，直接拒绝（那基本是误操作）；
    真要全清用 --all。这条护栏是 2026-09-23 一次真实事故换来的。
    """
    conn = connect()
    if not args.all and not args.source:
        print("要说清清哪个（这个操作不可恢复）：")
        print("  --all              清空全部")
        print("  --source script    只清某个来源（如 script）")
        print()
        print("当前来源分布:")
        for r in conn.execute(
            "SELECT source, COUNT(*) c FROM questions GROUP BY source ORDER BY c DESC"
        ):
            print(f"  {r['source']:<16} {r['c']} 题")
        return

    where = "1=1" if args.all else "source = ?"
    params: list = [] if args.all else [args.source]
    n = conn.execute(f"SELECT COUNT(*) c FROM questions WHERE {where}", params).fetchone()["c"]
    scope = "全部" if args.all else f"来源 {args.source!r}"
    if not n:
        print(f"没有匹配的题（{scope}）")
        return

    total = conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
    if not args.all and total and n >= total * 0.9:
        print(f"拒绝执行：来源 {args.source!r} 有 {n} 题，占全部 {total} 题的 {n/total:.0%}——")
        print("这基本等于清空整个题库。真要全清请用：")
        print("    python bank.py clear --all --yes")
        return
    if not args.yes:
        print(f"将删除 {n} 题（{scope}）")
        print("加 --yes 才真删。删除前会自动备份。")
        return

    # 先备份：这个操作不可恢复
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB_PATH.with_name(f"bank-{stamp}.bak.db")
    shutil.copy2(DB_PATH, backup)
    print(f"已备份 -> {backup.name}")

    conn.execute(f"DELETE FROM questions WHERE {where}", params)
    conn.commit()
    left = conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
    print(f"已删除 {n} 题（{scope}）；题库剩余 {left} 题")
    print(f"要还原就把 {backup.name} 改名成 bank.db")


def cmd_forget(args: argparse.Namespace) -> None:
    """删除录错的题。默认只列出会删什么，加 --yes 才真删。"""
    conn = connect()
    if args.ids:
        marks = ",".join("?" * len(args.ids))
        rows = conn.execute(
            f"SELECT id, stem_raw FROM questions WHERE id IN ({marks})", args.ids
        ).fetchall()
    else:
        q = norm(args.stem)
        rows = conn.execute(
            "SELECT id, stem_raw FROM questions WHERE stem_norm LIKE ? OR stem_raw LIKE ?",
            (f"%{q}%", f"%{args.stem}%"),
        ).fetchall()
    if not rows:
        print("没找到匹配的题")
        return
    for r in rows:
        print(f"{'已删除' if args.yes else '将删除'} #{r['id']} {r['stem_raw']}")
    if args.yes:
        conn.executemany("DELETE FROM questions WHERE id = ?", [(r["id"],) for r in rows])
        conn.executemany("DELETE FROM conflicts WHERE stem_key IN"
                         " (SELECT stem_key FROM questions WHERE id = ?)",
                         [(r["id"],) for r in rows])
        conn.commit()
        print(f"共 {len(rows)} 题")
    else:
        print("\n加 --yes 才真删")


def cmd_conflicts(args: argparse.Namespace) -> None:
    conn = connect()
    rows = conn.execute("SELECT * FROM conflicts ORDER BY id DESC").fetchall()
    if not rows:
        print("没有答案冲突。")
        return
    print(f"{len(rows)} 条冲突（题库里保留的是 old 那侧的答案）:\n")
    for r in rows:
        print(f"#{r['id']} [{r['source']} {r['seen_at']}] {r['stem_raw']}")
        print(f"    已有: {r['old_answer']}   这次录入: {r['new_answer']}\n")


def stem_similarity(a: str, b: str) -> float:
    """对称相似度。不能用 match()——它允许片段包含，而泛用短题干
    （「下列说法错误的是」）是很多长题干的子串，会虚高成假重复。"""
    return difflib.SequenceMatcher(None, a, b).ratio()


def option_overlap(oa_json: str | None, ob_json: str | None) -> float | None:
    """两组选项的重合比例。这是比题干相似度更有力的判据：
    同一道题被平台改写，选项通常只微调；而正反问法的两道题，选项完全不同。"""
    oa = json.loads(oa_json) if oa_json else []
    ob = json.loads(ob_json) if ob_json else []
    if not oa or not ob:
        return None
    sa, sb = set(oa), set(ob)
    return len(sa & sb) / max(len(sa), len(sb))


def cmd_dupes(args: argparse.Namespace) -> None:
    """列出疑似重复的题目对。

    注意：文本相似度**不足以判定**是否同一道题——正反问法的两道题
    （「包括哪些」与「不包括哪些」）文本极像但确实是两道题，而同一道题被
    换个问法后可能反而没那么像。所以这里只给候选和判据，由你按内容定夺。
    """
    conn = connect()
    rows = conn.execute(
        "SELECT id, stem_raw, stem_norm, options, answer, answer_text FROM questions ORDER BY id"
    ).fetchall()
    if len(rows) < 2:
        print("题太少，无需比对")
        return

    pairs = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i]["stem_norm"], rows[j]["stem_norm"]
            ratio = stem_similarity(a, b)
            len_ratio = min(len(a), len(b)) / max(len(a), len(b))
            if ratio >= args.min and len_ratio >= 0.6:
                pairs.append((ratio, len_ratio, rows[i], rows[j]))
    pairs.sort(key=lambda x: -x[0])
    if not pairs:
        print(f"没有相似度 ≥ {args.min} 的题目对——{len(rows)} 道题看起来彼此都不同。")
        return

    print(f"疑似重复 {len(pairs)} 组（题干相似度 ≥ {args.min}，长度相近）")
    print("只是候选，不是结论。两个判据都会骗人，请对着内容看:")
    print("  · 选项文本相同 -> 强重复信号")
    print("  · 选项文本不同 -> 也可能只是平台换了选项写法（如 2 与 两年）")
    print("  · 含/不含这类正反问法，文本极像但确实是两道题\n")
    for ratio, len_ratio, a, b in pairs[: args.top]:
        ov = option_overlap(a["options"], b["options"])
        ov_txt = "无法比较" if ov is None else f"{ov:.0%}"
        rel = "同" if norm_answer(a["answer"]) == norm_answer(b["answer"]) else "异"
        print(f"[题干 {ratio:.2f}] 选项文本重合 {ov_txt}  |  答案字母 {rel}")
        print(f"   #{a['id']} [{a['answer']}] {a['stem_raw'][:66]}")
        if a["answer_text"]:
            print(f"        → {a['answer_text'][:58]}")
        print(f"   #{b['id']} [{b['answer']}] {b['stem_raw'][:66]}")
        if b["answer_text"]:
            print(f"        → {b['answer_text'][:58]}")
        print()
    if len(pairs) > args.top:
        print(f"...另有 {len(pairs) - args.top} 组，加 --top 看更多\n")
    print("判定「平台题库有重复」的硬证据是同一场考试内出现重复题——"
          "导入时会直接报出来，那个不需要判断。")


def cmd_quiz(args: argparse.Namespace) -> None:
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM questions ORDER BY wrong_count DESC, right_count ASC, RANDOM() LIMIT ?",
        (args.n,),
    ).fetchall()
    if not rows:
        print("题库是空的")
        return
    right = wrong = 0
    for i, row in enumerate(rows, 1):
        opts = json.loads(row["options"]) if row["options"] else []
        print(f"\n--- {i}/{len(rows)} ---\n{row['stem_raw']}")
        for j, o in enumerate(opts):
            print(f"  {chr(65 + j)}. {o}")
        try:
            got = input("你的答案 > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not got:
            continue
        ok = norm_answer(got) == norm_answer(row["answer"])
        right, wrong = (right + 1, wrong) if ok else (right, wrong + 1)
        print("对" if ok else f"错，正确答案: {row['answer']}")
        if row["answer_text"]:
            print(f"    → {row['answer_text']}")
        if row["explanation"]:
            print(f"    解析: {row['explanation']}")
        col = "right_count" if ok else "wrong_count"
        conn.execute(f"UPDATE questions SET {col} = {col} + 1 WHERE id = ?", (row["id"],))
    conn.commit()
    print(f"\n本轮 对 {right} / 错 {wrong}")


def cmd_stats(args: argparse.Namespace) -> None:
    conn = connect()
    row = conn.execute(
        "SELECT COUNT(*) n, SUM(times_seen) hits, SUM(times_seen = 1) once,"
        " SUM(times_seen >= 3) hot, SUM(wrong_count > 0) shaky,"
        " SUM(wrong_count > 0 AND right_count = 0) bad FROM questions"
    ).fetchone()
    conf = conn.execute("SELECT COUNT(*) c FROM conflicts").fetchone()["c"]
    print(f"题目总数      {row['n'] or 0}")
    print(f"只出现过一次  {row['once'] or 0}   <- 还没被模拟考覆盖到")
    print(f"出现 3 次以上 {row['hot'] or 0}   <- 高频题，优先背")
    print(f"累计命中      {row['hits'] or 0}")
    print(f"错过的题      {row['shaky'] or 0}")
    print(f"做过但没对过  {row['bad'] or 0}")
    print(f"答案冲突      {conf}")

    src = conn.execute(
        "SELECT source, COUNT(*) c FROM questions GROUP BY source ORDER BY c DESC LIMIT 10"
    ).fetchall()
    if src:
        print("\n按来源:")
        for r in src:
            print(f"  {r['source']:<20} {r['c']}")


def cmd_export(args: argparse.Namespace) -> None:
    conn = connect()
    rows = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()
    out = [
        {
            "id": r["id"],
            "stem": r["stem_raw"],
            "options": json.loads(r["options"]) if r["options"] else [],
            "answer": r["answer"],
            "answer_text": r["answer_text"],
            "explanation": r["explanation"],
            "times_seen": r["times_seen"],
            "wrong_count": r["wrong_count"],
        }
        for r in rows
    ]
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"导出 {len(out)} 题 → {args.out}")


def ask_payload(conn: sqlite3.Connection, text: str, min_score: float = 0.85) -> dict:
    """给油猴脚本用的查询接口。返回 JSON 友好的结构。"""
    query = query_from_text(text)
    given = options_from_text(text)
    hits = [(s, k, r) for s, k, r in lookup(conn, query, 3) if s >= min_score]
    if not hits:
        return {"found": False, "reason": "题库无此题"}
    cands = []
    for s, kind, row in hits:
        letter, texts, why = remap_answer(row, given)
        cands.append(
            {
                "letter": letter,
                "texts": texts,
                "why": why,
                "stem": row["stem_raw"],
                "score": round(s, 3),
                "kind": kind,
                "stored_answer": norm_answer(row["answer"]),
            }
        )
    # 能把字母算出来的候选排前面——那些才是选项对得上的。脚本直接取 best 即可，
    # 不用自己判断，否则会拿到同题干但选项对不上的另一道题。
    cands.sort(key=lambda c: (c["letter"] is None, -c["score"]))
    return {"found": True, "best": next((c for c in cands if c["letter"]), None), "candidates": cands}


def learn_payload(conn: sqlite3.Connection, text: str, source: str) -> dict:
    """给油猴脚本用的入库接口：把采集到的整段卷面文本解析入库。"""
    records, failures = parse_text(text)
    tally, within, across = run_import(conn, [(r, source) for r in records])
    conn.commit()
    return {
        "ok": True,
        "parsed": len(records),
        "tally": tally,
        "within_batch_dupes": within,
        "cross_dupes": across,
        "failures": failures,
    }


def cmd_serve(args: argparse.Namespace) -> None:
    """跑一个只监听本机的桥接服务，供油猴脚本查询和入库。

    只绑 127.0.0.1，外网访问不到。每个请求单独开连接——sqlite 连接不能跨线程共用。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    connect().close()  # 先把表建好，之后每个请求各开各的连接
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, obj, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:  # 预检请求
            self._send({"ok": True})

        def do_GET(self) -> None:
            if self.path.startswith("/health"):
                with lock, connect() as c:
                    n = c.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
                self._send({"ok": True, "questions": n})
            elif self.path.startswith("/yiban-bank.user.js"):
                # 让油猴直接从本地地址安装，省掉「复制一整份代码粘进去」这一步。
                # 走 http 而不是 file://：扩展默认没有读本地文件的权限。
                if not USERJS_PATH.exists():
                    self._send({"ok": False, "error": f"找不到脚本：{USERJS_PATH}"}, 404)
                    return
                body = USERJS_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path in ("/", "/index.html"):
                with lock, connect() as c:
                    n = c.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
                note = ("" if USERJS_PATH.exists()
                        else '<p style="color:#c00">⚠ 没找到 油猴/yiban-bank.user.js'
                             '——脚本文件不在，装不了。确认解压出来的目录是完整的。</p>')
                html = (_INSTALL_PAGE.replace("{n}", str(n))
                                    .replace("{script_missing}", note))
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send({"ok": False, "error": "unknown path"}, 404)

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8", "replace") or "{}")
            except Exception as exc:
                self._send({"ok": False, "error": f"bad request: {exc!r}"}, 400)
                return
            text = payload.get("text") or ""
            try:
                with lock, connect() as c:
                    if self.path.startswith("/ask-batch"):
                        # 批量查询：油猴脚本一次把整份卷子发过来。
                        # 每题一次请求的话，每次 await 都会让浏览器有机会重绘——
                        # 那正是「看得见翻页」的根源；合并成一次就没有中间等待了。
                        # 注意这条必须排在 "/ask" 之前判断（startswith 会把 /ask-batch 也算进 /ask）。
                        items = payload.get("items") or []
                        results = [
                            ask_payload(c, str(t), float(payload.get("min", 0.85)))
                            for t in items
                        ]
                        self._send({"ok": True, "results": results})
                    elif self.path.startswith("/ask"):
                        # 「text 不能为空」只对 /ask 和 /learn 有意义；
                        # 放在分发之前会把不带 text 的 /clear 也拦掉（踩过）。
                        if not text.strip():
                            self._send({"ok": False, "error": "empty text"}, 400)
                        else:
                            self._send(ask_payload(c, text, float(payload.get("min", 0.85))))
                    elif self.path.startswith("/learn"):
                        if not text.strip():
                            self._send({"ok": False, "error": "empty text"}, 400)
                        else:
                            self._send(learn_payload(c, text, payload.get("source") or "script"))
                    elif self.path.startswith("/clear"):
                        # 必须显式带 confirm，避免误触发；备份在 clear_payload 里做
                        if not payload.get("confirm"):
                            self._send({"ok": False, "error": "需要 confirm: true"}, 400)
                        else:
                            self._send(clear_payload(
                                c, payload.get("source"), bool(payload.get("all"))
                            ))
                    else:
                        self._send({"ok": False, "error": "unknown path"}, 404)
            except Exception as exc:
                self._send({"ok": False, "error": repr(exc)}, 500)

        def log_message(self, *a) -> None:  # 别刷屏
            pass

    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"启动失败：端口 {args.port} 用不了（{exc}）。")
        print("多半是已经开着一个了——看看是不是还有别的这种黑窗口。")
        print("要么先关掉那个，要么换个端口：python bank.py serve --port 8766")
        return
    banner = [
        "=" * 60,
        "  题库服务已经跑起来了",
        "",
        f"  在浏览器打开： http://{args.host}:{args.port}/",
        "  那个页面会带你装脚本，并说明怎么用。",
        "",
        "  【这个窗口别关】——关了服务就停了。要停止就关掉它。",
        "=" * 60,
        "",
        f"  题库文件: {DB_PATH}",
        f"  脚本文件: {USERJS_PATH}" + ("" if USERJS_PATH.exists() else "  ← 不存在，装不了脚本"),
    ]
    print(chr(10).join(banner), flush=True)   # 重定向到文件时也立刻可见
    print("\nCtrl+C 结束。")
    if args.open:
        try:
            import webbrowser
            webbrowser.open(f"http://{args.host}:{args.port}/")
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    p = argparse.ArgumentParser(description="学生手册题库")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("find", help="宽松检索，列候选")
    s.add_argument("query")
    s.add_argument("--top", type=int, default=5)
    s.add_argument("--min", type=float, default=0.5)
    s.set_defaults(func=cmd_find)

    s = sub.add_parser("ask", help="严格查询；不给题干就从标准输入读整道题")
    s.add_argument("query", nargs="?", default=None)
    s.add_argument("--min", type=float, default=0.85)
    s.set_defaults(func=cmd_ask)

    s = sub.add_parser("conflicts", help="查看答案冲突")
    s.set_defaults(func=cmd_conflicts)

    s = sub.add_parser("clear", help="清空题库（先备份；默认只预览）")
    s.add_argument("--all", action="store_true", help="清空全部")
    s.add_argument("--source", default=None, help="只清某个来源，如 script")
    s.add_argument("--yes", action="store_true", help="确认删除")
    s.set_defaults(func=cmd_clear)

    s = sub.add_parser("forget", help="删除录错的题（默认只预览）")
    s.add_argument("ids", nargs="*", type=int)
    s.add_argument("--stem", default="", help="按题干片段删")
    s.add_argument("--yes", action="store_true", help="确认删除")
    s.set_defaults(func=cmd_forget)

    s = sub.add_parser("dupes", help="列出疑似重复的题目对（候选，需人工判断）")
    s.add_argument("--min", type=float, default=0.75)
    s.add_argument("--top", type=int, default=20)
    s.set_defaults(func=cmd_dupes)

    s = sub.add_parser("quiz", help="刷题，错题优先")
    s.add_argument("--n", type=int, default=20)
    s.set_defaults(func=cmd_quiz)

    s = sub.add_parser("stats", help="题库概况")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("serve", help="启动本地桥接服务，供油猴脚本查询/入库")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="启动后自动打开安装页")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("export", help="导出为 JSON")
    s.add_argument("--out", default="bank.json")
    s.set_defaults(func=cmd_export)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
