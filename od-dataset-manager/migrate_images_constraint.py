#!/usr/bin/env python3
"""
Migrate images table: change UNIQUE constraint from (project_id, name) to (version_id, name).
Deduplicates records where the same (version_id, name) appears multiple times.
"""
import sqlite3
import os

DB_FILE = "annotations.db"

def migrate():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    print("=== Starting migration ===")

    # Check current state
    cursor.execute("SELECT COUNT(*) FROM images")
    total_before = cursor.fetchone()[0]
    print(f"Total images before migration: {total_before}")

    # Check for duplicates
    cursor.execute("""
        SELECT version_id, name, COUNT(*) as cnt
        FROM images
        WHERE version_id IS NOT NULL
        GROUP BY version_id, name
        HAVING cnt > 1
        ORDER BY cnt DESC
        LIMIT 5
    """)
    duplicates = cursor.fetchall()
    if duplicates:
        print(f"Found duplicate records (showing up to 5):")
        for row in duplicates:
            print(f"  version_id={row[0]}, name={row[1]}, count={row[2]}")
    else:
        print("No duplicates found")

    # Step 1: Create new table with correct constraint
    print("\n[1/5] Creating new images table...")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS images_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            name TEXT NOT NULL,
            filename TEXT NOT NULL,
            image_path TEXT NOT NULL,
            image_width INTEGER,
            image_height INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id),
            UNIQUE(version_id, name)
        )
    """)

    # Step 2: Deduplicate and migrate data (keep earliest record per version_id+name)
    print("[2/5] Migrating data (deduplicating)...")
    cursor.execute("""
        INSERT OR IGNORE INTO images_new
            (id, project_id, version_id, name, filename, image_path,
             image_width, image_height, created_at)
        SELECT
            MIN(id) as id,
            project_id,
            version_id,
            name,
            filename,
            image_path,
            image_width,
            image_height,
            created_at
        FROM images
        WHERE version_id IS NOT NULL
        GROUP BY version_id, name
        ORDER BY created_at ASC
    """)

    # Handle legacy records without version_id (if any)
    cursor.execute("SELECT COUNT(*) FROM images WHERE version_id IS NULL")
    legacy_count = cursor.fetchone()[0]
    if legacy_count > 0:
        print(f"  Found {legacy_count} legacy records without version_id, skipping...")

    # Step 3: Verify new table
    cursor.execute("SELECT COUNT(*) FROM images_new")
    total_after = cursor.fetchone()[0]
    print(f"\n[3/5] New table has {total_after} records")

    if total_after == 0 and total_before > 0:
        print("ERROR: Migration would delete all data! Aborting.")
        conn.rollback()
        conn.close()
        return False

    # Step 4: Replace old table
    print("[4/5] Replacing old table...")
    cursor.execute("DROP TABLE images")
    cursor.execute("ALTER TABLE images_new RENAME TO images")

    # Step 5: Commit
    conn.commit()
    print("[5/5] Migration committed!")

    # Verify final state
    cursor.execute("SELECT COUNT(*) FROM images")
    final_count = cursor.fetchone()[0]
    print(f"\n=== Migration complete: {total_before} → {final_count} records ===")

    # Show versions with 0 images (might need re-sync)
    cursor.execute("""
        SELECT v.id, v.name, v.project_id, COUNT(i.id) as img_count
        FROM versions v
        LEFT JOIN images i ON i.version_id = v.id
        GROUP BY v.id
        HAVING img_count = 0
    """)
    empty_versions = cursor.fetchall()
    if empty_versions:
        print(f"\n⚠️  Found {len(empty_versions)} versions with 0 images:")
        for row in empty_versions:
            print(f"  version_id={row[0]}, name={row[1]}, project={row[2]}")
        print("   These will be re-synced when you open them in the app.")

    conn.close()
    return True

if __name__ == "__main__":
    if not os.path.exists(DB_FILE):
        print(f"ERROR: {DB_FILE} not found")
        exit(1)

    success = migrate()
    exit(0 if success else 1)
