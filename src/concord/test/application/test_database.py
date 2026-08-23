from concord.application.database import Database


def test_initialize_creates_database(tmp_path):
    database_path = tmp_path / "concord.db"
    database = Database(database_path)
    database.initialize()
    assert database_path.exists()


def test_initialize_creates_targets_table(tmp_path):
    database_path = tmp_path / "concord.db"
    database = Database(database_path)
    database.initialize()
    with database.connect() as connection:
        tables = connection.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """).fetchall()
    assert ("targets",) in tables


def test_connect(tmp_path):
    database = Database(tmp_path / "concord.db")
    database.initialize()
    with database.connect() as connection:
        result = connection.execute("SELECT 1").fetchone()
    assert result == (1,)


def test_targets_table_schema(tmp_path):
    database = Database(tmp_path / "concord.db")
    database.initialize()
    with database.connect() as connection:
        columns = connection.execute("PRAGMA table_info(targets)").fetchall()
    column_names = {column[1] for column in columns}
    assert column_names == {
        "id",
        "name",
        "local_path",
        "type",
        "created_at",
    }


def test_initialize_is_idempotent(tmp_path):
    database = Database(tmp_path / "concord.db")
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        tables = connection.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """).fetchall()
    assert ("targets",) in tables
