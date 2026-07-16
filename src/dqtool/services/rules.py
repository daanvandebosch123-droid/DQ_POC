from __future__ import annotations

from dqtool.models.entities import Rule, RuleGroup, RuleType

RULE_TEMPLATES: dict[RuleType, dict[str, str]] = {
    RuleType.NOT_NULL: {
        "name": "Not Null Check",
        "description": "Fails every row where the selected field contains a database null value.",
        "setup": "Choose the source connection, table or file, and the field that must always contain a value. Empty text is not considered null, so use an additional rule when blank strings must also fail.",
    },
    RuleType.UNIQUE: {
        "name": "Unique Check",
        "description": "Fails every row whose selected field or combination of fields occurs more than once.",
        "setup": "Add one field for a simple unique key, or add multiple fields for a composite key. Every row belonging to a duplicated key is returned as failed.",
    },
    RuleType.DUPLICATE: {
        "name": "Duplicate Check",
        "description": "Finds rows that share the same values in the selected fields.",
        "setup": "Add the fields that define a duplicate. To detect completely identical rows, add every relevant field. All copies of each duplicate are returned.",
    },
    RuleType.ROW_COUNT: {
        "name": "Row Count Check",
        "description": "Checks whether the selected source row count falls inside an inclusive minimum and maximum range.",
        "setup": "Enter the smallest and largest acceptable row counts. Set a very large maximum when only a minimum is important, or set both values equal for an exact count.",
    },
    RuleType.VALUE_RANGE: {
        "name": "Value Range Check",
        "description": "Fails numeric values below the minimum or above the maximum; both boundary values are accepted.",
        "setup": "Choose a numeric field and enter its allowed minimum and maximum. Null values are ignored, so combine this with Not Null when values are mandatory.",
    },
    RuleType.REGEX: {
        "name": "Regex Check",
        "description": "Fails text values that do not match the supplied regular-expression pattern.",
        "setup": "Choose a text field and enter a regular expression, for example ^[^@]+@[^@]+$ for a basic email shape. Null values are ignored unless a Not Null rule is also added.",
    },
    RuleType.LENGTH: {
        "name": "Length Check",
        "description": "Checks that the text representation of a value has an inclusive minimum and maximum length.",
        "setup": "Choose a field and enter the accepted character-length range. Null values are ignored; spaces count as characters.",
    },
    RuleType.ALLOWED_VALUES: {
        "name": "Allowed Values Check",
        "description": "Fails values that are not present in a fixed list of accepted values.",
        "setup": "Choose a field and enter accepted values separated by commas, such as ACTIVE, INACTIVE. Matching is exact and null values are ignored.",
    },
    RuleType.DATE_VALIDITY: {
        "name": "Date Validity Check",
        "description": "Fails values that cannot be converted into a valid date; null values also fail this check.",
        "setup": "Choose the field that should contain dates. Use a custom SQL rule when a specific date format or minimum/maximum date must be enforced.",
    },
    RuleType.CUSTOM_SQL_FAIL_ROWS: {
        "name": "Custom SQL Failed Rows",
        "description": "Runs custom SQL where every row returned by the query is treated as a failed row.",
        "setup": "Write a SELECT query that returns only invalid rows. For CSV sources, query dataset_view. Do not return valid rows, because any returned row counts as a failure.",
    },
    RuleType.CUSTOM_SQL_THRESHOLD: {
        "name": "Custom SQL Threshold",
        "description": "Runs SQL that returns a metric named value, then fails when that metric satisfies the selected comparison against the threshold.",
        "setup": "Write a query returning one numeric column aliased as value, select an operator, and enter the threshold. Example: SELECT COUNT(*) AS value FROM dataset_view WHERE amount < 0.",
    },
    RuleType.CUSTOM_SQL_CONNECTION: {
        "name": "Custom SQL (Whole Connection)",
        "description": "Runs custom SQL against the entire connection instead of one table; every returned row is a failed row.",
        "setup": "Choose only the connection and write a SELECT that may reference any table in it, including joins across tables. On a CSV connection every file is available as a view named after the file (customers.csv becomes customers). Return only invalid rows.",
    },
    RuleType.REFERENTIAL_INTEGRITY: {
        "name": "Referential Integrity",
        "description": "Fails source rows whose non-null key does not exist in the selected target source.",
        "setup": "Choose the source key, then choose the target connection and table or file plus its matching target key. Null source keys are ignored; add Not Null separately when they must fail.",
    },
    RuleType.KEYED_COMPARISON: {
        "name": "Keyed Comparison",
        "description": "Joins source and target records by a key and fails rows where any selected comparison field differs.",
        "setup": "Choose the key and comparison fields, then enter a target relation available in the same query engine. This is an advanced rule; use matching field names on both sides.",
    },
}


