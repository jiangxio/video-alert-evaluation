#!/usr/bin/env python3
"""
Git文件分类脚本
分析git status输出，将文件分类为：应该提交、应该忽略、需要审查
"""

import json
import re
import sys
import os
from pathlib import Path


# 应该提交的文件扩展名
CODE_EXTENSIONS = {
    '.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.go', '.rs',
    '.cpp', '.c', '.h', '.hpp', '.cs', '.rb', '.php', '.swift',
    '.kt', '.scala', '.r', '.m', '.mm', '.pl', '.sh', '.bash',
    '.zsh', '.ps1', '.bat', '.cmd', '.html', '.htm', '.css',
    '.scss', '.sass', '.less', '.xml', '.svg'
}

# 配置文件扩展名
CONFIG_EXTENSIONS = {
    '.json', '.yaml', '.yml', '.toml', '.ini', '.conf', '.config',
    '.properties', '.cfg'
}

# 文档文件扩展名
DOC_EXTENSIONS = {
    '.md', '.txt', '.rst', '.adoc', '.org'
}

# 不应该提交的数据文件扩展名
DATA_EXTENSIONS = {
    '.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv',
    '.mp3', '.wav', '.flac', '.aac', '.ogg',
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.ico',
    '.zip', '.tar', '.gz', '.bz2', '.7z', '.rar',
    '.db', '.sqlite', '.sqlite3', '.mdb', '.accdb',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.log'
}

# 不应该提交的目录名
SKIP_DIRECTORIES = {
    '__pycache__', 'node_modules', '.git', '.svn', '.hg',
    'venv', '.venv', 'env', '.env', 'virtualenv',
    'dist', 'build', 'target', 'out', 'output',
    '.cache', '.pytest_cache', '.mypy_cache', '.tox',
    '.idea', '.vscode', '.vs',
    'coverage', 'htmlcov', '.coverage',
    'logs', 'tmp', 'temp', 'uploads', 'downloads',
    'generated_videos', 'thumbnails', 'ground_truth_frames',
    'report', 'wechat-bridge'
}

# 不应该提交的文件名模式
SKIP_FILE_PATTERNS = [
    r'^\.',           # 隐藏文件
    r'\.tmp$',        # 临时文件
    r'\.temp$',       # 临时文件
    r'\.log$',        # 日志文件
    r'\.pyc$',        # Python缓存
    r'\.pyo$',        # Python缓存
    r'\.DS_Store$',   # macOS文件
    r'Thumbs\.db$',   # Windows文件
    r'\.env$',        # 环境变量
    r'\.env\.local$',
    r'\.env\..*$',
    r'.*\.key$',      # 密钥文件
    r'.*\.pem$',
    r'.*\.crt$',
    r'.*\.p12$',
    r'EOF$',           # 空文件标记
]

# 需要特别注意的文件名
REVIEW_PATTERNS = [
    r'\.env',          # 环境变量文件
    r'config.*\.json', # 配置文件
    r'secret',         # 密钥相关
    r'password',
    r'credential',
    r'test.*\.json',   # 测试数据
    r'test.*\.csv',
    r'mock.*\.json',
]


def get_file_size(filepath):
    """获取文件大小（字节）"""
    try:
        return os.path.getsize(filepath)
    except:
        return 0


def classify_file(filepath, status_type):
    """
    分类单个文件
    返回: ('to_commit' | 'to_ignore' | 'to_review', reason)
    """
    path = Path(filepath)
    filename = path.name
    ext = path.suffix.lower()

    # 检查是否是特殊文件
    if filename in ['.gitignore', '.gitattributes', '.gitmodules']:
        return ('to_commit', '版本控制文件')

    if filename in ['requirements.txt', 'package.json', 'Cargo.toml',
                    'go.mod', 'pom.xml', 'build.gradle', 'Gemfile',
                    'composer.json', 'Podfile']:
        return ('to_commit', '依赖定义文件')

    # 检查是否在跳过目录中
    for part in path.parts:
        if part in SKIP_DIRECTORIES:
            return ('to_ignore', f'在忽略目录 {part} 中')

    # 检查文件名模式
    for pattern in SKIP_FILE_PATTERNS:
        if re.match(pattern, filename, re.IGNORECASE):
            return ('to_ignore', f'匹配忽略模式: {pattern}')

    # 检查数据文件扩展名
    if ext in DATA_EXTENSIONS:
        size = get_file_size(filepath)
        if size > 10 * 1024 * 1024:  # >10MB
            return ('to_review', f'大文件 ({size/1024/1024:.1f}MB)，建议不提交')
        return ('to_ignore', f'数据文件 ({ext})')

    # 检查是否需要审查
    for pattern in REVIEW_PATTERNS:
        if re.search(pattern, filepath, re.IGNORECASE):
            return ('to_review', f'可能包含敏感信息或测试数据')

    # 检查代码文件
    if ext in CODE_EXTENSIONS:
        return ('to_commit', f'源代码文件 ({ext})')

    # 检查配置文件
    if ext in CONFIG_EXTENSIONS:
        return ('to_commit', f'配置文件 ({ext})')

    # 检查文档文件
    if ext in DOC_EXTENSIONS:
        return ('to_commit', f'文档文件 ({ext})')

    # 未知类型，默认为需要审查
    return ('to_review', f'未知类型，请确认 ({ext if ext else "无扩展名"})')


def parse_git_status(status_output):
    """
    解析git status --porcelain的输出
    返回: [(status, filepath), ...]
    """
    files = []
    for line in status_output.strip().split('\n'):
        if not line.strip():
            continue

        # git status --porcelain 格式: XY filename 或 XY "filename"
        # X = staged status, Y = unstaged status
        if line.startswith('"'):
            # 带引号的文件名
            match = re.match(r'"(..) (.*)"$', line)
            if match:
                status = match.group(1)
                filepath = match.group(2)
                files.append((status, filepath))
        else:
            status = line[:2]
            filepath = line[3:].strip()
            files.append((status, filepath))

    return files


def get_status_description(status):
    """获取git状态描述"""
    staged, unstaged = status[0], status[1]

    status_map = {
        'M': '修改',
        'A': '新增',
        'D': '删除',
        'R': '重命名',
        'C': '复制',
        'U': '更新',
        '?': '未跟踪',
    }

    if staged == '?' or unstaged == '?':
        return '未跟踪'
    if staged != ' ' and staged in status_map:
        return f'已暂存({status_map[staged]})'
    if unstaged != ' ' and unstaged in status_map:
        return status_map[unstaged]

    return '未知'


def classify_files(status_output, repo_path='.'):
    """
    主函数：分类所有文件
    """
    files = parse_git_status(status_output)

    result = {
        'to_commit': [],
        'to_ignore': [],
        'to_review': [],
        'summary': {
            'total': len(files),
            'to_commit': 0,
            'to_ignore': 0,
            'to_review': 0
        }
    }

    for status, filepath in files:
        status_desc = get_status_description(status)
        category, reason = classify_file(filepath, status)

        file_info = {
            'path': filepath,
            'status': status_desc,
            'reason': reason
        }

        result[category].append(file_info)
        result['summary'][category] += 1

    return result


def main():
    """主函数"""
    # 读取git status输出
    if len(sys.argv) > 1:
        # 从文件读取
        with open(sys.argv[1], 'r') as f:
            status_output = f.read()
    else:
        # 从标准输入读取
        status_output = sys.stdin.read()

    # 获取仓库路径
    repo_path = sys.argv[2] if len(sys.argv) > 2 else '.'

    # 分类文件
    result = classify_files(status_output, repo_path)

    # 输出JSON
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
