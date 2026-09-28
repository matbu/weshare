import psycopg
from psycopg.rows import dict_row

from .config import settings


async def connect(autocommit: bool = False) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(
        settings.database_url, row_factory=dict_row, autocommit=autocommit
    )
