"""Range continuation errors must tell the model how to recover."""

from types import SimpleNamespace
from uuid import uuid4

from app.auth.principal import Principal
from app.core.errors import ValidationError
from app.domain.entities import Role
from app.services.tools.registry import get_tool
from app.services.tools.types import ToolContext


async def test_read_document_preserves_safe_range_recovery_hint() -> None:
    async def read_document(**kwargs: object) -> None:
        raise ValidationError("Overlapping passages: increase max_passages or narrow the range.")

    context = ToolContext(
        principal=Principal(user_id=uuid4(), tenant_id=uuid4(), roles=(Role.MEMBER,)),
        retrieval=SimpleNamespace(read_document=read_document),  # type: ignore[arg-type]
    )
    result = await get_tool("read_document").handler({"document": str(uuid4())}, context)
    assert not result.ok
    assert "increase max_passages" in result.content
