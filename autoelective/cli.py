"""Small public command-line surface; no probing or credential dumps."""
import argparse
from pathlib import Path

from .session import ROOT, login, check
from .config import load_settings


def main():
    parser = argparse.ArgumentParser(description='USTCAutoElective：登录、只读监控及显式授权选退课')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('login', 'check-session', 'check-config', 'monitor'):
        command = sub.add_parser(name)
        command.add_argument('--config', type=Path, default=ROOT / 'config.ini')
    sub.choices['login'].add_argument('--prompt-credentials', action='store_true',
                                     help='只在本机交互终端输入凭据，由官网表单提交一次；不保存密码')
    sub.choices['login'].add_argument('--timeout', type=int, default=300)
    sub.choices['monitor'].add_argument('--dry-run', action='store_true', help='强制只读，覆盖 auto 配置')
    sub.choices['monitor'].add_argument('--cycles', type=int, default=0, help='运行轮数；0 表示持续运行')
    sub.add_parser('status', help='读取本地最后心跳；不会启动监控')
    sub.add_parser('stop', help='请求当前事务结束或进入未知保护后安全退出')
    args = parser.parse_args()
    try:
        if args.command == 'check-config':
            settings = load_settings(args.config)
            print(f'配置有效：mode={settings.mode}, 间隔={settings.interval:g}s, '
                  f'学分上限={settings.max_credits:g}, 目标数={len(settings.targets)}, '
                  f'冲突退课规则={len(settings.swaps)}, 成功后退课规则={len(settings.after_select)}。')
            print('仅离线格式核验，不代表课程存在、可选或符合培养要求。')
            return 0
        if args.command in ('monitor', 'status', 'stop'):
            from .monitor import run, show_status, request_stop
            if args.command == 'monitor':
                return run(args)
            return show_status() if args.command == 'status' else request_stop()
        return login(args) if args.command == 'login' else check(args)
    except KeyboardInterrupt:
        print('用户取消；若中断选退课，请先核对官网课表和本地事务记录。')
        return 130
    except Exception as error:
        # Never print browser exceptions, response bodies, URLs or credential values.
        print(f'未完成（{type(error).__name__}）；请按 README 核对配置、网络或重新登录。')
        return 1
