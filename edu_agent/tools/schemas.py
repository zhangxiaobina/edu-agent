"""16 个教学教务工具的 OpenAI function-calling schema。

入参名 / 枚举 / 语义对照真实平台 Controller 抽取（见 README 映射表），
工具描述用中文（Agent 以中文推理、产出中文）。SCHEMAS 可直接作为
`tools=[{"type":"function","function": s} for s in SCHEMAS]` 传给模型。
"""
from __future__ import annotations

# ---- 复用的枚举 ----
_DIFFICULTY = ["easy", "medium", "hard"]
_QTYPE = ["single", "multiple", "judge", "fill", "essay", "coding"]

SCHEMAS: list[dict] = [
    # ==================== 查询类 ====================
    {
        "name": "query_student_scores",
        "description": "查询学生考试成绩。可按考试、学生或班级过滤；only_failed=true 只看不及格。"
                       "返回每条作答记录的分数、正确数、是否及格、排名等。",
        "parameters": {
            "type": "object",
            "properties": {
                "exam_id": {"type": "integer", "description": "考试 ID"},
                "student_id": {"type": "integer", "description": "学生 ID"},
                "class_id": {"type": "integer", "description": "班级 ID（结合 exam 过滤该班成绩）"},
                "only_failed": {"type": "boolean", "description": "仅返回不及格记录", "default": False},
                "page": {"type": "integer", "default": 1},
                "page_size": {"type": "integer", "default": 50},
            },
            "required": [],
        },
    },
    {
        "name": "list_exams",
        "description": "列出考试，可按班级/课程/状态过滤、按名称搜索。返回考试基本信息与"
                       "参考人数、提交数、均分、通过率等统计。",
        "parameters": {
            "type": "object",
            "properties": {
                "class_id": {"type": "integer", "description": "班级 ID"},
                "course_id": {"type": "integer", "description": "课程 ID"},
                "status": {"type": "integer", "enum": [0, 1, 2],
                           "description": "0=未开始 1=进行中 2=已结束"},
                "search": {"type": "string", "description": "考试名/编号关键词"},
                "page": {"type": "integer", "default": 1},
                "page_size": {"type": "integer", "default": 50},
            },
            "required": [],
        },
    },
    {
        "name": "get_class_roster",
        "description": "获取班级学生名单，含每个学生的考试数、作业数、平均分。支持按姓名/学号搜索与排序。",
        "parameters": {
            "type": "object",
            "properties": {
                "class_id": {"type": "integer", "description": "班级 ID（必填）"},
                "search": {"type": "string", "description": "按学生姓名或学号搜索"},
                "sort_by": {"type": "string", "enum": ["student_name", "student_username", "join_time", "avg_score"]},
                "sort_order": {"type": "string", "enum": ["asc", "desc"], "default": "asc"},
                "page": {"type": "integer", "default": 1},
                "page_size": {"type": "integer", "default": 100},
            },
            "required": ["class_id"],
        },
    },
    {
        "name": "search_questions",
        "description": "搜索题库题目，可按题库/课程/题型/难度/知识点过滤与关键词检索。"
                       "用于组卷选题、给薄弱知识点找练习题等。",
        "parameters": {
            "type": "object",
            "properties": {
                "question_bank_id": {"type": "integer", "description": "题库 ID"},
                "course_id": {"type": "integer", "description": "课程 ID"},
                "question_type": {"type": "string", "enum": _QTYPE},
                "difficulty": {"type": "string", "enum": _DIFFICULTY},
                "knowledge_point": {"type": "string",
                                    "description": "知识点名称或 node_uid（按图谱关联过滤）"},
                "keyword": {"type": "string", "description": "题目标题/内容关键词"},
                "status": {"type": "integer", "enum": [0, 1], "default": 1,
                           "description": "0=禁用 1=正常"},
                "page": {"type": "integer", "default": 1},
                "page_size": {"type": "integer", "default": 20},
            },
            "required": [],
        },
    },
    {
        "name": "get_learning_progress",
        "description": "查询学生课程学习进度（课件完成度）。返回课程整体进度汇总与逐课件明细。",
        "parameters": {
            "type": "object",
            "properties": {
                "student_id": {"type": "integer", "description": "学生 ID（必填）"},
                "course_id": {"type": "integer", "description": "课程 ID；不填则返回该生所有课程"},
            },
            "required": ["student_id"],
        },
    },
    # ==================== 知识图谱 ====================
    {
        "name": "query_knowledge_graph",
        "description": "查询课程知识图谱（mirror Neo4j：chapter/topic/concept/skill 节点 + "
                       "先修PREREQUISITE_OF/包含PART_OF/相关RELATED_TO/相似SIMILAR_TO 关系）。"
                       "operation 选择查询类型。",
        "parameters": {
            "type": "object",
            "properties": {
                "course_id": {"type": "integer", "description": "课程 ID（必填，确定图范围）"},
                "operation": {"type": "string",
                              "enum": ["find", "neighbors", "prerequisites", "related", "similar", "path"],
                              "description": "find=按名/类型找节点; neighbors=相邻; prerequisites=全部前置; "
                                             "related/similar=相关/相似; path=两节点最短学习路径"},
                "node": {"type": "string", "description": "目标节点名称或 node_uid（除 find 外必填）"},
                "target": {"type": "string", "description": "operation=path 时的终点节点名/uid"},
                "node_type": {"type": "string", "enum": ["chapter", "topic", "concept", "skill"],
                              "description": "operation=find 时按类型过滤"},
                "name": {"type": "string", "description": "operation=find 时按名称模糊匹配"},
            },
            "required": ["course_id", "operation"],
        },
    },
    # ==================== 分析类 ====================
    {
        "name": "analyze_class_errors",
        "description": "分析某次考试（或某班）错得最多的题目 Top-N，附题目难度与所考知识点。"
                       "用于发现班级普遍薄弱处。",
        "parameters": {
            "type": "object",
            "properties": {
                "exam_id": {"type": "integer", "description": "考试 ID（优先）"},
                "class_id": {"type": "integer", "description": "班级 ID（汇总该班所有考试）"},
                "top": {"type": "integer", "default": 10, "description": "返回前 N 题"},
            },
            "required": [],
        },
    },
    {
        "name": "diagnose_weak_points",
        "description": "诊断薄弱知识点：按知识点掌握度 mastery_rate 低于阈值排序。"
                       "可针对单个学生，或汇总整班。",
        "parameters": {
            "type": "object",
            "properties": {
                "student_id": {"type": "integer", "description": "学生 ID（个人诊断）"},
                "class_id": {"type": "integer", "description": "班级 ID（全班汇总诊断）"},
                "course_id": {"type": "integer", "description": "限定课程"},
                "threshold": {"type": "number", "default": 0.6, "description": "掌握度阈值，低于即薄弱"},
                "top": {"type": "integer", "default": 10},
            },
            "required": [],
        },
    },
    {
        "name": "get_score_distribution",
        "description": "统计某次考试的成绩分布（分数段人数）与汇总指标（均分/中位数/最高最低/标准差/通过率）。",
        "parameters": {
            "type": "object",
            "properties": {
                "exam_id": {"type": "integer", "description": "考试 ID（必填）"},
            },
            "required": ["exam_id"],
        },
    },
    # ==================== 操作类（写入合成库） ====================
    {
        "name": "create_exam",
        "description": "创建一场考试（草稿状态 status=0）。返回新建考试 ID 与编号。",
        "parameters": {
            "type": "object",
            "properties": {
                "exam_name": {"type": "string", "description": "考试名称（必填）"},
                "class_id": {"type": "integer", "description": "目标班级 ID（必填）"},
                "course_id": {"type": "integer", "description": "课程 ID（必填）"},
                "duration": {"type": "integer", "default": 90, "description": "时长(分钟)"},
                "question_bank_id": {"type": "integer", "description": "关联题库 ID"},
                "total_score": {"type": "number", "default": 100},
                "pass_score": {"type": "number", "default": 60},
                "question_count": {"type": "integer", "default": 0},
                "description": {"type": "string"},
            },
            "required": ["exam_name", "class_id", "course_id"],
        },
    },
    {
        "name": "generate_paper",
        "description": "按规则自动组卷：从题库按数量/难度配比/知识点过滤抽题，生成试卷预览"
                       "（含选中题目、难度与题型分布、总分、质量评分与建议）。",
        "parameters": {
            "type": "object",
            "properties": {
                "question_bank_id": {"type": "integer", "description": "题库 ID（必填）"},
                "paper_name": {"type": "string", "description": "试卷名称"},
                "total_questions": {"type": "integer", "default": 10, "description": "目标题量"},
                "difficulty_distribution": {
                    "type": "object",
                    "description": "各难度题数，如 {\"easy\":4,\"medium\":4,\"hard\":2}",
                    "additionalProperties": {"type": "integer"},
                },
                "question_counts": {
                    "type": "object",
                    "description": "各题型题数，如 {\"single\":6,\"judge\":2}",
                    "additionalProperties": {"type": "integer"},
                },
                "knowledge_points": {"type": "array", "items": {"type": "string"},
                                     "description": "限定知识点（名称或 uid）列表"},
            },
            "required": ["question_bank_id"],
        },
    },
    {
        "name": "batch_grade",
        "description": "批量判分：对一场考试的所有作答记录按逐题作答重算得分、正确数与排名，"
                       "状态置为已批改(3)。regrade=true 时重判全部，否则只判未批改的。",
        "parameters": {
            "type": "object",
            "properties": {
                "exam_id": {"type": "integer", "description": "考试 ID（必填）"},
                "regrade": {"type": "boolean", "default": False, "description": "是否重判已批改记录"},
            },
            "required": ["exam_id"],
        },
    },
    {
        "name": "assign_homework",
        "description": "布置作业到一个或多个班级。返回新建作业 ID。",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "作业标题（必填）"},
                "course_id": {"type": "integer", "description": "课程 ID（必填）"},
                "class_ids": {"type": "array", "items": {"type": "integer"},
                              "description": "目标班级 ID 列表（必填）"},
                "end_time": {"type": "string", "description": "截止时间 yyyy-MM-dd HH:mm:ss（必填）"},
                "homework_type": {"type": "string", "enum": ["open", "kg"], "default": "open"},
                "description": {"type": "string"},
                "start_time": {"type": "string", "description": "开放时间 yyyy-MM-dd HH:mm:ss"},
                "total_score": {"type": "number", "default": 100},
                "max_submissions": {"type": "integer", "default": 1},
            },
            "required": ["title", "course_id", "class_ids", "end_time"],
        },
    },
    # ==================== AI / 执行 ====================
    {
        "name": "generate_questions",
        "description": "AI 出题：围绕指定知识点按题型/难度配比生成若干题目（合成）。"
                       "可选直接入库。注：引擎接入前为模板化生成，接入工具调用模型后可替换为真实生成。",
        "parameters": {
            "type": "object",
            "properties": {
                "course_id": {"type": "integer", "description": "课程 ID（必填）"},
                "knowledge_point": {"type": "string", "description": "围绕的知识点名称或 uid"},
                "count": {"type": "integer", "default": 5, "description": "生成题目数"},
                "question_types": {"type": "object",
                                   "description": "各题型题数，如 {\"single\":3,\"fill\":2}",
                                   "additionalProperties": {"type": "integer"}},
                "difficulty_distribution": {"type": "object",
                                            "additionalProperties": {"type": "integer"}},
                "save_to_bank": {"type": "integer", "description": "若给定题库 ID 则把生成题入库"},
            },
            "required": ["course_id"],
        },
    },
    {
        "name": "recommend_study_path",
        "description": "为学生推荐学习路径：定位其薄弱知识点（mastery<阈值），结合知识图谱"
                       "前置关系给出有序复习序列，并为每个薄弱点附练习题。target 不填则自动选最该补的目标。",
        "parameters": {
            "type": "object",
            "properties": {
                "student_id": {"type": "integer", "description": "学生 ID（必填）"},
                "course_id": {"type": "integer", "description": "课程 ID（必填）"},
                "target": {"type": "string", "description": "目标知识点名称/uid；不填自动选取"},
                "threshold": {"type": "number", "default": 0.6, "description": "薄弱判定阈值"},
                "max_points": {"type": "integer", "default": 6, "description": "路径最多包含的知识点数"},
                "questions_per_point": {"type": "integer", "default": 3, "description": "每个薄弱点附题数"},
            },
            "required": ["student_id", "course_id"],
        },
    },
    {
        "name": "run_code",
        "description": "在沙箱中运行代码并返回结果（mirror Jobe）。给定 expected_output 时比对判定。"
                       "目前支持 language=python。",
        "parameters": {
            "type": "object",
            "properties": {
                "source_code": {"type": "string", "description": "源代码（必填）"},
                "language": {"type": "string", "enum": ["python"], "default": "python"},
                "stdin": {"type": "string", "description": "标准输入"},
                "expected_output": {"type": "string", "description": "期望输出（给定则比对）"},
                "timeout": {"type": "integer", "default": 5, "description": "墙钟超时(秒)"},
            },
            "required": ["source_code"],
        },
    },
]

# name -> schema，便于 registry 校验
SCHEMA_BY_NAME = {s["name"]: s for s in SCHEMAS}
