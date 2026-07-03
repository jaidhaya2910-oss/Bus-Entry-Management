import sqlite3
from datetime import datetime

DB_NAME = "bus_entry.db"

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def add_column_if_missing(cur, table, column, definition):
    cur.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cur.fetchall()]
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def create_database():
    conn = get_db()
    cur = conn.cursor()

    cur.execute('''
    CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        name TEXT NOT NULL,
        role TEXT DEFAULT 'Admin',
        created_at TEXT NOT NULL
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS buses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bus_number TEXT UNIQUE NOT NULL,
        bus_name TEXT,
        driver_name TEXT,
        driver_mobile TEXT,
        route_name TEXT,
        entry_time TEXT,
        exit_time TEXT,
        status TEXT DEFAULT 'OUTSIDE',
        active TEXT DEFAULT 'YES',
        created_at TEXT NOT NULL
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS entry_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bus_id INTEGER,
        bus_number TEXT NOT NULL,
        bus_name TEXT,
        driver_name TEXT,
        route_name TEXT,
        scheduled_entry_time TEXT,
        actual_entry_time TEXT,
        actual_exit_time TEXT,
        entry_status TEXT,
        late_minutes INTEGER DEFAULT 0,
        bus_status TEXT DEFAULT 'INSIDE',
        image_path TEXT,
        log_date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(bus_id) REFERENCES buses(id)
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS unknown_buses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        detected_number TEXT,
        image_path TEXT,
        detected_time TEXT,
        log_date TEXT NOT NULL
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        college_name TEXT DEFAULT 'Smart College',
        report_email TEXT DEFAULT '',
        updated_at TEXT
    )
    ''')

    add_column_if_missing(cur, "settings", "smtp_host", "TEXT DEFAULT 'smtp.gmail.com'")
    add_column_if_missing(cur, "settings", "smtp_port", "TEXT DEFAULT '587'")
    add_column_if_missing(cur, "settings", "smtp_email", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "settings", "smtp_password", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "entry_logs", "entry_type", "TEXT DEFAULT 'FIRST ENTRY'")
    add_column_if_missing(cur, "entry_logs", "duplicate_count", "INTEGER DEFAULT 1")

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cur.execute("INSERT OR IGNORE INTO admins (username,password,name,role,created_at) VALUES (?,?,?,?,?)",
                ('admin','admin123','System Admin','Admin',now))

    # Default demo bus data removed permanently.
    # Only buses added from the Bus Master page will be stored in the database.
    default_bus_numbers = ('TN45AB1234', 'TN45AB5678', 'TN45CD1111', 'TN45CD2222')
    cur.execute(
        "DELETE FROM buses WHERE bus_number IN (?,?,?,?)",
        default_bus_numbers
    )

    cur.execute("INSERT INTO settings (college_name, report_email, updated_at) SELECT 'Smart College', '', ? WHERE NOT EXISTS (SELECT 1 FROM settings)", (now,))
    conn.commit()
    conn.close()

if __name__ == '__main__':
    create_database()
    print('Database created successfully!')
