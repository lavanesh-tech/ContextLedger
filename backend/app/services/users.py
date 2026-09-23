from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import ConflictError
from app.domain.validation import normalize_email, normalize_name
from app.models.user import User
from app.repositories.users import UserRepository


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)

    async def register(self, *, email: str, display_name: str) -> User:
        """Create a user. Emails are unique case-insensitively (stored lower-cased)."""
        normalized_email = normalize_email(email)
        name = normalize_name(display_name, field="display_name")

        async with self._session.begin():
            if await self._users.get_by_email(normalized_email) is not None:
                raise ConflictError("a user with this email already exists")
            user = User(email=normalized_email, display_name=name)
            self._users.add(user)
            try:
                await self._session.flush()
            except IntegrityError as exc:  # lost a race with a concurrent registration
                raise ConflictError("a user with this email already exists") from exc
        return user
