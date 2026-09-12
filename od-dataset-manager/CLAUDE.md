# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Flask-based web application for managing and annotating object detection datasets, primarily focused on detecting phone calls ("call_phone", "not_call_phone", "other"). The application supports multiple data formats (COCO, YOLO) and provides a web-based annotation interface.

## Running the Application

```bash
# Start the Flask development server
python app.py

# The server runs on http://0.0.0.0:5000
```

The application will automatically create necessary directories (images, labels, backup) on startup.

## Configuration

Edit [config.py](config.py) to customize:

```python
# Class/label definitions for the dataset
CLASSES = ["call_phone", "not_call_phone", "other"]

# Dataset directory paths
IMAGES_DIR = "datasets/calling/images"
LABELS_DIR = "datasets/calling/labels"
BACKUP_LABELS_DIR = os.path.join(LABELS_DIR, "backup")

# Supported image formats
IMAGE_EXTS = ['.jpg', '.jpeg', '.png']
```

## Architecture

### Backend Structure

**[app.py](app.py)** - Flask application with REST API endpoints:
- `GET /` - Serves the main web interface
- `GET /api/images` - Lists all images with annotation status
- `GET /image/<name>` - Serves image files
- `GET /api/labels/<name>` - Retrieves label JSON for an image
- `POST /api/labels/<name>` - Saves label JSON to disk
- `POST /api/export/coco` - Exports dataset to COCO format
- `POST /api/import/coco` - Imports COCO format dataset
- `POST /api/export/yolo` - Exports dataset to YOLO format
- `POST /api/import/yolo` - Imports YOLO format for a specific image

**Security features:**
- Filename validation using regex `^[A-Za-z0-9_.-]+$` to prevent path traversal
- All label changes are automatically backed up with timestamps
- Image dimensions are validated against saved labels

### Frontend Structure

**[templates/index.html](templates/index.html)** - Main UI template
**[static/style.css](static/style.css)** - Application styling
**[static/main.js](static/main.js)** - Annotation interaction logic (SVG-based bounding box drawing)

### Dataset Structure

```
datasets/calling/
├── images/          # Original image files (.jpg, .png)
├── labels/          # Annotation files (JSON format, one per image)
│   └── backup/      # Automatic backups of modified labels
├── result/          # Processed images
├── anno_images/     # Annotated images
├── fu/              # Additional dataset files
└── scripts/         # Helper scripts for dataset processing
```

## Data Formats

### Internal Label Format (JSON)

Saved in `labels/<name>.json`:

```json
{
  "imagePath": "0040.png",
  "imageHeight": 480,
  "imageWidth": 640,
  "shapes": [
    {
      "label": "call_phone",
      "points": [[97.7, 61.1], [129.1, 97.2]],
      "group_id": null,
      "shape_type": "rectangle",
      "flags": {}
    }
  ]
}
```

### Processing Scripts

**[process_dataset.py](process_dataset.py)** - Dataset utility script:
- Backs up original files
- Converts LabelMe format to internal JSON format
- Creates empty JSON files for unlabeled images
- Removes orphaned label files
- Renames files sequentially (0001, 0002, etc.)
- Generates COCO format output

```bash
# Run the dataset processor
python datasets/calling/process_dataset.py
```

**[datasets/calling/scripts/](datasets/calling/scripts/)** - Additional helper scripts:
- `create_empty_json.py` - Creates empty label files
- `convert_xml_to_json.py` - Converts XML annotations to JSON
- `select_phone_xml.py` - Filters phone-related XML files
- `rename_json_files.py` - Renames JSON label files
- `process_dataset.py` - Alternate dataset processor

## Important Notes

- Images and labels are linked by filename (without extension)
- All file operations are sanitized using the `safe_basename()` function
- Label files are automatically backed up before overwriting with timestamp-based names
- The web interface supports pagination, filtering by annotation status, and real-time drawing
- COCO export uses class indices starting from 1
- YOLO format uses normalized coordinates (0-1) relative to image dimensions
