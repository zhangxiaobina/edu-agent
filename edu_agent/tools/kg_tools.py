"""知识图谱工具：图谱查询 + 学习路径推荐（结合学生掌握度与图谱前置关系）。"""
from __future__ import annotations

import sqlite3

from ..data import kg
from .query_tools import search_questions

_NODE_FIELDS = ("node_uid", "name", "type", "difficulty", "importance", "course_id")


def _clean(node: dict) -> dict:
    out = {k: node[k] for k in _NODE_FIELDS if k in node}
    for extra in ("_rel_type", "_weight", "_direction"):
        if extra in node:
            out[extra.lstrip("_")] = node[extra]
    return out


def query_knowledge_graph(conn: sqlite3.Connection, course_id, operation,
                          node=None, target=None, node_type=None, name=None) -> dict:
    g = kg.KnowledgeGraph.from_sqlite(conn, course_id=course_id)
    if not g.nodes:
        return {"error": f"课程 {course_id} 无知识图谱数据"}

    if operation == "find":
        found = g.find_nodes(name=name, node_type=node_type)
        return {"operation": "find", "course_id": course_id,
                "count": len(found), "nodes": [_clean(n) for n in found]}

    if node is None:
        return {"error": f"operation={operation} 需提供 node"}
    src = g.resolve(node)
    if not src:
        return {"error": f"未找到节点：{node}"}

    if operation == "neighbors":
        nbrs = g.neighbors(src["node_uid"], direction="both")
        return {"operation": "neighbors", "node": _clean(src),
                "count": len(nbrs), "neighbors": [_clean(n) for n in nbrs]}

    if operation == "prerequisites":
        pres = g.prerequisites(src["node_uid"])
        return {"operation": "prerequisites", "node": _clean(src),
                "count": len(pres), "prerequisites": [_clean(n) for n in pres]}

    if operation in ("related", "similar"):
        rel = "RELATED_TO" if operation == "related" else "SIMILAR_TO"
        nbrs = g.neighbors(src["node_uid"], rel_types={rel}, direction="both")
        return {"operation": operation, "node": _clean(src),
                "count": len(nbrs), "nodes": [_clean(n) for n in nbrs]}

    if operation == "path":
        if not target:
            return {"error": "operation=path 需提供 target"}
        dst = g.resolve(target)
        if not dst:
            return {"error": f"未找到终点节点：{target}"}
        res = g.shortest_path([src["node_uid"]], dst["node_uid"])
        if not res:
            return {"operation": "path", "from": _clean(src), "to": _clean(dst),
                    "path": [], "note": "两节点间无（有向）学习路径"}
        path, cost = res
        return {"operation": "path", "from": _clean(src), "to": _clean(dst),
                "cost": cost, "length": len(path), "path": [_clean(n) for n in path],
                "cypher_hint": g.to_cypher_hint(dst["node_uid"])}

    return {"error": f"未知 operation：{operation}"}


def recommend_study_path(conn: sqlite3.Connection, student_id, course_id, target=None,
                         threshold=0.6, max_points=6, questions_per_point=3) -> dict:
    g = kg.KnowledgeGraph.from_sqlite(conn, course_id=course_id)
    if not g.nodes:
        return {"error": f"课程 {course_id} 无知识图谱数据"}

    # 学生掌握度（仅 concept/skill 参与路径）
    mastery = {
        r["node_uid"]: r["mastery_rate"]
        for r in conn.execute(
            "SELECT node_uid, mastery_rate FROM student_knowledge_stats "
            "WHERE student_id=? AND course_id=?", (student_id, course_id))
    }
    weak_uids = {uid for uid, m in mastery.items()
                 if m < threshold and g.nodes.get(uid, {}).get("type") in ("concept", "skill")}

    # 选目标
    if target:
        tnode = g.resolve(target)
        if not tnode:
            return {"error": f"未找到目标知识点：{target}"}
    else:
        # 自动选：薄弱节点中 importance 最高者（优先 skill/topic）
        cands = [g.nodes[u] for u in weak_uids] or \
                [n for n in g.nodes.values() if n["type"] in ("concept", "skill")]
        if not cands:
            return {"student_id": student_id, "course_id": course_id,
                    "note": "无可推荐的知识点", "path": []}
        cands.sort(key=lambda n: (n["type"] != "skill", -(n.get("importance") or 0)))
        tnode = cands[0]
    target_uid = tnode["node_uid"]

    # 组装路径：目标的全部前置 ∩ 薄弱，按各自前置链长度排序（越基础越靠前），目标置末。
    prereqs = g.prerequisites(target_uid)
    weak_prereqs = [n for n in prereqs if n["node_uid"] in weak_uids]
    weak_prereqs.sort(key=lambda n: len(g.prerequisites(n["node_uid"])))
    ordered = weak_prereqs[:]
    if mastery.get(target_uid, 1.0) < threshold or not ordered:
        ordered.append(tnode)  # 目标本身也薄弱，或没有薄弱前置时，至少复习目标
    # 截断（保留最靠近目标的若干 + 目标）
    if len(ordered) > max_points:
        ordered = ordered[-max_points:]

    path = []
    for n in ordered:
        practice = search_questions(conn, course_id=course_id, knowledge_point=n["node_uid"],
                                    page_size=questions_per_point)
        path.append({
            "node_uid": n["node_uid"], "name": n["name"], "type": n["type"],
            "difficulty": n.get("difficulty"),
            "mastery_rate": mastery.get(n["node_uid"]),
            "practice_questions": [
                {"id": q["id"], "title": q["title"], "difficulty": q["difficulty"],
                 "question_type": q["question_type"]}
                for q in practice.get("questions", [])
            ],
        })
    return {
        "student_id": student_id, "course_id": course_id,
        "target": {"node_uid": target_uid, "name": tnode["name"], "type": tnode["type"],
                   "mastery_rate": mastery.get(target_uid)},
        "weak_point_count": len(weak_uids),
        "path": path,
        "cypher_hint": g.to_cypher_hint(target_uid),
    }
