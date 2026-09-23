"""User accounts. Users are global; what they can do is decided per organization."""

from fastapi import APIRouter, status

from app.api.dependencies import PrincipalDep, SessionDep
from app.api.errors import problem_responses
from app.domain.errors import NotFoundError
from app.repositories.users import UserRepository
from app.schemas.api import UserCreate, UserOut
from app.services.users import UserService

router = APIRouter(prefix="/users", tags=["users"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Register a user",
    responses=problem_responses(409, 422),
)
async def register_user(body: UserCreate, session: SessionDep) -> UserOut:
    """Open registration for local development. Phase 13 replaces it with OAuth sign-in."""
    user = await UserService(session).register(email=body.email, display_name=body.display_name)
    return UserOut.model_validate(user)


@router.get("/me", summary="The calling user", responses=problem_responses(401, 404))
async def get_me(principal: PrincipalDep, session: SessionDep) -> UserOut:
    async with session.begin():
        user = await UserRepository(session).get(principal)
    if user is None:
        raise NotFoundError("user not found")
    return UserOut.model_validate(user)
