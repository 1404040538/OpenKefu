"""Data-access repositories. All SQL for a business domain lives in its repository class.

Repositories are pure data access: no HTTP semantics, no business rules.
Callers (routers, AppContext, runtime) keep validation/authorization logic.
"""

from openkefu.web.repositories.conversations import ConversationsRepository
from openkefu.web.repositories.logs import RuntimeLogsRepository
from openkefu.web.repositories.return_records import ReturnRecordsRepository
from openkefu.web.repositories.shops import ShopsRepository
from openkefu.web.repositories.users import UsersRepository


class Repositories:
    """Bundle of domain repositories sharing one Database connection pool."""

    def __init__(self, db):
        self.db = db
        self.users = UsersRepository(db)
        self.shops = ShopsRepository(db)
        self.conversations = ConversationsRepository(db)
        self.return_records = ReturnRecordsRepository(db)
        self.logs = RuntimeLogsRepository(db)


__all__ = [
    "Repositories",
    "UsersRepository",
    "ShopsRepository",
    "ConversationsRepository",
    "ReturnRecordsRepository",
    "RuntimeLogsRepository",
]
