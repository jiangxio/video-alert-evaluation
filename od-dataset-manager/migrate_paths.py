#!/usr/bin/env python3
"""One-time migration: convert absolute paths to relative paths in DB."""

import os
import sqlite3
import config

DB_FILE = os.path.join(config.BASE_DIR, 'annotations.db')


def _rel_path(abs_path):
    if abs_path and abs_path.startswith(config.BASE_DIR + os.sep):
        return os.path.relpath(abs_path, config.BASE_DIR)
    return abs_path


def main():
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row

        # Migrate projects
        rows = conn.execute('SELECT id, images_dir, labels_dir FROM projects').fetchall()
        for row in rows:
            new_img_dir = _rel_path(row['images_dir'])
            new_lbl_dir = _rel_path(row['labels_dir'])
            if new_img_dir != row['images_dir'] or new_lbl_dir != row['labels_dir']:
                conn.execute(
                    'UPDATE projects SET images_dir = ?, labels_dir = ? WHERE id = ?',
                    (new_img_dir, new_lbl_dir, row['id'])
                )
                print(f"Project {row['id']}: images_dir={new_img_dir}, labels_dir={new_lbl_dir}")

        # Migrate images
        rows = conn.execute('SELECT id, image_path FROM images').fetchall()
        for row in rows:
            new_path = _rel_path(row['image_path'])
            if new_path != row['image_path']:
                conn.execute(
                    'UPDATE images SET image_path = ? WHERE id = ?',
                    (new_path, row['id'])
                )
                print(f"Image {row['id']}: {new_path}")

        conn.commit()
    print("Migration complete.")


if __name__ == '__main__':
    main()
