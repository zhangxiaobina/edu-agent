"""合成教学库生成器（可复现）。

设计要点：
- 固定随机种子 → 同一种子产出字节级一致的库，满足「每步可复现、不编数」。
- 所有时间基于固定的学期起始日 BASE_DATE 偏移计算，绝不调用 now()，保证复现。
- 学生有隐变量 ability，题目有 difficulty；作答正确与否 = f(ability, difficulty)，
  因此「成绩 / 错题 / 知识点掌握度 / 学习路径」四者内部一致、互相印证。
- 课程的知识图谱按真实课程大纲手工编排（chapter→concept→skill），
  再程序化补 先修/相关/相似 边，mirror Neo4j 设计文档（单 :KnowledgePoint 标签 + 4 类关系）。
- 红线：内容 100% 合成；真实学生数据绝不进入。

运行：python -m edu_agent.data.generate  [--seed 42] [--out path.db]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sqlite3
import uuid
from pathlib import Path

from . import db

# ---------------------------------------------------------------------------
# 常量与可复现锚点
# ---------------------------------------------------------------------------
BASE_DATE = dt.datetime(2025, 9, 1, 8, 0, 0)  # 学期起始；所有时间由此偏移
UUID_NS = uuid.UUID("12345678-1234-5678-1234-567812345678")  # 固定命名空间→确定性 node_uid

SURNAMES = list("王李张刘陈杨赵黄周吴徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭")
GIVEN = list("伟芳娜秀英敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂兰志宇浩然子轩欣怡梓涵一诺")

DIFFICULTY_PENALTY = {"easy": 0.0, "medium": 0.10, "hard": 0.24}
QTYPE_WEIGHTS = [("single", 0.45), ("multiple", 0.2), ("judge", 0.15), ("fill", 0.1), ("coding", 0.1)]


def _fmt(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 课程大纲（手工编排的真实课程结构 → 知识图谱）
# concepts 按学习顺序排列；hard 标记的 concept 题目更难、班级错误率更高。
# ---------------------------------------------------------------------------
CURRICULA = [
    {
        "name": "Python程序设计",
        "chapters": [
            {"name": "Python基础与数据类型", "concepts": ["变量与赋值", "数值类型", "字符串操作", "布尔与比较"]},
            {"name": "流程控制", "concepts": ["条件语句", "for循环", "while循环", "break与continue"]},
            {"name": "函数", "concepts": ["函数定义", "参数与返回值", "变量作用域", "递归", "lambda与高阶函数"]},
            {"name": "容器类型", "concepts": ["列表list", "元组tuple", "字典dict", "集合set", "列表推导式"]},
            {"name": "文件与异常", "concepts": ["文件读写", "异常处理", "with上下文管理"]},
            {"name": "面向对象", "concepts": ["类与对象", "继承", "多态", "魔术方法"]},
        ],
        "hard": ["递归", "lambda与高阶函数", "列表推导式", "继承", "多态", "魔术方法"],
    },
    {
        "name": "数据结构",
        "chapters": [
            {"name": "线性表", "concepts": ["数组", "单链表", "双向链表"]},
            {"name": "栈与队列", "concepts": ["栈stack", "队列queue", "循环队列"]},
            {"name": "树", "concepts": ["二叉树", "二叉搜索树", "平衡树AVL", "堆heap"]},
            {"name": "查找与散列", "concepts": ["顺序查找", "二分查找", "哈希表"]},
            {"name": "排序", "concepts": ["冒泡排序", "快速排序", "归并排序"]},
            {"name": "图", "concepts": ["图的表示", "深度优先DFS", "广度优先BFS", "最短路径"]},
        ],
        "hard": ["平衡树AVL", "快速排序", "归并排序", "最短路径", "堆heap"],
    },
    {
        "name": "计算机网络",
        "chapters": [
            {"name": "网络体系结构", "concepts": ["OSI七层模型", "TCP/IP模型", "封装与分用"]},
            {"name": "传输层", "concepts": ["UDP协议", "TCP协议", "三次握手", "拥塞控制"]},
            {"name": "网络层", "concepts": ["IP地址", "子网划分", "路由选择"]},
            {"name": "应用层", "concepts": ["HTTP协议", "DNS解析", "HTTPS与TLS"]},
        ],
        "hard": ["拥塞控制", "三次握手", "子网划分", "HTTPS与TLS"],
    },
]


# ---------------------------------------------------------------------------
# 生成器主体
# ---------------------------------------------------------------------------
class Generator:
    def __init__(self, rng: random.Random, *, n_students: int = 140,
                 class_names: list[str] | None = None, courses_per_class: int = 2):
        self.rng = rng
        self.conn: sqlite3.Connection | None = None
        # 扩展参数：默认 = 冻结的 seed-42 基准库；扩库（DPO 用）时显式放大。
        self.n_students = n_students
        self.class_names = class_names
        self.courses_per_class = courses_per_class
        # 缓存便于交叉引用
        self.teachers: list[dict] = []
        self.students: list[dict] = []
        self.courses: list[dict] = []
        self.classes: list[dict] = []
        self.student_ability: dict[int, float] = {}
        self.course_nodes: dict[int, list[dict]] = {}   # course_id -> kg nodes
        self.course_concepts: dict[int, list[dict]] = {}  # course_id -> concept/skill nodes only
        self.question_concept: dict[int, str] = {}       # question_id -> primary node_uid
        self.next_q_id = 1

    # -- 工具方法 --
    def _name(self) -> str:
        n = self.rng.choice(SURNAMES) + self.rng.choice(GIVEN)
        if self.rng.random() < 0.4:
            n += self.rng.choice(GIVEN)
        return n

    def node_uid(self, course_id: int, ntype: str, idx: int) -> str:
        return str(uuid.uuid5(UUID_NS, f"{course_id}:{ntype}:{idx}"))

    # -- 各实体生成 --
    def gen_teachers(self, n: int = 6):
        for i in range(1, n + 1):
            t = {"id": i, "username": f"T{1000 + i}", "name": self._name() + "老师"}
            self.teachers.append(t)
        self.conn.executemany(
            "INSERT INTO teachers(id,username,name) VALUES(:id,:username,:name)", self.teachers
        )

    def gen_students(self, n: int = 140):
        for i in range(1, n + 1):
            sid = i
            sname = self._name()
            s = {
                "id": sid,
                "username": f"2024{i:04d}",
                "name": sname,
                "phone": f"138{self.rng.randint(0, 99999999):08d}",
                "email": f"stu{i:04d}@example.edu",  # 合成域名
            }
            self.students.append(s)
            # ability：班内能力分布，clamp 到 [0.30, 0.95]
            self.student_ability[sid] = min(0.95, max(0.30, self.rng.gauss(0.70, 0.14)))
        self.conn.executemany(
            "INSERT INTO students(id,username,name,phone,email) VALUES(:id,:username,:name,:phone,:email)",
            self.students,
        )

    def gen_courses(self):
        for idx, cur in enumerate(CURRICULA, start=1):
            teacher = self.teachers[(idx - 1) % len(self.teachers)]
            c = {
                "id": idx,
                "name": cur["name"],
                "teacher_id": teacher["id"],
                "description": f"{cur['name']}课程（合成示例数据）",
                "_curriculum": cur,
            }
            self.courses.append(c)
            self.conn.execute(
                "INSERT INTO courses(id,name,teacher_id,description) VALUES(?,?,?,?)",
                (c["id"], c["name"], c["teacher_id"], c["description"]),
            )

    def gen_classes(self, names: list[str] | None = None, courses_per_class: int = 2):
        # 默认 4 个班级；确保存在「3班」并选修 Python（demo 锚点）。names=None 即冻结正库。
        if names is None:
            names = ["计科2024-1班", "计科2024-2班", "计科2024-3班", "软件2024-1班"]
        sid_pool = [s["id"] for s in self.students]
        self.rng.shuffle(sid_pool)
        per = len(sid_pool) // len(names)
        for ci, cname in enumerate(names, start=1):
            teacher = self.teachers[ci % len(self.teachers)]
            cls = {"id": ci, "name": cname, "teacher_id": teacher["id"], "student_ids": []}
            self.classes.append(cls)
            self.conn.execute(
                "INSERT INTO classes(id,name,teacher_id) VALUES(?,?,?)",
                (cls["id"], cls["name"], cls["teacher_id"]),
            )
            members = sid_pool[(ci - 1) * per : ci * per]
            cls["student_ids"] = members
            for sid in members:
                jt = _fmt(BASE_DATE + dt.timedelta(days=self.rng.randint(0, 5)))
                self.conn.execute(
                    "INSERT INTO class_students(class_id,student_id,join_time,status) VALUES(?,?,?,1)",
                    (ci, sid, jt),
                )
            # 班级选修课程：默认每班选 2 门；3 班必含 Python(course 1)。扩库时 courses_per_class 放大。
            if cname == "计科2024-3班":
                course_ids = [1, 2]
            else:
                n_pick = min(courses_per_class, len(self.courses))
                course_ids = sorted(self.rng.sample([c["id"] for c in self.courses], n_pick))
            cls["course_ids"] = course_ids
            for course_id in course_ids:
                self.conn.execute(
                    "INSERT INTO class_courses(class_id,course_id) VALUES(?,?)", (ci, course_id)
                )

    def gen_knowledge_graph(self):
        """按大纲构建 KG 节点与边（mirror Neo4j 设计）。"""
        edge_id = 1
        for course in self.courses:
            cid = course["id"]
            cur = course["_curriculum"]
            graph_id = cid
            nodes: list[dict] = []
            concept_seq: list[dict] = []  # 用于排先修链
            hard = set(cur["hard"])

            # 课程级 topic 节点（综合应用）
            topic_uid = self.node_uid(cid, "topic", 0)
            topic = {"node_uid": topic_uid, "graph_id": graph_id, "course_id": cid,
                     "name": f"{course['name']}综合应用", "description": "跨章节综合应用主题",
                     "type": "topic", "difficulty": 4, "importance": 0.9,
                     "status": "active", "source": "manual"}
            nodes.append(topic)
            self._insert_node(topic)

            for ch_idx, ch in enumerate(cur["chapters"], start=1):
                ch_uid = self.node_uid(cid, "chapter", ch_idx)
                ch_node = {"node_uid": ch_uid, "graph_id": graph_id, "course_id": cid,
                           "name": ch["name"], "description": f"第{ch_idx}章 {ch['name']}",
                           "type": "chapter", "difficulty": None, "importance": 0.6,
                           "status": "active", "source": "manual"}
                nodes.append(ch_node)
                self._insert_node(ch_node)
                chapter_concepts = []
                for co_idx, cname in enumerate(ch["concepts"], start=1):
                    c_uid = self.node_uid(cid, f"concept-{ch_idx}", co_idx)
                    diff = 4 if cname in hard else self.rng.choice([2, 3])
                    node = {"node_uid": c_uid, "graph_id": graph_id, "course_id": cid,
                            "name": cname, "description": f"{ch['name']} - {cname}",
                            "type": "concept", "difficulty": diff,
                            "importance": round(self.rng.uniform(0.4, 0.95), 2),
                            "status": "active", "source": "manual", "_hard": cname in hard}
                    nodes.append(node)
                    self._insert_node(node)
                    concept_seq.append(node)
                    chapter_concepts.append(node)
                    # PART_OF：concept → chapter
                    self._edge(edge_id, graph_id, cid, "PART_OF", c_uid, ch_uid, 0.85)
                    edge_id += 1
                # 每章一个 skill 节点：运用<章节>
                sk_uid = self.node_uid(cid, "skill", ch_idx)
                skill = {"node_uid": sk_uid, "graph_id": graph_id, "course_id": cid,
                         "name": f"运用「{ch['name']}」解题", "description": "本章综合解题能力",
                         "type": "skill", "difficulty": 4, "importance": 0.85,
                         "status": "active", "source": "manual"}
                nodes.append(skill)
                self._insert_node(skill)
                self._edge(edge_id, graph_id, cid, "PART_OF", sk_uid, ch_uid, 0.8)
                edge_id += 1
                # 章内最后一个 concept → skill（先修）
                if chapter_concepts:
                    self._edge(edge_id, graph_id, cid, "PREREQUISITE_OF",
                               chapter_concepts[-1]["node_uid"], sk_uid, 0.8)
                    edge_id += 1
                    # skill → topic（汇入综合应用）
                    self._edge(edge_id, graph_id, cid, "PREREQUISITE_OF", sk_uid, topic_uid,
                               round(self.rng.uniform(0.5, 0.7), 2))
                    edge_id += 1

            # 先修链：concept_seq 顺序相邻 → PREREQUISITE_OF
            for a, b in zip(concept_seq, concept_seq[1:]):
                w = round(self.rng.uniform(0.6, 0.9), 2)
                self._edge(edge_id, graph_id, cid, "PREREQUISITE_OF", a["node_uid"], b["node_uid"], w)
                edge_id += 1
            # 相关 / 相似 边：随机挑若干对
            for _ in range(max(3, len(concept_seq) // 4)):
                a, b = self.rng.sample(concept_seq, 2)
                rel = self.rng.choice(["RELATED_TO", "SIMILAR_TO"])
                self._edge(edge_id, graph_id, cid, rel, a["node_uid"], b["node_uid"],
                           round(self.rng.uniform(0.4, 0.6), 2))
                edge_id += 1

            # 落库（节点已在创建时即时插入；此处仅缓存便于交叉引用）
            self.course_nodes[cid] = nodes
            self.course_concepts[cid] = [n for n in nodes if n["type"] in ("concept", "skill")]

    def _insert_node(self, node: dict):
        self.conn.execute(
            """INSERT INTO kg_nodes(node_uid,graph_id,course_id,name,description,type,
               difficulty,importance,status,source)
               VALUES(:node_uid,:graph_id,:course_id,:name,:description,:type,
               :difficulty,:importance,:status,:source)""",
            {k: v for k, v in node.items() if not k.startswith("_")},
        )

    def _edge(self, eid, graph_id, course_id, rel, start_uid, end_uid, weight, source="manual"):
        self.conn.execute(
            """INSERT INTO kg_edges(id,graph_id,course_id,rel_type,start_uid,end_uid,weight,source)
               VALUES(?,?,?,?,?,?,?,?)""",
            (eid, graph_id, course_id, rel, start_uid, end_uid, weight, source),
        )

    def gen_questions(self):
        """每门课建题库；每个 concept 出 2-4 题，并经 kg_resource_link 关联到该 concept。"""
        for course in self.courses:
            cid = course["id"]
            teacher_id = course["teacher_id"]
            bank_id = cid  # 一门课一个主题库
            self.conn.execute(
                "INSERT INTO question_banks(id,name,course_id,creator_id) VALUES(?,?,?,?)",
                (bank_id, f"{course['name']}题库", cid, teacher_id),
            )
            concepts = [n for n in self.course_nodes[cid] if n["type"] == "concept"]
            for node in concepts:
                hard = node.get("_hard", False)
                n_q = self.rng.randint(3, 5)
                for _ in range(n_q):
                    qid = self.next_q_id
                    self.next_q_id += 1
                    qtype = self._weighted_qtype()
                    difficulty = "hard" if hard and self.rng.random() < 0.6 else \
                        self.rng.choices(["easy", "medium", "hard"], weights=[0.3, 0.5, 0.2])[0]
                    options, answer = self._make_options(qtype)
                    score = {"easy": 4, "medium": 5, "hard": 8}[difficulty]
                    lang = "python" if qtype == "coding" else None
                    self.conn.execute(
                        """INSERT INTO questions(id,title,content,question_type,difficulty,options,
                           correct_answer,explanation,score,source,status,creator_id,language,
                           usage_count,course_id)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (qid, f"【{node['name']}】{qtype}题{qid}",
                         f"关于「{node['name']}」的{difficulty}难度{qtype}题（合成）。",
                         qtype, difficulty, json.dumps(options, ensure_ascii=False) if options else None,
                         answer, f"考查知识点：{node['name']}。", score, "manual", 1,
                         teacher_id, lang, self.rng.randint(0, 50), cid),
                    )
                    self.conn.execute(
                        "INSERT INTO question_bank_questions(question_bank_id,question_id) VALUES(?,?)",
                        (bank_id, qid),
                    )
                    # kg_resource_link：question tests concept
                    self.conn.execute(
                        """INSERT INTO kg_resource_link(course_id,node_uid,resource_type,resource_id,
                           link_type,weight) VALUES(?,?,?,?,?,?)""",
                        (cid, node["node_uid"], "question", qid, "tests", 1.0),
                    )
                    self.question_concept[qid] = node["node_uid"]

    def _weighted_qtype(self) -> str:
        types, weights = zip(*QTYPE_WEIGHTS)
        return self.rng.choices(types, weights=weights)[0]

    def _make_options(self, qtype: str):
        if qtype == "single":
            return (["选项A", "选项B", "选项C", "选项D"], self.rng.choice(["A", "B", "C", "D"]))
        if qtype == "multiple":
            picks = sorted(self.rng.sample(["A", "B", "C", "D"], self.rng.randint(2, 3)))
            return (["选项A", "选项B", "选项C", "选项D"], ",".join(picks))
        if qtype == "judge":
            return (["正确", "错误"], self.rng.choice(["正确", "错误"]))
        if qtype == "fill":
            return (None, "参考答案")
        return (None, "# 参考实现\npass")  # coding

    def gen_courseware_and_progress(self):
        cw_id = 1
        for course in self.courses:
            cid = course["id"]
            chapters = [n for n in self.course_nodes[cid] if n["type"] == "chapter"]
            cw_ids = []
            for order, ch in enumerate(chapters, start=1):
                self.conn.execute(
                    "INSERT INTO courseware(id,course_id,name,type,duration,sort_order) VALUES(?,?,?,?,?,?)",
                    (cw_id, cid, f"{ch['name']}·讲解视频", "video", self.rng.randint(20, 60), order),
                )
                cw_ids.append(cw_id)
                cw_id += 1
            # 选修该课程的学生 → 学习进度
            students = self._students_in_course(cid)
            for sid in students:
                ability = self.student_ability[sid]
                for order, cwid in enumerate(cw_ids):
                    # 越靠前的课件完成度越高；能力高的学生进度更靠前
                    base = ability - 0.12 * order + self.rng.uniform(-0.1, 0.1)
                    prog = int(min(100, max(0, base * 100)))
                    completed = 1 if prog >= 95 else 0
                    status = "completed" if completed else ("in_progress" if prog > 0 else "not_started")
                    dur = self.rng.randint(20, 60)
                    watched = int(dur * prog / 100)
                    t0 = BASE_DATE + dt.timedelta(days=order * 7 + self.rng.randint(0, 4))
                    self.conn.execute(
                        """INSERT INTO learning_progress(student_id,course_id,courseware_id,progress,
                           completed,watched_time,last_position,study_status,start_time,last_access_time)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (sid, cid, cwid, prog, completed, watched, watched,
                         status, _fmt(t0), _fmt(t0 + dt.timedelta(hours=2))),
                    )

    def _students_in_course(self, course_id: int) -> list[int]:
        sids: list[int] = []
        for cls in self.classes:
            if course_id in cls["course_ids"]:
                sids.extend(cls["student_ids"])
        return sorted(set(sids))

    def gen_exams_and_records(self):
        exam_id = 1
        record_id = 1
        answer_id = 1
        for cls in self.classes:
            for course_id in cls["course_ids"]:
                course = self.courses[course_id - 1]
                # 选 ~14 道题：3 道 hard（集中错题）+ 其余从 easy/medium 抽，控制难度配比
                bank_qs = self._course_questions(course_id)
                hard_qs = [q for q in bank_qs if q["difficulty"] == "hard"]
                easy_med = [q for q in bank_qs if q["difficulty"] != "hard"]
                chosen = self.rng.sample(hard_qs, min(3, len(hard_qs)))
                chosen += self.rng.sample(easy_med, min(11, len(easy_med)))
                self.rng.shuffle(chosen)
                total = sum(q["score"] for q in chosen)
                pass_score = round(total * 0.6, 1)
                start = BASE_DATE + dt.timedelta(days=70 + exam_id, hours=2)
                end = start + dt.timedelta(minutes=90)
                self.conn.execute(
                    """INSERT INTO exams(id,exam_name,exam_code,description,class_id,course_id,
                       question_bank_id,creator_id,start_time,end_time,duration,total_score,
                       pass_score,question_count,status)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (exam_id, f"{course['name']}期中考试-{cls['name']}", f"EX{exam_id:04d}",
                     f"{cls['name']} {course['name']} 期中测验（合成）", cls["id"], course_id,
                     course_id, course["teacher_id"], _fmt(start), _fmt(end), 90,
                     total, pass_score, len(chosen), 2),  # status=2 已结束（已有成绩）
                )
                for order, q in enumerate(chosen, start=1):
                    self.conn.execute(
                        "INSERT INTO exam_questions(exam_id,question_id,score,sort_order) VALUES(?,?,?,?)",
                        (exam_id, q["id"], q["score"], order),
                    )
                # 每个在班学生作答
                scored: list[tuple[int, float]] = []
                for sid in cls["student_ids"]:
                    ability = self.student_ability[sid]
                    earned_total = 0.0
                    correct = 0
                    rid = record_id
                    answer_rows = []
                    for q in chosen:
                        penalty = DIFFICULTY_PENALTY[q["difficulty"]]
                        p = min(0.96, max(0.04, ability - penalty + self.rng.uniform(-0.08, 0.08)))
                        is_correct = 1 if self.rng.random() < p else 0
                        earned = q["score"] if is_correct else 0.0
                        earned_total += earned
                        correct += is_correct
                        ans = q["correct_answer"] if is_correct else "（错误作答）"
                        answer_rows.append(
                            (answer_id, rid, exam_id, sid, q["id"], ans, is_correct, earned, q["score"])
                        )
                        answer_id += 1
                    passed = 1 if earned_total >= pass_score else 0
                    dur = self.rng.randint(45, 90)
                    st = start + dt.timedelta(minutes=self.rng.randint(0, 5))
                    # 先插记录（被 exam_answers 外键引用），再批量插逐题作答
                    self.conn.execute(
                        """INSERT INTO exam_records(id,exam_id,student_id,score,total_score,
                           correct_count,answer_count,status,start_time,submit_time,duration,passed,rank)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (rid, exam_id, sid, round(earned_total, 1), total, correct, len(chosen),
                         3, _fmt(st), _fmt(st + dt.timedelta(minutes=dur)), dur, passed, None),
                    )
                    self.conn.executemany(
                        """INSERT INTO exam_answers(id,record_id,exam_id,student_id,question_id,
                           student_answer,is_correct,earned_score,max_score)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        answer_rows,
                    )
                    scored.append((rid, earned_total))
                    record_id += 1
                # 排名
                for rank, (rid, _) in enumerate(sorted(scored, key=lambda x: -x[1]), start=1):
                    self.conn.execute("UPDATE exam_records SET rank=? WHERE id=?", (rank, rid))
                exam_id += 1

    def _course_questions(self, course_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id,score,difficulty,correct_answer FROM questions WHERE course_id=?", (course_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def gen_wrong_questions_and_stats(self):
        """从 exam_answers 派生错题本 + 学生知识点掌握度（保证四者一致）。"""
        # 错题本：聚合每个学生每题的错误次数
        self.conn.execute(
            """INSERT INTO wrong_questions(student_id,question_id,course_id,times_wrong,last_wrong_time)
               SELECT ea.student_id, ea.question_id, q.course_id, COUNT(*) AS tw,
                      MAX(er.submit_time)
               FROM exam_answers ea
               JOIN questions q ON q.id = ea.question_id
               JOIN exam_records er ON er.id = ea.record_id
               WHERE ea.is_correct = 0
               GROUP BY ea.student_id, ea.question_id, q.course_id"""
        )
        # 知识点掌握度：按题目关联的 concept 聚合正确率
        rows = self.conn.execute(
            """SELECT ea.student_id, krl.node_uid, kn.course_id,
                      SUM(ea.is_correct) AS correct, COUNT(*) AS total
               FROM exam_answers ea
               JOIN kg_resource_link krl
                 ON krl.resource_type='question' AND krl.resource_id = ea.question_id
               JOIN kg_nodes kn ON kn.node_uid = krl.node_uid
               GROUP BY ea.student_id, krl.node_uid, kn.course_id"""
        ).fetchall()
        synced = _fmt(BASE_DATE + dt.timedelta(days=80))
        for r in rows:
            total = r["total"] or 0
            correct = r["correct"] or 0
            mastery = round(correct / total, 3) if total else 0.0
            self.conn.execute(
                """INSERT INTO student_knowledge_stats(student_id,node_uid,course_id,mastery_rate,
                   correct_count,total_questions,last_synced_at) VALUES(?,?,?,?,?,?,?)""",
                (r["student_id"], r["node_uid"], r["course_id"], mastery, correct, total, synced),
            )

    def gen_homeworks(self):
        hw_id = 1
        for course in self.courses:
            cid = course["id"]
            related_classes = [c for c in self.classes if cid in c["course_ids"]]
            if not related_classes:
                continue
            start = BASE_DATE + dt.timedelta(days=30 + hw_id)
            end = start + dt.timedelta(days=7)
            self.conn.execute(
                """INSERT INTO homeworks(id,title,homework_type,description,course_id,creator_id,
                   start_time,end_time,total_score,max_submissions,status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (hw_id, f"{course['name']}第{hw_id}次作业", "open",
                 f"{course['name']}章节练习（合成）", cid, course["teacher_id"],
                 _fmt(start), _fmt(end), 100, 3, "PUBLISHED"),
            )
            for cls in related_classes:
                self.conn.execute(
                    "INSERT INTO homework_classes(homework_id,class_id) VALUES(?,?)", (hw_id, cls["id"])
                )
            hw_id += 1

    def run(self, conn: sqlite3.Connection):
        self.conn = conn
        self.gen_teachers()
        self.gen_students(self.n_students)
        self.gen_courses()
        self.gen_classes(self.class_names, self.courses_per_class)
        self.gen_knowledge_graph()
        self.gen_questions()
        self.gen_courseware_and_progress()
        self.gen_exams_and_records()
        self.gen_wrong_questions_and_stats()
        self.gen_homeworks()
        conn.commit()


def build(seed: int = 42, out_path: str | None = None, *, n_classes: int = 4,
          n_students: int | None = None, courses_per_class: int = 2) -> Path:
    """生成合成库到 out_path（默认包内 edu.db），返回路径。

    默认参数 = 冻结的 seed-42 基准库（逐字节不变）。扩库（DPO 用）时显式放大
    n_classes/courses_per_class 并写到独立 out_path，绝不覆盖基准库。
    """
    # 班级名：默认 None → gen_classes 用冻结的 4 个；扩库时按 n_classes 生成（2025 年份避开 demo 锚点名）。
    class_names = None
    if n_classes != 4:
        class_names = [f"计科2025-{i}班" for i in range(1, n_classes + 1)]
    # 学生数：默认按 35/班 估，至少 140；n_classes=4 → 140（与基准一致）。
    if n_students is None:
        n_students = max(140, n_classes * 35)
    target = Path(out_path) if out_path else db.db_path()
    if target.exists():
        target.unlink()
    conn = db.connect(target)
    try:
        db.init_schema(conn)
        Generator(random.Random(seed), n_students=n_students,
                  class_names=class_names, courses_per_class=courses_per_class).run(conn)
    finally:
        conn.close()
    return target


def _summary(path: Path) -> str:
    conn = db.connect(path)
    tables = ["teachers", "students", "courses", "classes", "questions", "kg_nodes",
              "kg_edges", "exams", "exam_records", "exam_answers", "wrong_questions",
              "student_knowledge_stats", "learning_progress", "homeworks"]
    lines = []
    for t in tables:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        lines.append(f"  {t:24s} {n}")
    conn.close()
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="生成合成教学库（可复现）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=str, default=None)
    ap.add_argument("--n-classes", type=int, default=4,
                    help="班级数（默认 4 = 冻结基准库；扩库 DPO 时放大，须配 --out 写独立库）")
    ap.add_argument("--n-students", type=int, default=None,
                    help="学生数（默认按 35/班 估，至少 140）")
    ap.add_argument("--courses-per-class", type=int, default=2,
                    help="每班选课数（默认 2；扩库时设 3 = 选满全部课程，最大化 班×课 锚点）")
    args = ap.parse_args()
    path = build(seed=args.seed, out_path=args.out, n_classes=args.n_classes,
                 n_students=args.n_students, courses_per_class=args.courses_per_class)
    print(f"✓ 合成教学库已生成: {path}  (seed={args.seed}, n_classes={args.n_classes})")
    print(_summary(path))


if __name__ == "__main__":
    main()
