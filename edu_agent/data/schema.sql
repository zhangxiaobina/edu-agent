-- EduAgent 合成教学库 schema
-- mirror 真实在线教学平台 (Spring Boot LMS) 的核心实体与状态枚举。
-- 红线：本库内容 100% 合成，由 generate.py 固定种子可复现生成；真实学生数据绝不入库。
--
-- 状态枚举（对照真实平台）：
--   exams.status          0=未开始/草稿  1=进行中/已发布  2=已结束
--   exam_records.status   0=未开始  1=答题中  2=已提交  3=已批改
--   questions.status      0=禁用  1=正常
--   class_students.status 0=退出  1=正常
--   homeworks.status      DRAFT / PUBLISHED / CLOSED
--   question_type         single | multiple | judge | fill | essay | coding
--   difficulty            easy | medium | hard
--   source                manual | import | ai
--   kg node type          chapter | topic | concept | skill
--   kg rel_type           PREREQUISITE_OF | PART_OF | RELATED_TO | SIMILAR_TO

PRAGMA foreign_keys = ON;

-- ---------- 用户 ----------
CREATE TABLE teachers (
    id        INTEGER PRIMARY KEY,
    username  TEXT NOT NULL,           -- 工号
    name      TEXT NOT NULL
);

CREATE TABLE students (
    id        INTEGER PRIMARY KEY,
    username  TEXT NOT NULL,           -- 学号
    name      TEXT NOT NULL,
    phone     TEXT,                    -- 合成
    email     TEXT                     -- 合成
);

-- ---------- 课程 / 班级 ----------
CREATE TABLE courses (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    teacher_id  INTEGER NOT NULL REFERENCES teachers(id),
    description TEXT
);

CREATE TABLE classes (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,         -- e.g. 计科2024-3班
    teacher_id  INTEGER NOT NULL REFERENCES teachers(id)
);

-- 班级 ↔ 课程（多对多）
CREATE TABLE class_courses (
    class_id   INTEGER NOT NULL REFERENCES classes(id),
    course_id  INTEGER NOT NULL REFERENCES courses(id),
    PRIMARY KEY (class_id, course_id)
);

-- 班级 ↔ 学生（多对多 + 入班信息）
CREATE TABLE class_students (
    class_id   INTEGER NOT NULL REFERENCES classes(id),
    student_id INTEGER NOT NULL REFERENCES students(id),
    join_time  TEXT NOT NULL,
    status     INTEGER NOT NULL DEFAULT 1,   -- 0=退出 1=正常
    PRIMARY KEY (class_id, student_id)
);

-- ---------- 题库 / 题目 ----------
CREATE TABLE question_banks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    course_id   INTEGER NOT NULL REFERENCES courses(id),
    creator_id  INTEGER NOT NULL REFERENCES teachers(id)
);

CREATE TABLE questions (
    id             INTEGER PRIMARY KEY,
    title          TEXT NOT NULL,
    content        TEXT NOT NULL,
    question_type  TEXT NOT NULL,            -- single|multiple|judge|fill|essay|coding
    difficulty     TEXT NOT NULL,            -- easy|medium|hard
    options        TEXT,                     -- JSON 数组（选择题）
    correct_answer TEXT,                     -- "A" / "A,C" / 参考答案
    explanation    TEXT,
    score          REAL NOT NULL DEFAULT 5,
    source         TEXT NOT NULL DEFAULT 'manual',  -- manual|import|ai
    status         INTEGER NOT NULL DEFAULT 1,      -- 0=禁用 1=正常
    creator_id     INTEGER REFERENCES teachers(id),
    language       TEXT,                     -- coding 题：python|java|cpp...
    usage_count    INTEGER NOT NULL DEFAULT 0,
    course_id      INTEGER REFERENCES courses(id)
);

CREATE TABLE question_bank_questions (
    question_bank_id INTEGER NOT NULL REFERENCES question_banks(id),
    question_id      INTEGER NOT NULL REFERENCES questions(id),
    PRIMARY KEY (question_bank_id, question_id)
);

-- ---------- 考试 ----------
CREATE TABLE exams (
    id               INTEGER PRIMARY KEY,
    exam_name        TEXT NOT NULL,
    exam_code        TEXT NOT NULL,
    description      TEXT,
    class_id         INTEGER NOT NULL REFERENCES classes(id),
    course_id        INTEGER NOT NULL REFERENCES courses(id),
    question_bank_id INTEGER REFERENCES question_banks(id),
    creator_id       INTEGER NOT NULL REFERENCES teachers(id),
    start_time       TEXT,
    end_time         TEXT,
    duration         INTEGER NOT NULL DEFAULT 90,    -- 分钟
    total_score      REAL NOT NULL DEFAULT 100,
    pass_score       REAL NOT NULL DEFAULT 60,
    question_count   INTEGER NOT NULL DEFAULT 0,
    status           INTEGER NOT NULL DEFAULT 0      -- 0=未开始 1=进行中 2=已结束
);

