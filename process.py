#!/usr/bin/env python3
"""
视频水印与告警评估平台 — 统一命令行入口

把分散在 scripts/（正式功能）与项目根目录（分析报告）的脚本收编为
子命令式入口，提供一致的 --help 体验。

机制：子命令命中后用 subprocess 透传剩余参数到目标脚本，退出码原样回传。
这与改造前 process.py 转发 scripts/process_single.py 的模式一致，也是
web 服务之外的既定 CLI 调用方式；脚本文件本身保持原位（app 服务直接
import 它们，不能移动）。

用法:
  python process.py                        # 显示全部子命令
  python process.py <command> [options]    # 运行某子命令
  python process.py <command> --help       # 查看该命令的参数（带 argparse 的
                                          # 脚本透传原生帮助，无 argparse 的显示本入口说明）

旧接口兼容:
  python process.py --single <file>   →   python process.py watermark <file>
  python process.py --batch           →   python process.py watermark-batch
  python process.py --install         →   python process.py install
  （旧写法仍可用，会打印一行弃用提示）
"""
import argparse
import subprocess
import sys
from collections import namedtuple
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


# 子命令注册表：(名称, 相对脚本路径, 分组, 一行说明, 是否带 argparse)
# script=None 表示内置实现（不走转发），has_argparse 仅影响 --help 的处理方式。
Command = namedtuple('Command', ['name', 'script', 'group', 'desc', 'has_argparse'])

_COMMANDS = [
    Command('install', None, '环境', '安装 Python 依赖 (pip install -r requirements.txt)', False),

    Command('watermark', 'scripts/process_single.py', '水印', '给单个视频添加水印', True),
    Command('watermark-batch', 'scripts/batch_process.py', '水印', '批量给所有视频添加水印', False),

    Command('ocr', 'scripts/ocr_easy.py', 'OCR', 'EasyOCR 识别截图水印', True),
    Command('ocr-paddle', 'scripts/final_ocr.py', 'OCR', 'PaddleOCR 识别截图水印', True),

    Command('verify', 'scripts/verify_alert.py', '验证', '验证告警图片是否命中 ground truth', True),

    Command('stream', 'scripts/stream_videos.py', '推流', '按顺序推流到 MediaMTX (RTSP)', True),
    Command('stream-fight', 'scripts/stream_fight_loop.py', '推流', 'Fight/NonFight 拼接循环推流', True),
    Command('stream-merged', 'scripts/stream_merged_sources.py', '推流', '同源片段合并后推流', True),

    Command('db-fix-duplicates', 'scripts/fix_duplicate_video_ids.py', '数据库', '清理 videos 表重复 video_id', False),

    Command('recall', 'compute_fight_recall.py', '分析报告', '计算推流测试集召回率（按视频统计告警命中/漏报，输出汇总 CSV 与 Markdown 报告）', False),
    Command('recall-audit', 'independent_recall_audit.py', '分析报告', '独立召回审计', False),
    Command('leakage', 'leakage_audit.py', '分析报告', '泄漏审计', False),
    Command('leakage-v2', 'leakage_audit_v2.py', '分析报告', '泄漏审计 v2', False),
    Command('detection-report', 'gen_detection_report.py', '分析报告', '生成检测报告', False),
    Command('retest-report', 'gen_retest_report.py', '分析报告', '生成复测报告', False),
    Command('algo-condition', 'check_algo_condition.py', '分析报告', '查看 AIBOX 算法 condition 参数', False),
    Command('annotate-alarms', 'annotate_alarm_images.py', '分析报告', '标注告警图片目标框', False),
    Command('md2pdf', 'md_to_pdf.py', '分析报告', 'Markdown 转 PDF', False),
]

_BY_NAME = {c.name: c for c in _COMMANDS}


def install_deps():
    """安装 requirements.txt 中的 Python 依赖"""
    print("安装 Python 依赖...")
    req_file = PROJECT_ROOT / 'requirements.txt'
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(req_file)], check=True)
    print("依赖安装完成！")


