"""核对麦弦监听进程并等待它退出。WSL 通过 stdin 运行同一份代码。"""
import os
import re
import select
import signal
import sys
from pathlib import Path


def matches(root, cwd, argv, port):
    if not argv or Path(cwd).resolve() != root:
        return False
    name = Path(argv[0]).name.lower()
    args = argv[1:]
    if re.fullmatch(r'python(?:w|[0-9.]*)?(?:\.exe)?', name):
        if args == ['-m', 'cover_api.app']:
            return True
        if args[:2] == ['-m', 'uvicorn']:
            args = args[2:]
        elif args and Path(args[0]).name == 'uvicorn':
            args = args[1:]
        else:
            return False
    elif name not in ('uvicorn', 'uvicorn.exe'):
        return False
    if args[:1] != ['cover_api.app:app']:
        return False
    # 只接受安装器和文档使用的选项，不接受改变应用来源的 --app-dir 等参数。
    opts = args[1:]
    pairs = dict(zip(opts[::2], opts[1::2]))
    return (len(opts) == 2 * len(pairs) and pairs.keys() <= {'--host', '--port'}
            and pairs.get('--port', '8000') == str(port))


def linux_sockets(port):
    result = set()
    for name in ('tcp', 'tcp6'):
        table = Path('/proc/net') / name
        if table.exists():
            for line in table.read_text().splitlines()[1:]:
                fields = line.split()
                if fields[3] == '0A' and int(fields[1].split(':')[1], 16) == port:
                    result.add('socket:[' + fields[9] + ']')
    return result


def linux_stop(root, port):
    sockets = linux_sockets(port)
    if not sockets:
        return 0
    if len(sockets) != 1:
        raise RuntimeError('未找到唯一监听套接字')
    owners = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            if any(os.readlink(fd) in sockets for fd in (proc / 'fd').iterdir()):
                owners.append(proc)
        except (FileNotFoundError, PermissionError):
            continue
    if len(owners) != 1:
        raise RuntimeError('无法确认唯一监听进程')
    proc, = owners
    pid = int(proc.name)
    handle = os.pidfd_open(pid)
    try:
        argv = [os.fsdecode(s) for s in (proc / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')]
        if (not matches(root, proc / 'cwd', argv, port) or linux_sockets(port) != sockets
                or not any(os.readlink(fd) in sockets for fd in (proc / 'fd').iterdir())):
            raise RuntimeError('监听进程与项目或入口不符')
        signal.pidfd_send_signal(handle, signal.SIGTERM)
        poll = select.poll()
        poll.register(handle, select.POLLIN)
        if not poll.poll(8000):
            raise RuntimeError('等待进程退出超时')
    finally:
        os.close(handle)
    return pid


def windows_stop(root, port):
    import psutil
    owners = {c.pid for c in psutil.net_connections(kind='tcp')
              if c.status == psutil.CONN_LISTEN and c.laddr.port == port}
    if not owners:
        return 0
    if len(owners) != 1 or None in owners:
        raise RuntimeError('未找到唯一监听进程')
    proc = psutil.Process(owners.pop())
    if not matches(root, proc.cwd(), proc.cmdline(), port):
        raise RuntimeError('监听进程与项目或入口不符')
    if not any(c.status == psutil.CONN_LISTEN and c.laddr.port == port for c in proc.net_connections('tcp')):
        raise RuntimeError('监听进程已变化')
    proc.terminate()
    proc.wait(8)
    return proc.pid


if __name__ == '__main__':
    try:
        raw, port = sys.argv[1:]
        if not raw and os.name != 'nt':
            raw = next((str(p) for p in (Path.home() / 'feiyu-cover-api', Path.home() / 'feiyu-cover',
                       *(Path('/mnt') / d / 'feiyu-cover' for d in ('g', 'd', 'c')))
                       if (p / 'cover_api/app.py').is_file()), '')
        root = Path(raw).expanduser().resolve()
        if not raw or not (root / 'cover_api/app.py').is_file():
            raise RuntimeError('未找到绑定的 API 项目目录')
        print((windows_stop if os.name == 'nt' else linux_stop)(root, int(port)))
    except Exception as exc:
        sys.exit(str(exc))
