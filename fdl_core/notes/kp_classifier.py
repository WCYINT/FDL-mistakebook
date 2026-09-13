"""LLM 学科分类器 + 学科自动新增（Phase 1 · 多科地基）。

职责边界（与 kp_matcher 正交）
-----------------------------
    kp_matcher     : "这道错题考哪个知识点？"  → mistake_record.kp_id
    kp_classifier  : "这个知识点属于哪个学科/领域？" → knowledge_point.subject_id / knowledge_domain / parent_id

本模块解决三个问题
------------------
1. **学科自动新增**：LLM 判定某知识点属于学科 X，若 subject 表无 X 则自动建行
   （不再靠人工预先登记，也不再靠 code 前缀猜测）。
2. **学科归属分类**：LLM 判定每个 KP 的 subject / knowledge_domain / parent_code。
3. **细分领域**：domain 是比 subject 更细的维度（如数学"数与代数/图形与几何"），
   供星图二级筛选使用。

学科自动新增的三要素（King 要求明确）
-------------------------------------
- **触发条件**：`ensure_subject()` 被调用的三种时机——
  ① 存量回填（`classify_kp_batch` 处理 47 条存量 KP）；
  ② 新 KP 入库（`promote_candidate` 晋级候选时）；
  ③ 手动触发（`POST /api/kp/classify` / CLI）。
  且仅当 LLM 返回的 subject code **在 subject 表中查无此行**时才创建。
- **判定依据**：LLM 按"该知识点考查的**核心能力**归属哪个学科"判定；
  subject code 经 `normalize_subject_code()` 归一（大小写 + 别名）；
  canonical 四科（数学/语文/英语/科学）用 `CANONICAL_SUBJECTS` 的官方名与配色；
  canonical 之外的（LLM 提议的更细学科）建行并标 `source='LLM_PROPOSED'` 以便追溯。
- **去重规则**：① code 大写归一 + 别名映射（`CHN`→`CHINESE`、`数学`→`MATH`）；
  ② 按 `subject.code` UNIQUE 精确查重，命中则复用既有行（**绝不重复建**）；
  ③ 名称撞车检测（不同 code 但同 name → 复用既有行并记 warning，防"语文/CHINESE"并存）。

写回范围：`knowledge_point.subject_id / knowledge_domain / parent_id`（三列）。
闸门：沿用本项目既定纪律——双跑一致时才自动写；低置信落 `kp_candidate` 式的
待人工队列（本模块用返回值 `needs_review=True` 标记，由调用方决定入队）。
"""

from __future__ import annotations

import logging

from fdl_core.l2.fallback import run_chain
from fdl_core.l2.minimax import MiniMaxClient
from fdl_core.mistakes.attribution_engine import (
    AUTO_ACCEPT_THRESHOLD,
    AUTO_CONFIRM_RUNS,
    CONFIRM_RETRY_MAX,
    _clamp_conf,
    parse_llm_json,
)
from fdl_core.srs.time_layer import fmt_ts, now_utc

logger = logging.getLogger("fdl.notes.kp_classifier")

PROMPT_VERSION = "kp-classify-v1"

# ── 学科注册表（canonical 基线 + 自动新增的判定基准）────────────
# 四科为小学课标基线；字段与 subject 表对齐（除 id 由 DB 分配）。
# 配色为纯色（P0：禁紫粉渐变），四科互相可区分。
CANONICAL_SUBJECTS: dict[str, dict] = {
    "MATH": {
        "name": "数学",
        "short_name": "数",
        "color_hex": "#0E7C7B",
        "rotation_weight": 0.35,
        "sort_order": 1,
        "domains": ["数与代数", "图形与几何", "统计与概率", "综合与实践"],
    },
    "CHINESE": {
        "name": "语文",
        "short_name": "语",
        "color_hex": "#C0392B",
        "rotation_weight": 0.30,
        "sort_order": 2,
        "domains": ["识字与写字", "阅读与鉴赏", "表达与交流", "梳理与探究"],
    },
    "ENGLISH": {
        "name": "英语",
        "short_name": "英",
        "color_hex": "#5B8DBE",
        "rotation_weight": 0.20,
        "sort_order": 3,
        "domains": ["词汇", "语法", "听说", "读写"],
    },
    "SCIENCE": {
        "name": "科学",
        "short_name": "科",
        "color_hex": "#5A8F5A",
        "rotation_weight": 0.15,
        "sort_order": 4,
        "domains": ["物质科学", "生命科学", "地球与宇宙", "技术与工程"],
    },
}

