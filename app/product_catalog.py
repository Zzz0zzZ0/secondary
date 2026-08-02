import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Optional


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SKILL_FACTS = (
    PROJECT_DIR
    / "skill"
    / "generate-secondary-lead-message"
    / "references"
    / "business-facts.md"
)
DEFAULT_REPORT_PATH = PROJECT_DIR / "outputs" / "product-catalog-comparison.json"
SCHEMA_PATTERN = re.compile(r"^workspace_[a-z0-9]+$")
NUMBERED_ITEM_PATTERN = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$")
PARENTHETICAL_PATTERN = re.compile(r"\(([^()]*)\)")
INCLUDING_PATTERN = re.compile(r",?\s+including\s+", re.IGNORECASE)


def normalize_product_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def load_skill_portfolio(path: Path = DEFAULT_SKILL_FACTS) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    in_portfolio = False
    products: list[dict[str, Any]] = []
    for line in lines:
        if line.strip() == "## Product Portfolio":
            in_portfolio = True
            continue
        if in_portfolio and line.startswith("## "):
            break
        if not in_portfolio:
            continue
        match = NUMBERED_ITEM_PATTERN.match(line)
        if match:
            products.append(
                {
                    "index": int(match.group(1)),
                    "name": match.group(2),
                }
            )
    if not products:
        raise RuntimeError(f"No Product Portfolio list found in {path}")
    return products


def _split_aliases(value: str) -> Iterable[str]:
    yield value
    for separator in (" and ", " / "):
        if separator in value:
            yield from value.split(separator)


def skill_product_aliases(name: str) -> list[str]:
    aliases: list[str] = []

    def add(value: str) -> None:
        cleaned = value.strip(" ,:-")
        normalized = normalize_product_name(cleaned)
        if cleaned and normalized and cleaned not in aliases:
            aliases.append(cleaned)

    parenthetical_values = PARENTHETICAL_PATTERN.findall(name)
    without_parentheses = PARENTHETICAL_PATTERN.sub("", name).strip()
    before_colon = without_parentheses.split(":", 1)[0]
    including_parts = INCLUDING_PATTERN.split(before_colon, maxsplit=1)

    for alias in _split_aliases(including_parts[0]):
        add(alias)
    if len(including_parts) == 2:
        add(including_parts[1])
    for value in parenthetical_values:
        if value.casefold().startswith("hs code "):
            continue
        for alias in _split_aliases(value):
            add(alias)
    return aliases