def _grouped_commands():
    """按首次出现顺序聚合分组，返回 [(group, [Command, ...]), ...]"""
    order = []
    groups = {}
    for cmd in _COMMANDS:
        if cmd.group not in groups:
            groups[cmd.group] = []
            order.append(cmd.group)
        groups[cmd.group].append(cmd)
    return [(g, groups[g]) for g in order]


def print_help():
    """打印按分组组织的顶层帮助"""
    print("视频水印与告警评估平台 — 统一命令行入口\n")
    print("用法: python process.py <command> [options]")
    print("      python process.py <command> --help   # 查看该命令的参数")
    print("      python process.py                    # 显示本帮助\n")
    print("运行 `python process.py <command> --help` 查看该命令的详细参数。")
    print("带 argparse 的命令透传原生帮助；无 argparse 的显示本入口说明。\n")
    for group, cmds in _grouped_commands():
        print(f"  [{group}]")
        for cmd in cmds:
            print(f"    {cmd.name:<20} {cmd.desc}")
        print()


def _print_command_help(cmd):
    """为无 argparse 的命令打印本入口提供的说明（避免透传 --help 到不支持它的脚本而报错）"""
    target = "（内置）" if cmd.script is None else f"scripts/..  →  {cmd.script}"
    print(f"{cmd.name}: {cmd.desc}")
    print(f"  目标: {target}")
    print(f"  用法: python process.py {cmd.name} [该脚本自身的参数]")
    if cmd.script is not None:
        print(f"  该命令直接转发到 {cmd.script}，未在统一入口额外定义参数，")
        print(f"  如脚本支持参数，请直接运行: python {cmd.script} --help")


def run_script(script_rel, extra_args):
    """转发到目标脚本，透传参数与退出码"""
    script_path = PROJECT_ROOT / script_rel
    if not script_path.exists():
        print(f"错误: 转发脚本不存在: {script_path}", file=sys.stderr)
        return 1
    cmd = [sys.executable, str(script_path)] + list(extra_args)
    result = subprocess.run(cmd)
    return result.returncode


def _compat_shim(argv):
    """旧标志 --single/--batch/--install 自动映射到新子命令，并打印弃用提示"""
    if not argv:
        return argv
    first = argv[0]
    mapping = {
        '--single': 'watermark',
        '--batch': 'watermark-batch',
        '--install': 'install',
    }
    if first in mapping:
        new_cmd = mapping[first]
        print(
            f"[提示] '{first}' 已弃用，等价于 `process.py {new_cmd}`，请改用新写法。",
            file=sys.stderr,
        )
        return [new_cmd] + argv[1:]
    return argv


def main():
    argv = _compat_shim(sys.argv[1:])

    # 无参数或顶层 -h/--help：打印分组帮助（不报错）
    if not argv or argv[0] in ('-h', '--help'):
        print_help()
        return

    # 用 argparse 仅做子命令识别 + invalid choice 报错；每个子命令 add_help=False，
    # 因此 --help 等会落入 remainder 被透传或拦截，不会被 argparse 消费。
    parser = argparse.ArgumentParser(prog='python process.py', add_help=False)
    sub = parser.add_subparsers(dest='command', metavar='<command>')
    for cmd in _COMMANDS:
        sub.add_parser(cmd.name, add_help=False)

    args, remainder = parser.parse_known_args(argv)

    if args.command is None:
        print_help()
        return

    cmd = _BY_NAME[args.command]

    # 内置命令
    if cmd.name == 'install':
        if any(a in ('-h', '--help') for a in remainder):
            print("install: 安装 Python 依赖 (pip install -r requirements.txt)")
            print("用法: python process.py install")
            return
        install_deps()
        return

    # 无 argparse 的命令：拦截 --help，打印本入口说明，避免透传到脚本报错
    if not cmd.has_argparse and any(a in ('-h', '--help') for a in remainder):
        _print_command_help(cmd)
        return

    sys.exit(run_script(cmd.script, remainder))


if __name__ == '__main__':
    main()