# 别名归一表：LLM 可能输出的变体 → canonical code。
# 与 kp_matcher._parse_code_meta 的宽松前缀识别不同，此处是唯一权威映射。
SUBJECT_ALIASES: dict[str, str] = {
    "CHN": "CHINESE",
    "CHINESE": "CHINESE",
    "语文": "CHINESE",
    "MATH": "MATH",
    "MATHEMATICS": "MATH",
    "数学": "MATH",
    "EN": "ENGLISH",
    "ENG": "ENGLISH",
    "ENGLISH": "ENGLISH",
    "英语": "ENGLISH",
    "SCI": "SCIENCE",
    "SCIENCE": "SCIENCE",
    "科学": "SCIENCE",
}


def normalize_subject_code(raw: str) -> str:
    """学科 code 归一：去空白 → 大写 → 别名映射。查不到别名则原样返回（大写）。"""
    code = str(raw or "").strip().upper()
    return SUBJECT_ALIASES.get(code, SUBJECT_ALIASES.get(str(raw or "").strip(), code))


# 领域名别名归一（2026-09-12）：课标 2022 用"数与代数"，但历史卡片写过
# "数与运算"（更细的子主题名）。两者并存会让星图的领域筛选出现两个近义分组
# → 统一归到课标名。此表只做**显示/写入归一**，不追溯修改既有数据
# （既有 13 条"数与运算"的处理见交付报告的"待确认点"）。
DOMAIN_ALIASES: dict[str, str] = {
    "数与运算": "数与代数",
    "数的运算": "数与代数",
    "代数": "数与代数",
    "几何": "图形与几何",
    "图形": "图形与几何",
    "统计": "统计与概率",
    "概率统计": "统计与概率",
    "实践": "综合与实践",
    "综合实践": "综合与实践",
    "识字写字": "识字与写字",
    "阅读理解": "阅读与鉴赏",
    "写作": "表达与交流",
}


def normalize_domain(raw: str | None) -> str | None:
    """领域名归一（去空白 + 别名映射）；空值返回 None。"""
    if not raw:
        return None
    name = str(raw).strip()
    return DOMAIN_ALIASES.get(name, name) if name else None