CREATE TABLE exam_questions (
    exam_id     INTEGER NOT NULL REFERENCES exams(id),
    question_id INTEGER NOT NULL REFERENCES questions(id),
    score       REAL NOT NULL DEFAULT 5,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (exam_id, question_id)
);

-- 学生一次考试的作答记录（汇总）
CREATE TABLE exam_records (
    id            INTEGER PRIMARY KEY,
    exam_id       INTEGER NOT NULL REFERENCES exams(id),
    student_id    INTEGER NOT NULL REFERENCES students(id),
    score         REAL,
    total_score   REAL,
    correct_count INTEGER,
    answer_count  INTEGER,
    status        INTEGER NOT NULL DEFAULT 0,   -- 0=未开始 1=答题中 2=已提交 3=已批改
    start_time    TEXT,
    submit_time   TEXT,
    duration      INTEGER,                      -- 实际用时(分)
    passed        INTEGER,                      -- 0/1
    rank          INTEGER,
    UNIQUE (exam_id, student_id)
);

-- 学生作答的逐题明细（错题分析、判分基础）
CREATE TABLE exam_answers (
    id             INTEGER PRIMARY KEY,
    record_id      INTEGER NOT NULL REFERENCES exam_records(id),
    exam_id        INTEGER NOT NULL REFERENCES exams(id),
    student_id     INTEGER NOT NULL REFERENCES students(id),
    question_id    INTEGER NOT NULL REFERENCES questions(id),
    student_answer TEXT,
    is_correct     INTEGER,                     -- 0/1，NULL=待判
    earned_score   REAL,
    max_score      REAL
);

-- ---------- 作业 ----------
CREATE TABLE homeworks (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    homework_type   TEXT NOT NULL DEFAULT 'open',   -- open|kg
    description     TEXT,
    course_id       INTEGER NOT NULL REFERENCES courses(id),
    creator_id      INTEGER NOT NULL REFERENCES teachers(id),
    reference_kg_id INTEGER,
    start_time      TEXT,
    end_time        TEXT,
    total_score     REAL NOT NULL DEFAULT 100,
    max_submissions INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'PUBLISHED'  -- DRAFT|PUBLISHED|CLOSED
);

CREATE TABLE homework_classes (
    homework_id INTEGER NOT NULL REFERENCES homeworks(id),
    class_id    INTEGER NOT NULL REFERENCES classes(id),
    PRIMARY KEY (homework_id, class_id)
);

