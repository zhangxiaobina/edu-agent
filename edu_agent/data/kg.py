"""内存知识图谱引擎（mirror 真实 Neo4j 设计）。

从 SQLite 的 kg_nodes / kg_edges 装载，提供图查询与学习路径计算，
不依赖 Neo4j 服务即可复现。后续可替换为真实 Neo4j 后端（见 backend 抽象）。

mirror 的设计要点（来自真实 7 万字设计文档）：
- 单一标签 :KnowledgePoint，type ∈ {chapter, topic, concept, skill}
- 关系 PREREQUISITE_OF / PART_OF（有向）、RELATED_TO / SIMILAR_TO（无向，存一向）
- 每条边带 weight∈[0,1]；最短学习路径 cost = Σ(1 − weight)
- chapter 节点不参与最短路径计算（仅作分组容器）
"""
from __future__ import annotations

import heapq
import sqlite3
from dataclasses import dataclass, field

DIRECTED_RELS = {"PREREQUISITE_OF", "PART_OF"}
UNDIRECTED_RELS = {"RELATED_TO", "SIMILAR_TO"}


@dataclass
class Edge:
    rel_type: str
    start_uid: str
    end_uid: str
    weight: float
    source: str


@dataclass
class KnowledgeGraph:
    """一门课（或一个 graph_id）的知识图谱视图。"""

    nodes: dict[str, dict] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    # 邻接表：out[uid] = [(edge, neighbor_uid)]；undirected 关系两向都加。
    _out: dict[str, list[tuple[Edge, str]]] = field(default_factory=dict)
    _in: dict[str, list[tuple[Edge, str]]] = field(default_factory=dict)

    # ---------- 装载 ----------
    @classmethod
    def from_sqlite(cls, conn: sqlite3.Connection, course_id: int | None = None,
                    graph_id: int | None = None) -> "KnowledgeGraph":
        g = cls()
        where, params = [], []
        if course_id is not None:
            where.append("course_id=?")
            params.append(course_id)
        if graph_id is not None:
            where.append("graph_id=?")
            params.append(graph_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        for r in conn.execute(f"SELECT * FROM kg_nodes{clause}", params):
            g.nodes[r["node_uid"]] = dict(r)
        for r in conn.execute(f"SELECT * FROM kg_edges{clause}", params):
            g.add_edge(Edge(r["rel_type"], r["start_uid"], r["end_uid"], r["weight"], r["source"]))
        return g

    def add_edge(self, e: Edge) -> None:
        self.edges.append(e)
        self._out.setdefault(e.start_uid, []).append((e, e.end_uid))
        self._in.setdefault(e.end_uid, []).append((e, e.start_uid))
        if e.rel_type in UNDIRECTED_RELS:  # 无向关系：反向也可达
            self._out.setdefault(e.end_uid, []).append((e, e.start_uid))
            self._in.setdefault(e.start_uid, []).append((e, e.end_uid))

    # ---------- 基础查询 ----------
    def get_node(self, uid: str) -> dict | None:
        return self.nodes.get(uid)

    def find_nodes(self, name: str | None = None, node_type: str | None = None,
                   exact: bool = False) -> list[dict]:
        out = []
        for n in self.nodes.values():
            if node_type and n["type"] != node_type:
                continue
            if name:
                if exact and n["name"] != name:
                    continue
                if not exact and name.lower() not in n["name"].lower():
                    continue
            out.append(n)
        return out

    def resolve(self, ref: str) -> dict | None:
        """按 node_uid 或名称（精确优先，否则模糊唯一匹配）解析一个节点。"""
        if ref in self.nodes:
            return self.nodes[ref]
        exact = self.find_nodes(name=ref, exact=True)
        if exact:
            return exact[0]
        fuzzy = self.find_nodes(name=ref)
        return fuzzy[0] if fuzzy else None

    def neighbors(self, uid: str, rel_types: set[str] | None = None,
                  direction: str = "out") -> list[dict]:
        """返回邻居节点（带 rel_type / weight / direction 标注）。"""
        adj = {"out": self._out, "in": self._in}
        buckets = [self._out, self._in] if direction == "both" else [adj[direction]]
        seen, results = set(), []
        for bucket in buckets:
            for e, nb in bucket.get(uid, []):
                if rel_types and e.rel_type not in rel_types:
                    continue
                key = (e.rel_type, nb)
                if key in seen:
                    continue
                seen.add(key)
                node = self.nodes.get(nb)
                if node:
                    results.append({**node, "_rel_type": e.rel_type, "_weight": e.weight,
                                    "_direction": "out" if bucket is self._out else "in"})
        return results

    def prerequisites(self, uid: str, max_depth: int = 6) -> list[dict]:
        """沿 PREREQUISITE_OF 逆向回溯，返回该节点的全部前置知识点（去重，按发现顺序）。"""
        result, seen = [], set()
        frontier = [(uid, 0)]
        while frontier:
            cur, depth = frontier.pop(0)
            if depth >= max_depth:
                continue
            for e, pre in self._in.get(cur, []):
                if e.rel_type != "PREREQUISITE_OF" or pre in seen:
                    continue
                seen.add(pre)
                node = self.nodes.get(pre)
                if node and node["type"] != "chapter":
                    result.append(node)
                frontier.append((pre, depth + 1))
        return result

    # ---------- 学习路径：mirror 设计文档 shortestPath，cost = Σ(1 − weight) ----------
    def shortest_path(self, start_uids: list[str], target_uid: str,
                      allowed_rels: set[str] = frozenset({"PREREQUISITE_OF", "PART_OF"}),
                      exclude_chapter: bool = True, max_hops: int = 50
                      ) -> tuple[list[dict], float] | None:
        """从任一 start 到 target 的最小代价有向路径。

        代价 = Σ(1 − weight)，与设计文档一致（weight 越大耦合越紧、代价越低）。
        chapter 节点默认不进入路径。返回 (有序节点列表, 总代价) 或 None。
        注：真实平台 Cypher 用 `*..6` 限制跳数（性能取舍）；此处默认放宽到 12 以适配合成小图。
        """
        if target_uid not in self.nodes:
            return None
        starts = [s for s in start_uids if s in self.nodes]
        if not starts:
            return None
        # 多源 Dijkstra：超级源 → 所有 start 0 代价
        dist: dict[str, float] = {}
        prev: dict[str, tuple[str, Edge] | None] = {}
        pq: list[tuple[float, int, str]] = []
        counter = 0
        for s in starts:
            dist[s] = 0.0
            prev[s] = None
            heapq.heappush(pq, (0.0, counter, s))
            counter += 1
        while pq:
            d, _, u = heapq.heappop(pq)
            if d > dist.get(u, float("inf")):
                continue
            if u == target_uid:
                break
            for e, v in self._out.get(u, []):
                if e.rel_type not in allowed_rels:
                    continue
                node_v = self.nodes.get(v)
                if exclude_chapter and node_v and node_v["type"] == "chapter" and v != target_uid:
                    continue
                nd = d + (1.0 - e.weight)
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = (u, e)
                    heapq.heappush(pq, (nd, counter, v))
                    counter += 1
        if target_uid not in dist:
            return None
        # 回溯
        path_uids, cur = [], target_uid
        while cur is not None:
            path_uids.append(cur)
            step = prev.get(cur)
            cur = step[0] if step else None
        path_uids.reverse()
        if len(path_uids) - 1 > max_hops:
            return None
        path_nodes = [self.nodes[u] for u in path_uids
                      if not (exclude_chapter and self.nodes[u]["type"] == "chapter")]
        return path_nodes, round(dist[target_uid], 3)

    def to_cypher_hint(self, target_uid: str) -> str:
        """返回与真实平台等价的 Cypher（仅作展示/对照，不执行）。"""
        return (
            "MATCH (target:KnowledgePoint {node_uid: $target}) WHERE target.type <> 'chapter' "
            "MATCH (start:KnowledgePoint) WHERE start.node_uid IN $weakNodeUids "
            "WITH target, start MATCH p = shortestPath((start)-[:PREREQUISITE_OF|PART_OF*..6]->(target)) "
            "WITH p, reduce(s=0.0, rel IN relationships(p) | s + (1.0 - coalesce(rel.weight,0.5))) AS cost "
            "ORDER BY cost ASC LIMIT 1 "
            "RETURN [n IN nodes(p) WHERE n.type <> 'chapter' | n.node_uid] AS path_uids"
        )