def ensure_subject(
    conn,
    code: str,
    *,
    name: str | None = None,
    source: str = "LLM",
    user_id: int = 1,
) -> dict:
    """幂等地确保学科存在；不存在则按规则自动创建。

    返回 {ok, created, subject_id, code, name, source}
    - created=True 表示本次新建；False 表示命中既有行（去重成功）。
    - 去重：① code 归一后按 UNIQUE 查；② 名称撞车复用既有行。
    """
    code_n = normalize_subject_code(code)
    if not code_n:
        return {"ok": False, "error": "subject code 为空"}

    # 去重 ①：按归一 code 精确查
    row = conn.execute(
        "SELECT id, name FROM subject WHERE code=? AND deleted_at IS NULL", (code_n,)
    ).fetchone()
    if row:
        return {
            "ok": True,
            "created": False,
            "subject_id": row[0],
            "code": code_n,
            "name": row[1],
            "source": "EXISTING",
        }

    # canonical 取值；否则用调用方给的 name（LLM 提议的更细学科）
    canon = CANONICAL_SUBJECTS.get(code_n)
    final_name = (canon or {}).get("name") or name or code_n
    final_short = (canon or {}).get("short_name")
    final_color = (canon or {}).get("color_hex") or "#8A8A8A"

    # 去重 ②：名称撞车（不同 code 同 name）→ 复用既有行，不重复建
    dup = conn.execute(
        "SELECT id, code FROM subject WHERE name=? AND deleted_at IS NULL", (final_name,)
    ).fetchone()
    if dup:
        logger.warning(
            "学科名称撞车：请求 code=%s name=%s，但已存在 code=%s —— 复用既有行",
            code_n,
            final_name,
            dup[1],
        )
        return {
            "ok": True,
            "created": False,
            "subject_id": dup[0],
            "code": dup[1],
            "name": final_name,
            "source": "EXISTING_BY_NAME",
        }

    # 自动创建
    max_sort = conn.execute("SELECT COALESCE(MAX(sort_order), 0) FROM subject").fetchone()[0]
    # canonical 的 sort_order 可能已被白名单外学科占用 → 撞号则顺延到末尾
    want_sort = (canon or {}).get("sort_order")
    if not want_sort or want_sort <= max_sort:
        want_sort = max_sort + 1
    cur = conn.execute(
        "INSERT INTO subject (user_id, code, name, short_name, color_hex,"
        " rotation_weight, grade_start, grade_end, sort_order, is_active,"
        " created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
        (
            user_id,
            code_n,
            final_name,
            final_short,
            final_color,
            (canon or {}).get("rotation_weight", 0.1),
            (canon or {}).get("grade_start", 1),
            (canon or {}).get("grade_end", 9),
            want_sort,
            fmt_ts(now_utc()),
            fmt_ts(now_utc()),
        ),
    )
    conn.commit()
    logger.info("学科自动新增：code=%s name=%s（来源=%s）", code_n, final_name, source)
    return {
        "ok": True,
        "created": True,
        "subject_id": int(cur.lastrowid),
        "code": code_n,
        "name": final_name,
        "source": source,
    }


def ensure_canonical_subjects(conn) -> dict:
    """确保四科基线存在（幂等）。供首次接线与星图筛选器初始化使用。"""
    created, existing = [], []
    for code in CANONICAL_SUBJECTS:
        r = ensure_subject(conn, code)
        (created if r.get("created") else existing).append(code)
    return {"created": created, "existing": existing}


# ── LLM 分类 ────────────────────────────────────────────────
def _build_prompts(items: list[dict]) -> tuple[str, str]:
    """items: [{id, code, name, description?}] → (system, user)"""
    lines = []
    for it in items:
        desc = (it.get("description") or "").strip()
        lines.append(
            f"[{it['id']}] {it.get('code', '')} | {it['name']}"
            + (f" | 说明：{desc[:120]}" if desc else "")
        )
    subject_lines = []
    for code, meta in CANONICAL_SUBJECTS.items():
        subject_lines.append(
            f"- {code}（{meta['name']}）：细分领域示例 {'/'.join(meta['domains'])}"
        )
    system = (
        "你是小学课程分类专家。给出一批知识点，为每个判定：\n"
        "1. subject：考查的**核心能力**归属哪个学科。四科标准码：\n"
        + "\n".join(subject_lines)
        + "\n"
        "   若确属四科之外的其他学科（如道德与法治、信息技术），可给出新学科码"
        "与中文名——不要硬塞进四科。\n"
        "2. domain：该学科内的细分领域（用学科通用名；四科请优先用上面示例中的名称）。\n"
        "3. parent_code：若该知识点隶属一个更上位的知识点，给出其 code；没有则 null。\n"
        "4. confidence：0.0-1.0，信息不足时给低值。\n"
        "只输出一个 JSON 对象，不要 markdown 代码块：\n"
        '{"items":[{"id":<编号>,"subject":"<学科码>","subject_name":"<中文名，'
        '仅新学科需要>","domain":"<细分领域>","parent_code":null,'
        '"confidence":0.9,"rationale":"<一句话依据>"}]}'
    )
    user = "【知识点清单】（[编号] code | 名称 | 说明）\n" + "\n".join(lines) + "\n\n请分类。"
    return system, user


def _single_run(items: list[dict], client) -> dict:
    """一次 LLM 调用 + 解析。返回 {source_layer, parsed_map}。"""
    system, user = _build_prompts(items)
    chain = run_chain(system, user, client=client, version_stamp=PROMPT_VERSION)
    parsed = parse_llm_json(chain.answer)
    out: dict = {"source_layer": chain.source, "map": {}}
    if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
        for it in parsed["items"]:
            if not isinstance(it, dict):
                continue
            try:
                k = int(it.get("id"))
            except (TypeError, ValueError):
                continue
            out["map"][k] = it
    return out