def _match_score(left: str, right: str) -> float:
    normalized_left = normalize_product_name(left)
    normalized_right = normalize_product_name(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    shorter, longer = sorted(
        (normalized_left, normalized_right),
        key=len,
    )
    if len(shorter) >= 4 and shorter in longer:
        return 0.9
    return 0.0


def _crm_names(product: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for field in ("name", "ename"):
        value = product.get(field)
        if isinstance(value, str) and value.strip() and value.strip() not in names:
            names.append(value.strip())
    return names


def compare_catalogs(
    crm_products: list[dict[str, Any]],
    skill_products: list[dict[str, Any]],
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    matched_crm_ids: set[str] = set()
    matched_skill_indexes: set[int] = set()

    for crm_product in crm_products:
        crm_identity = str(crm_product.get("id") or "")
        crm_names = _crm_names(crm_product)
        best: Optional[dict[str, Any]] = None
        for skill_product in skill_products:
            aliases = skill_product_aliases(str(skill_product["name"]))
            for crm_name in crm_names:
                for alias in aliases:
                    score = _match_score(crm_name, alias)
                    if score and (best is None or score > best["score"]):
                        best = {
                            "crm_id": crm_identity,
                            "crm_name": crm_product.get("name"),
                            "crm_ename": crm_product.get("ename"),
                            "skill_index": int(skill_product["index"]),
                            "skill_name": skill_product["name"],
                            "matched_crm_value": crm_name,
                            "matched_skill_alias": alias,
                            "score": score,
                        }
        if best is not None:
            matches.append(best)
            matched_crm_ids.add(crm_identity)
            matched_skill_indexes.add(best["skill_index"])

    crm_only = [
        product
        for product in crm_products
        if str(product.get("id") or "") not in matched_crm_ids
    ]
    skill_only = [
        product
        for product in skill_products
        if int(product["index"]) not in matched_skill_indexes
    ]
    status = "aligned" if not crm_only and not skill_only else "differences_found"
    return {
        "status": status,
        "judgment": (
            "CRM active products and the Skill portfolio cover each other."
            if status == "aligned"
            else "CRM active products and the Skill portfolio are not fully aligned."
        ),
        "counts": {
            "crm_products": len(crm_products),
            "skill_product_families": len(skill_products),
            "matched_crm_products": len(matched_crm_ids),
            "covered_skill_families": len(matched_skill_indexes),
            "crm_only": len(crm_only),
            "skill_only": len(skill_only),
        },
        "matches": matches,
        "crm_only": crm_only,
        "skill_only": skill_only,
    }


def fetch_crm_products(environment: Optional[dict[str, str]] = None) -> list[dict[str, Any]]:
    environment = environment or dict(os.environ)
    required = ["TWENTY_DB_HOST", "TWENTY_DB_NAME", "TWENTY_DB_USER"]
    missing = [name for name in required if not environment.get(name)]
    if missing:
        raise RuntimeError(
            "Missing Twenty database configuration: " + ", ".join(missing)
        )
    password = environment.get("TWENTY_DB_PASSWORD")
    if password is None:
        raise RuntimeError("TWENTY_DB_PASSWORD is required")
    schema = environment.get(
        "TWENTY_WORKSPACE_SCHEMA",
        "workspace_avv74ijhm70d2bh7o1toe4anv",
    )
    if not SCHEMA_PATTERN.fullmatch(schema):
        raise RuntimeError(f"TWENTY_WORKSPACE_SCHEMA has an invalid format: {schema}")

    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise RuntimeError("Missing dependency. Run: ./scripts/setup_python.sh") from exc

    with psycopg.connect(
        host=environment["TWENTY_DB_HOST"],
        port=int(environment.get("TWENTY_DB_PORT", "5432")),
        dbname=environment["TWENTY_DB_NAME"],
        user=environment["TWENTY_DB_USER"],
        password=password,
        sslmode=environment.get("TWENTY_DB_SSLMODE", "prefer"),
        connect_timeout=int(environment.get("TWENTY_DB_CONNECT_TIMEOUT", "5")),
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("BEGIN READ ONLY")
            cursor.execute(
                sql.SQL(
                    """
                    SELECT id::text, name, ename
                    FROM {}._product
                    WHERE "deletedAt" IS NULL
                    ORDER BY lower(COALESCE(ename, name)), id
                    """
                ).format(sql.Identifier(schema))
            )
            return [
                {"id": row[0], "name": row[1], "ename": row[2]}
                for row in cursor.fetchall()
            ]


def build_report(
    skill_path: Path = DEFAULT_SKILL_FACTS,
    environment: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    crm_products = fetch_crm_products(environment)
    skill_products = load_skill_portfolio(skill_path)
    comparison = compare_catalogs(crm_products, skill_products)
    return {
        "source": {
            "crm_table": "_product",
            "crm_filter": '"deletedAt" IS NULL',
            "skill_file": str(skill_path.resolve()),
        },
        "crm_products": crm_products,
        "skill_products": [
            {
                **product,
                "aliases": skill_product_aliases(str(product["name"])),
            }
            for product in skill_products
        ],
        "comparison": comparison,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare Twenty CRM products with the Hermes Skill portfolio"
    )
    parser.add_argument("--skill", type=Path, default=DEFAULT_SKILL_FACTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT_PATH)
    args = parser.parse_args(argv)

    report = build_report(args.skill)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Report written: {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