-- ---------- 课件 / 学习进度 ----------
CREATE TABLE courseware (
    id         INTEGER PRIMARY KEY,
    course_id  INTEGER NOT NULL REFERENCES courses(id),
    name       TEXT NOT NULL,
    type       TEXT NOT NULL DEFAULT 'video',  -- video|pdf|doc
    duration   INTEGER,                         -- 分钟
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE learning_progress (
    id              INTEGER PRIMARY KEY,
    student_id      INTEGER NOT NULL REFERENCES students(id),
    course_id       INTEGER NOT NULL REFERENCES courses(id),
    courseware_id   INTEGER NOT NULL REFERENCES courseware(id),
    progress        INTEGER NOT NULL DEFAULT 0,   -- 0-100
    completed       INTEGER NOT NULL DEFAULT 0,   -- 0/1
    watched_time    INTEGER NOT NULL DEFAULT 0,   -- 分钟
    last_position   INTEGER NOT NULL DEFAULT 0,
    study_status    TEXT NOT NULL DEFAULT 'not_started', -- not_started|in_progress|completed
    start_time      TEXT,
    last_access_time TEXT,
    UNIQUE (student_id, courseware_id)
);

-- ---------- 错题本 ----------
CREATE TABLE wrong_questions (
    id             INTEGER PRIMARY KEY,
    student_id     INTEGER NOT NULL REFERENCES students(id),
    question_id    INTEGER NOT NULL REFERENCES questions(id),
    course_id      INTEGER REFERENCES courses(id),
    times_wrong    INTEGER NOT NULL DEFAULT 1,
    last_wrong_time TEXT,
    UNIQUE (student_id, question_id)
);

-- ---------- 知识图谱（mirror Neo4j 设计：单 :KnowledgePoint 标签） ----------
CREATE TABLE kg_nodes (
    node_uid    TEXT PRIMARY KEY,        -- UUID
    graph_id    INTEGER NOT NULL,
    course_id   INTEGER NOT NULL REFERENCES courses(id),
    name        TEXT NOT NULL,
    description TEXT,
    type        TEXT NOT NULL,           -- chapter|topic|concept|skill
    difficulty  INTEGER,                 -- 1-5（chapter 忽略）
    importance  REAL,                    -- 0-1
    status      TEXT NOT NULL DEFAULT 'active',  -- draft|active
    source      TEXT NOT NULL DEFAULT 'manual'   -- ai|manual
);

CREATE TABLE kg_edges (
    id        INTEGER PRIMARY KEY,
    graph_id  INTEGER NOT NULL,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    rel_type  TEXT NOT NULL,             -- PREREQUISITE_OF|PART_OF|RELATED_TO|SIMILAR_TO
    start_uid TEXT NOT NULL REFERENCES kg_nodes(node_uid),
    end_uid   TEXT NOT NULL REFERENCES kg_nodes(node_uid),
    weight    REAL NOT NULL DEFAULT 0.5, -- 0-1，越大耦合越紧；最短路 cost=1-weight
    source    TEXT NOT NULL DEFAULT 'manual'
);

-- 知识点 ↔ 资源（题目/题库/课件）关联（mirror kg_resource_link）
CREATE TABLE kg_resource_link (
    id            INTEGER PRIMARY KEY,
    course_id     INTEGER NOT NULL REFERENCES courses(id),
    node_uid      TEXT NOT NULL REFERENCES kg_nodes(node_uid),
    resource_type TEXT NOT NULL,         -- question|question_bank|courseware
    resource_id   INTEGER NOT NULL,
    link_type     TEXT NOT NULL DEFAULT 'covers', -- covers|tests|extends
    weight        REAL NOT NULL DEFAULT 1.0,
    UNIQUE (node_uid, resource_type, resource_id)
);

-- 学生对各知识点的掌握度（mirror student_knowledge_stats）
CREATE TABLE student_knowledge_stats (
    id             INTEGER PRIMARY KEY,
    student_id     INTEGER NOT NULL REFERENCES students(id),
    node_uid       TEXT NOT NULL REFERENCES kg_nodes(node_uid),
    course_id      INTEGER NOT NULL REFERENCES courses(id),
    mastery_rate   REAL NOT NULL DEFAULT 0,   -- 0-1
    correct_count  INTEGER NOT NULL DEFAULT 0,
    total_questions INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT,
    UNIQUE (student_id, node_uid)
);

-- ---------- 写工具事务运行时 ----------
-- 业务写入、committed 状态和 outbox 事件共享此数据库事务。
CREATE TABLE tool_operations (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    plan_step_id TEXT,
    tool_call_id TEXT,
    status TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    result_json TEXT,
    snapshot_json TEXT,
    approval_scope TEXT NOT NULL,
    approved_by TEXT,
    approval_expires_at TEXT,
    last_error TEXT,
    compensation_attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    committed_at TEXT,
    compensated_at TEXT
);

CREATE INDEX idx_tool_operations_owner
    ON tool_operations(tenant_id, actor_id, created_at DESC);
CREATE INDEX idx_tool_operations_call
    ON tool_operations(run_id, tool_call_id);

CREATE TABLE tool_approvals (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES tool_operations(id) ON DELETE CASCADE,
    payload_hash TEXT NOT NULL,
    scope TEXT NOT NULL,
    decision TEXT NOT NULL,
    approver_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_tool_approvals_operation
    ON tool_approvals(operation_id, created_at DESC);

CREATE TABLE tool_outbox (
    event_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES tool_operations(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT
);

CREATE INDEX idx_tool_outbox_pending
    ON tool_outbox(status, lease_until, created_at);

CREATE TABLE tool_consumer_events (
    consumer_name TEXT NOT NULL,
    event_id TEXT NOT NULL,
    consumed_at TEXT NOT NULL,
    PRIMARY KEY(consumer_name, event_id)
);

-- 常用查询索引
CREATE INDEX idx_class_students_class ON class_students(class_id);
CREATE INDEX idx_exam_records_exam ON exam_records(exam_id);
CREATE INDEX idx_exam_answers_exam ON exam_answers(exam_id);
CREATE INDEX idx_exam_answers_question ON exam_answers(question_id);
CREATE INDEX idx_exams_class ON exams(class_id);
CREATE INDEX idx_kg_nodes_course ON kg_nodes(course_id);
CREATE INDEX idx_kg_edges_course ON kg_edges(course_id);
CREATE INDEX idx_kg_resource_link_node ON kg_resource_link(node_uid);
CREATE INDEX idx_kg_resource_link_res ON kg_resource_link(resource_type, resource_id);
CREATE INDEX idx_sks_student ON student_knowledge_stats(student_id);
CREATE INDEX idx_wrong_student ON wrong_questions(student_id);
CREATE INDEX idx_lp_student ON learning_progress(student_id);