def classify_kp(
    conn,
    kp_id: int,
    *,
    client: MiniMaxClient | None = None,
    confirm_runs: int | None = None,
    write: bool = True,
) -> dict:
    """单条 KP 分类：LLM 判 subject/domain/parent → 必要时自动新增学科 → 写回。

    返回 {ok, kp_id, code, subject, subject_id, subject_created, domain,
          parent_code, confidence, source_layer, agreed, errors}
    """
    result: dict = {"ok": False, "kp_id": kp_id, "errors": []}
    try:
        row = conn.execute(
            "SELECT id, code, name, description, subject_id FROM knowledge_point WHERE id=?",
            (kp_id,),
        ).fetchone()
        if not row:
            result["errors"].append(f"知识点不存在：{kp_id}")
            return result
        items = [{"id": row[0], "code": row[1], "name": row[2], "description": row[3]}]
        runs = AUTO_CONFIRM_RUNS if confirm_runs is None else max(1, int(confirm_runs))

        first = _single_run(items, client)
        got = first["map"].get(kp_id)
        result["source_layer"] = first["source_layer"]
        if not got:
            result["errors"].append(f"LLM 未返回该条判定（来源 {first['source_layer']}）")
            return result

        code_n = normalize_subject_code(got.get("subject"))
        conf = _clamp_conf(got.get("confidence"))
        domain = normalize_domain(got.get("domain"))
        parent_code = str(got.get("parent_code") or "").strip() or None
        agreed = True

        # 双跑一致闸门（沿用归因引擎纪律：高置信才自动写）
        if conf >= AUTO_ACCEPT_THRESHOLD and runs > 1:
            for i in range(runs - 1):
                nxt = _single_run(items, client)
                g2 = nxt["map"].get(kp_id) or {}
                # 复核跑技术性失败（未返回该条）→ 重试；语义不一致仍转人工
                retries = 0
                while retries < CONFIRM_RETRY_MAX and not g2:
                    retries += 1
                    logger.warning(
                        "分类复核跑 #%s 未返回该条，重试 %s/%s（kp=%s）",
                        i + 2,
                        retries,
                        CONFIRM_RETRY_MAX,
                        kp_id,
                    )
                    nxt = _single_run(items, client)
                    g2 = nxt["map"].get(kp_id) or {}
                c2 = normalize_subject_code(g2.get("subject"))
                if c2 != code_n or _clamp_conf(g2.get("confidence")) < AUTO_ACCEPT_THRESHOLD:
                    agreed = False
                    result["errors"].append(
                        f"复核跑 #{i + 2} 不一致：subject={c2 or '空'} "
                        f"conf={g2.get('confidence')}"
                        + (f"（重试 {retries} 次）" if retries else "")
                    )
                    break
        result.update(agreed=agreed, confidence=conf, domain=domain, parent_code=parent_code)

        low_conf = conf < AUTO_ACCEPT_THRESHOLD or not agreed
        if low_conf:
            result.update(
                ok=True,
                subject=code_n,
                subject_id=None,
                subject_created=False,
                needs_review=True,
                rationale=str(got.get("rationale") or "")[:200],
            )
            if write:
                result["errors"].append("低置信/不一致 → 不写库，转人工复核")
            return result

        # 学科自动新增（触发点②：分类结果写出时）
        sub = ensure_subject(
            conn,
            code_n,
            name=str(got.get("subject_name") or "").strip() or None,
            source="LLM",
        )
        if not sub.get("ok"):
            result["errors"].append(f"学科确保失败：{sub.get('error')}")
            return result
        result.update(
            ok=True,
            subject=sub["code"],
            subject_id=sub["subject_id"],
            subject_created=sub["created"],
        )

        if write:
            # parent_code → parent_id（找不到父节点则留 NULL，不阻断）
            parent_id = None
            if parent_code:
                p = conn.execute(
                    "SELECT id FROM knowledge_point WHERE code=?", (parent_code,)
                ).fetchone()
                parent_id = p[0] if p else None
            conn.execute(
                "UPDATE knowledge_point SET subject_id=?, knowledge_domain=?,"
                " parent_id=?, updated_at=? WHERE id=?",
                (sub["subject_id"], domain, parent_id, fmt_ts(now_utc()), kp_id),
            )
            conn.commit()
            result["written"] = True
        result["rationale"] = str(got.get("rationale") or "")[:200]
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("知识点分类失败 kp_id=%s", kp_id)
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        return result


