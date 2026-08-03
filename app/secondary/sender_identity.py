from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional


SENDER_IDENTITY_MAP = (
    Path(__file__).resolve().parents[2]
    / "skill"
    / "generate-secondary-lead-message"
    / "references"
    / "sender-identity-map.md"
)


@lru_cache(maxsize=1)
def _sender_identities() -> Dict[str, Dict[str, str]]:
    identities = {}
    for line in SENDER_IDENTITY_MAP.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip().strip("`") for cell in line.split("|")[1:-1]]
        if len(cells) == 3 and cells[0] not in {
            "CRM creator/member name",
            "---",
        }:
            identities[" ".join(cells[0].split())] = {
                "display_name": cells[1],
                "account": cells[2],
            }
    return identities


def resolve_sender_identity(sales_name: object) -> Optional[Dict[str, str]]:
    if not isinstance(sales_name, str):
        return None
    identity = _sender_identities().get(" ".join(sales_name.split()))
    return dict(identity) if identity is not None else None