RULE_CONFIG_EXAMPLES: dict[RuleType, dict] = {
    RuleType.NOT_NULL: {"column": "customer_id"},
    RuleType.UNIQUE: {"columns": ["customer_id"]},
    RuleType.DUPLICATE: {"columns": ["customer_id"]},
    RuleType.ROW_COUNT: {"min_count": 1, "max_count": 100000},
    RuleType.VALUE_RANGE: {"column": "amount", "min": 0, "max": 1000},
    RuleType.REGEX: {"column": "email", "pattern": "^[^@]+@[^@]+$"},
    RuleType.LENGTH: {"column": "code", "min_length": 1, "max_length": 20},
    RuleType.ALLOWED_VALUES: {"column": "status", "values": ["ACTIVE", "INACTIVE"]},
    RuleType.DATE_VALIDITY: {"column": "invoice_date"},
    RuleType.CUSTOM_SQL_FAIL_ROWS: {"sql": "SELECT * FROM dataset_view WHERE ..."},
    RuleType.CUSTOM_SQL_THRESHOLD: {
        "sql": "SELECT COUNT(*) AS value FROM dataset_view",
        "operator": ">",
        "threshold": 0,
    },
    RuleType.CUSTOM_SQL_CONNECTION: {
        "sql": "SELECT o.* FROM orders o LEFT JOIN customers c ON o.customer_id = c.customer_id WHERE c.customer_id IS NULL",
    },
    RuleType.REFERENTIAL_INTEGRITY: {
        "source_key": "customer_id",
        "target_connection_id": 2,
        "target_kind": "oracle_table",
        "target_name": "CUSTOMERS",
        "target_key": "customer_id",
    },
    RuleType.KEYED_COMPARISON: {
        "key_column": "customer_id",
        "compare_columns": ["name"],
        "target_relation": "customers_target",
    },
}


RULE_REQUIRED_CONFIG: dict[RuleType, tuple[str, ...]] = {
    RuleType.NOT_NULL: ("column",),
    RuleType.UNIQUE: ("columns",),
    RuleType.DUPLICATE: ("columns",),
    RuleType.ROW_COUNT: (),
    RuleType.VALUE_RANGE: ("column", "min", "max"),
    RuleType.REGEX: ("column", "pattern"),
    RuleType.LENGTH: ("column",),
    RuleType.ALLOWED_VALUES: ("column", "values"),
    RuleType.DATE_VALIDITY: ("column",),
    RuleType.CUSTOM_SQL_FAIL_ROWS: ("sql",),
    RuleType.CUSTOM_SQL_THRESHOLD: ("sql", "threshold"),
    RuleType.CUSTOM_SQL_CONNECTION: ("sql",),
    RuleType.REFERENTIAL_INTEGRITY: ("source_key", "target_key"),
    RuleType.KEYED_COMPARISON: ("key_column", "compare_columns", "target_relation"),
}


def normalize_rule_config(rule_type: RuleType, config: dict) -> dict:
    normalized = dict(config)
    if rule_type in {RuleType.UNIQUE, RuleType.DUPLICATE} and not normalized.get("columns"):
        column = normalized.get("column")
        if column:
            normalized["columns"] = [column]
    return normalized


def validate_rule_config(rule_type: RuleType, config: dict, *, require_source: bool = False) -> list[str]:
    normalized = normalize_rule_config(rule_type, config)
    missing = [key for key in RULE_REQUIRED_CONFIG[rule_type] if normalized.get(key) in (None, "", [])]
    errors = [f"Missing required setting: {key}" for key in missing]
    if require_source:
        errors.extend(_validate_source_reference(normalized, "source"))
    if rule_type == RuleType.REFERENTIAL_INTEGRITY:
        if require_source or "source_connection_id" in normalized:
            errors.extend(_validate_source_reference(normalized, "target"))
        else:
            target_dataset_id = normalized.get("target_dataset_id")
            try:
                valid_target_dataset = not isinstance(target_dataset_id, bool) and int(target_dataset_id) > 0
            except (TypeError, ValueError):
                valid_target_dataset = False
            if not valid_target_dataset:
                errors.append("Target dataset is required.")
    if rule_type in {RuleType.UNIQUE, RuleType.DUPLICATE} and "columns" not in missing:
        if not isinstance(normalized["columns"], list):
            errors.append("The columns setting must be a JSON list, for example [\"customer_id\"].")
    if rule_type == RuleType.ALLOWED_VALUES and "values" not in missing:
        if not isinstance(normalized["values"], list):
            errors.append("The values setting must be a JSON list.")
    errors.extend(_validate_fail_threshold(normalized))
    return list(dict.fromkeys(errors))


