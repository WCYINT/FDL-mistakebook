"""轻量 JSON Schema 校验器（P2-03 / KB-02）。

支持的 schema 子集（业余项目够用，零第三方依赖）：
- `type`：字符串或数组（多类型，如 `["integer", "null"]`）
- `required` / `properties`（对象递归）
- `enum` / `const`
- `minimum` / `maximum`（数值范围）
- `items`（数组元素递归）
- `pattern`（字符串正则，`re.search` 语义）

未知关键字（`$schema`/`title`/`description`/`format` 等元信息）忽略。
"""

from __future__ import annotations

import re

_TYPE_MAP: dict[str, type] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def _check_type(value, expected: str) -> bool:
    """判断 value 是否符合 JSON Schema 类型（排除 bool 是 int 子类的歧义）。"""
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    py = _TYPE_MAP.get(expected)
    if py is None:
        return True  # 未知类型名不拦截
    return isinstance(value, py)


def _validate(value, schema: dict, path: str, errors: list[str]) -> None:
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_check_type(value, x) for x in types):
            errors.append(f"{path}: 期望类型 {t}，实际 {type(value).__name__}")
            return  # 类型错误后不再继续校验其他关键字

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: 值 {value!r} 不在枚举 {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: 值 {value!r} 不等于常量 {schema['const']!r}")

    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < 最小值 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > 最大值 {schema['maximum']}")

    if isinstance(value, str) and "pattern" in schema:
        if not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} 不匹配模式 {schema['pattern']}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: 缺必填字段 '{key}'")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                _validate(value[key], sub, f"{path}.{key}", errors)

    if isinstance(value, list) and "items" in schema:
        for idx, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{idx}]", errors)


def validate(instance, schema: dict) -> list[str]:
    """校验 instance 是否符合 schema；返回错误列表（空 = 通过）。"""
    errors: list[str] = []
    _validate(instance, schema, "$", errors)
    return errors