def classify_kp_batch(
    conn,
    *,
    kp_ids: list[int] | None = None,
    client: MiniMaxClient | None = None,
    limit: int = 100,
    write: bool = True,
) -> dict:
    """批量分类（一次打包送 LLM——省 token 且能横向比较）。

    默认处理全部未分类的 KP（subject 已默认 MATH 但 knowledge_domain 为空的）。
    返回 {ok, total, classified, subjects_created, subjects_used, needs_review, errors}
    """
    out: dict = {
        "ok": False,
        "total": 0,
        "classified": 0,
        "subjects_created": [],
        "subjects_used": {},
        "needs_review": [],
        "errors": [],
    }
    try:
        if kp_ids:
            q = ",".join("?" * len(kp_ids))
            rows = conn.execute(
                f"SELECT id, code, name, description FROM knowledge_point"
                f" WHERE id IN ({q}) ORDER BY id",
                kp_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, code, name, description FROM knowledge_point"
                " WHERE knowledge_domain IS NULL OR knowledge_domain = ''"
                " ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        items = [{"id": r[0], "code": r[1], "name": r[2], "description": r[3]} for r in rows]
        out["total"] = len(items)
        if not items:
            out["ok"] = True
            return out

        resp = _single_run(items, client)
        out["source_layer"] = resp["source_layer"]
        if not resp["map"]:
            out["errors"].append(f"LLM 未返回有效结果（来源 {resp['source_layer']}）")
            return out

        stamp = fmt_ts(now_utc())
        for it in items:
            kp_id, code, _name = it["id"], it["code"], it["name"]
            got = resp["map"].get(kp_id)
            if not got:
                out["needs_review"].append(
                    {"kp_id": kp_id, "code": code, "reason": "LLM 未覆盖该条"}
                )
                continue
            code_n = normalize_subject_code(got.get("subject"))
            conf = _clamp_conf(got.get("confidence"))
            if conf < AUTO_ACCEPT_THRESHOLD:
                out["needs_review"].append(
                    {
                        "kp_id": kp_id,
                        "code": code,
                        "subject": code_n,
                        "confidence": conf,
                        "rationale": str(got.get("rationale") or "")[:120],
                    }
                )
                continue
            sub = ensure_subject(
                conn,
                code_n,
                name=str(got.get("subject_name") or "").strip() or None,
                source="LLM",
            )
            if not sub.get("ok"):
                out["errors"].append(f"{code}: {sub.get('error')}")
                continue
            if sub.get("created"):
                out["subjects_created"].append(sub["code"])
            out["subjects_used"][sub["code"]] = out["subjects_used"].get(sub["code"], 0) + 1
            domain = normalize_domain(got.get("domain"))
            parent_code = str(got.get("parent_code") or "").strip() or None
            parent_id = None
            if parent_code:
                p = conn.execute(
                    "SELECT id FROM knowledge_point WHERE code=?", (parent_code,)
                ).fetchone()
                parent_id = p[0] if p else None
            if write:
                conn.execute(
                    "UPDATE knowledge_point SET subject_id=?, knowledge_domain=?,"
                    " parent_id=?, updated_at=? WHERE id=?",
                    (sub["subject_id"], domain, parent_id, stamp, kp_id),
                )
            out["classified"] += 1
        if write:
            conn.commit()
        out["ok"] = True
        return out
    except Exception as exc:  # noqa: BLE001
        logger.exception("批量分类失败")
        out["errors"].append(f"{type(exc).__name__}: {exc}")
        return out