def _validate_fail_threshold(config: dict) -> list[str]:
    """fail_threshold_count/fail_threshold_percent are optional and apply to every rule type:
    a run only fails once failed rows exceed whichever tolerance is configured."""
    errors: list[str] = []
    if "fail_threshold_count" in config and config["fail_threshold_count"] not in (None, ""):
        try:
            if int(config["fail_threshold_count"]) < 0:
                errors.append("Fail threshold (rows) cannot be negative.")
        except (TypeError, ValueError):
            errors.append("Fail threshold (rows) must be a whole number.")
    if "fail_threshold_percent" in config and config["fail_threshold_percent"] not in (None, ""):
        try:
            percent = float(config["fail_threshold_percent"])
            if not (0 <= percent <= 100):
                errors.append("Fail threshold (%) must be between 0 and 100.")
        except (TypeError, ValueError):
            errors.append("Fail threshold (%) must be a number.")
    return errors


def _validate_source_reference(config: dict, prefix: str) -> list[str]:
    connection_key = f"{prefix}_connection_id"
    kind_key = f"{prefix}_kind"
    name_key = f"{prefix}_name"
    sql_key = f"{prefix}_sql"
    label = prefix.capitalize()
    errors: list[str] = []

    connection_id = config.get(connection_key)
    try:
        valid_connection_id = not isinstance(connection_id, bool) and int(connection_id) > 0
    except (TypeError, ValueError):
        valid_connection_id = False
    if not valid_connection_id:
        errors.append(f"{label} connection is required.")

    source_kind = config.get(kind_key)
    if source_kind == "connection":
        # Whole-connection rules need no table or file selection.
        return errors
    if source_kind not in {"csv_file", "oracle_table", "oracle_sql"}:
        errors.append(f"{label} type is required.")
    elif source_kind == "oracle_sql":
        if not str(config.get(sql_key) or "").strip():
            errors.append(f"{label} SQL is required for a custom SQL source.")
    elif not str(config.get(name_key) or "").strip():
        errors.append(f"{label} table or CSV file is required.")
    return errors


def would_create_cycle(
    group_id: int | None,
    candidate_child_ids: list[int],
    groups_by_id: dict[int, RuleGroup],
) -> bool:
    """Check whether nesting candidate_child_ids under group_id would create a cycle.

    A new (not yet saved) group can never be another group's ancestor yet, so this is
    only meaningful when editing an existing group. The check walks downward from each
    candidate child through its own child_group_ids; if that walk ever reaches group_id,
    adding the edge group_id -> candidate would close a loop.
    """
    if group_id is None:
        return False
    stack = list(candidate_child_ids)
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if current == group_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        child = groups_by_id.get(current)
        if child is None:
            continue
        stack.extend(child.child_group_ids)
    return False


def ancestor_group_ids(group_id: int, groups_by_id: dict[int, RuleGroup]) -> set[int]:
    """Return every group that (directly or transitively) contains group_id as a child.

    Used by the UI to exclude invalid choices from the "subgroups" picker: nesting one
    of these under the group being edited would create a cycle.
    """
    ancestors: set[int] = set()
    for candidate_id, candidate in groups_by_id.items():
        if candidate_id == group_id:
            continue
        if would_create_cycle(group_id, [candidate_id], groups_by_id):
            ancestors.add(candidate_id)
    return ancestors


def resolve_group_rules(
    group: RuleGroup,
    groups_by_id: dict[int, RuleGroup],
    rules_by_id: dict[int, Rule],
    _visited: set[int] | None = None,
) -> tuple[list[Rule], int, int]:
    """Flatten a rule group's direct and nested rules into one deduplicated list.

    Returns (rules, missing_rules, missing_groups). The counts include ids that could
    not be resolved because they were deleted or, when called with a caller-filtered
    rules_by_id/groups_by_id, because the current user cannot view them. Rules reachable
    through more than one path (direct membership plus one or more subgroups) are
    included once, in first-seen depth-first order. The _visited set guards against
    cycles defensively; save-time validation (would_create_cycle) should prevent them
    from ever being persisted.
    """
    visited = _visited if _visited is not None else set()
    if group.id is not None:
        visited.add(group.id)

    seen_rule_ids: set[int] = set()
    rules: list[Rule] = []
    missing_rules = 0
    missing_groups = 0

    for rule_id in group.rule_ids:
        rule = rules_by_id.get(rule_id)
        if rule is None:
            missing_rules += 1
        elif rule_id not in seen_rule_ids:
            seen_rule_ids.add(rule_id)
            rules.append(rule)

    for child_id in group.child_group_ids:
        if child_id in visited:
            continue
        child = groups_by_id.get(child_id)
        if child is None:
            missing_groups += 1
            continue
        child_rules, child_missing_rules, child_missing_groups = resolve_group_rules(
            child, groups_by_id, rules_by_id, visited
        )
        missing_rules += child_missing_rules
        missing_groups += child_missing_groups
        for rule in child_rules:
            if rule.id is not None and rule.id not in seen_rule_ids:
                seen_rule_ids.add(rule.id)
                rules.append(rule)

    return rules, missing_rules, missing_groups
