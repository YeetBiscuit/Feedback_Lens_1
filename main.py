import sqlite3
from pathlib import Path

from build import initialise_database


DB_PATH = Path("feedback_system.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def get_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name;
        """
    ).fetchall()
    return [row["name"] for row in rows]


def get_table_info(conn: sqlite3.Connection, table_name: str) -> list[sqlite3.Row]:
    return conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()


def pick_table(conn: sqlite3.Connection) -> str | None:
    tables = get_tables(conn)
    if not tables:
        print("No tables found. Initialise the database first.")
        return None

    print("\nAvailable tables:")
    for idx, name in enumerate(tables, start=1):
        print(f"{idx}. {name}")

    choice = input("Choose table by number or name: ").strip()
    if not choice:
        return None

    if choice.isdigit():
        index = int(choice)
        if 1 <= index <= len(tables):
            return tables[index - 1]
        print("Invalid table number.")
        return None

    if choice in tables:
        return choice

    print("Unknown table name.")
    return None


def parse_value(raw_value: str, declared_type: str) -> object:
    if raw_value == "":
        return None

    value = raw_value.strip()
    value_upper = value.upper()
    if value_upper == "NULL":
        return None

    type_upper = declared_type.upper()
    if "INT" in type_upper:
        if value_upper in {"TRUE", "YES"}:
            return 1
        if value_upper in {"FALSE", "NO"}:
            return 0
        return int(value)
    if any(token in type_upper for token in ("REAL", "FLOA", "DOUB", "DEC")):
        return float(value)
    return value


def list_rows(conn: sqlite3.Connection) -> None:
    table = pick_table(conn)
    if not table:
        return

    limit_input = input("How many rows to show? [default 20]: ").strip()
    limit = 20
    if limit_input:
        try:
            limit = max(1, int(limit_input))
        except ValueError:
            print("Invalid limit. Using 20.")

    query = f'SELECT * FROM "{table}" LIMIT ?'
    rows = conn.execute(query, (limit,)).fetchall()
    print(f"\nRows from {table}:")
    if not rows:
        print("(no rows)")
        return

    for row in rows:
        print(dict(row))


def insert_row(conn: sqlite3.Connection) -> None:
    table = pick_table(conn)
    if not table:
        return

    columns = get_table_info(conn, table)
    insert_cols = []
    values = []

    for col in columns:
        col_name = col["name"]
        declared_type = col["type"] or "TEXT"
        is_pk = col["pk"] == 1

        if is_pk and "INT" in declared_type.upper():
            # Skip integer primary key to let SQLite auto-assign rowid.
            continue

        prompt = f"{col_name} ({declared_type})"
        if col["notnull"] and col["dflt_value"] is None:
            prompt += " [required]"
        if col["dflt_value"] is not None:
            prompt += f" [default {col['dflt_value']}]"
        prompt += ": "

        raw = input(prompt)
        if raw == "" and col["notnull"] and col["dflt_value"] is None:
            print(f"{col_name} is required.")
            return

        if raw == "":
            continue

        try:
            parsed = parse_value(raw, declared_type)
        except ValueError as err:
            print(f"Invalid value for {col_name}: {err}")
            return

        insert_cols.append(col_name)
        values.append(parsed)

    if not insert_cols:
        query = f'INSERT INTO "{table}" DEFAULT VALUES'
        conn.execute(query)
    else:
        cols_sql = ", ".join(f'"{col}"' for col in insert_cols)
        placeholders = ", ".join("?" for _ in insert_cols)
        query = f'INSERT INTO "{table}" ({cols_sql}) VALUES ({placeholders})'
        conn.execute(query, values)

    conn.commit()
    print("Row inserted.")


def find_primary_key(columns: list[sqlite3.Row]) -> sqlite3.Row | None:
    for col in columns:
        if col["pk"] == 1:
            return col
    return None


def update_row(conn: sqlite3.Connection) -> None:
    table = pick_table(conn)
    if not table:
        return

    columns = get_table_info(conn, table)
    pk_col = find_primary_key(columns)
    if not pk_col:
        print("Cannot update safely: table has no primary key.")
        return

    pk_name = pk_col["name"]
    pk_type = pk_col["type"] or "TEXT"
    raw_pk = input(f"Primary key value for {pk_name}: ").strip()
    if raw_pk == "":
        print("Primary key is required.")
        return

    try:
        pk_value = parse_value(raw_pk, pk_type)
    except ValueError as err:
        print(f"Invalid primary key value: {err}")
        return

    updates = []
    params = []

    for col in columns:
        col_name = col["name"]
        if col_name == pk_name:
            continue

        declared_type = col["type"] or "TEXT"
        raw = input(f"New value for {col_name} ({declared_type}) [leave blank to skip]: ")
        if raw == "":
            continue

        try:
            value = parse_value(raw, declared_type)
        except ValueError as err:
            print(f"Invalid value for {col_name}: {err}")
            return

        updates.append(f'"{col_name}" = ?')
        params.append(value)

    if not updates:
        print("No changes entered.")
        return

    params.append(pk_value)
    query = f'UPDATE "{table}" SET {", ".join(updates)} WHERE "{pk_name}" = ?'
    cur = conn.execute(query, params)
    conn.commit()
    print(f"Rows updated: {cur.rowcount}")


def delete_row(conn: sqlite3.Connection) -> None:
    table = pick_table(conn)
    if not table:
        return

    columns = get_table_info(conn, table)
    pk_col = find_primary_key(columns)
    if not pk_col:
        print("Cannot delete safely: table has no primary key.")
        return

    pk_name = pk_col["name"]
    pk_type = pk_col["type"] or "TEXT"
    raw_pk = input(f"Primary key value for {pk_name}: ").strip()
    if raw_pk == "":
        print("Primary key is required.")
        return

    try:
        pk_value = parse_value(raw_pk, pk_type)
    except ValueError as err:
        print(f"Invalid primary key value: {err}")
        return

    confirm = input(f"Delete from {table} where {pk_name}={pk_value}? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Delete cancelled.")
        return

    query = f'DELETE FROM "{table}" WHERE "{pk_name}" = ?'
    cur = conn.execute(query, (pk_value,))
    conn.commit()
    print(f"Rows deleted: {cur.rowcount}")


def run_custom_sql(conn: sqlite3.Connection) -> None:
    print("Enter SQL (single statement).")
    sql = input("SQL> ").strip()
    if not sql:
        print("No SQL entered.")
        return

    try:
        cur = conn.execute(sql)
        if cur.description is None:
            conn.commit()
            print("Statement executed.")
            return

        rows = cur.fetchall()
        if not rows:
            print("(no rows)")
            return

        for row in rows:
            print(dict(row))
    except sqlite3.Error as err:
        print(f"SQL error: {err}")


def print_menu() -> None:
    print(
        """
==== Feedback Lens DB Console ====
1. Initialise database from schema.sql
2. List table names
3. View table rows
4. Insert row
5. Update row by primary key
6. Delete row by primary key
7. Run custom SQL
0. Exit
"""
    )


def main() -> None:
    with get_connection() as conn:
        while True:
            print_menu()
            choice = input("Choose an option: ").strip()

            if choice == "1":
                try:
                    initialise_database(DB_PATH, "schema.sql")
                    print(f"Database initialised at {DB_PATH}")
                except FileNotFoundError as err:
                    print(err)
            elif choice == "2":
                tables = get_tables(conn)
                if not tables:
                    print("No tables found.")
                else:
                    print("\n".join(tables))
            elif choice == "3":
                list_rows(conn)
            elif choice == "4":
                insert_row(conn)
            elif choice == "5":
                update_row(conn)
            elif choice == "6":
                delete_row(conn)
            elif choice == "7":
                run_custom_sql(conn)
            elif choice == "0":
                print("Goodbye.")
                break
            else:
                print("Invalid option.")


if __name__ == "__main__":
    main()
